"""Gateway relayer: verify a device voucher, build the `settle()` transaction.

Import path: run from the repo root so `shared/` and `gateway/` resolve as
namespace packages (`uvicorn gateway.api:app`).

Instrumentation (CLAUDE.md, "Instrumentation"): this module owns marks t2-t5.
`t0`/`t1` are the Pi's clock and ride along in the request body untouched, so
the experiment runner can write one CSV row per settlement attempt and still
decompose signing vs transport vs chain cost.

Submission is live as of M1, but gated: `RelayerConfig.submit` (env
`GATEWAY_SUBMIT`) decides whether `relay()` broadcasts the transaction or stops
once the calldata is built. Off by default, so constructing a Relayer can never
spend gas by accident — `scripts/e2e_m1.py` and `experiments/runner.py` turn it
on deliberately. The receipt fields the cost table needs (`gasUsed`,
`effectiveGasPrice`, and `l1Fee` on OP-stack chains) come back on `RelayResult`.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from eth_account import Account
from eth_account.messages import SignableMessage, encode_typed_data
from eth_utils import is_address, to_checksum_address
from hexbytes import HexBytes
from pydantic import BaseModel
from web3 import Web3
from web3.exceptions import ContractLogicError

from shared.voucher import Voucher

log = logging.getLogger(__name__)

ZERO_ADDRESS = "0x" + "00" * 20
SIGNATURE_LENGTH = 65  # r + s + v
RPC_TIMEOUT_S = 2.0

RECEIPT_TIMEOUT_S = 120.0
"""Ceiling on the wait for inclusion. Long enough for a slow public testnet
block, short enough that a stuck run fails instead of hanging the harness."""

CONFIRMATION_POLL_S = 0.2
GAS_LIMIT_MARGIN = 1.25
"""Headroom over `estimate_gas`. Does not distort the cost table: that reports
`gasUsed` from the receipt, not the limit we set."""

DEFAULT_PRIORITY_FEE_WEI = 1_000_000_000  # 1 gwei, if the node won't suggest one

# Minimal ABI for the function in CLAUDE.md, "Contract, core shape". Only the
# `settle` fragment is here because that is all the relayer encodes; the full
# ABI comes from `forge build` once the contract lands (M1).
SETTLE_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "settle",
        "stateMutability": "nonpayable",
        "outputs": [],
        "inputs": [
            {
                "name": "v",
                "type": "tuple",
                "components": [
                    {"name": "channel", "type": "address"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "cumulativeAmount", "type": "uint256"},
                    {"name": "serviceRef", "type": "bytes32"},
                    {"name": "deadline", "type": "uint64"},
                ],
            },
            {"name": "sigDevice", "type": "bytes"},
            {"name": "sigGateway", "type": "bytes"},
            {"name": "sigMerchant", "type": "bytes"},
        ],
    }
]


class VoucherRejected(Exception):
    """Voucher failed a pre-submission check - the gateway will not pay gas for it.

    Every check here is one the contract also enforces on-chain; failing fast
    off-chain just saves a guaranteed-to-revert transaction. The contract stays
    the authority (H2) - rejecting here is an optimisation, never the security
    boundary.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RelayerNotReady(Exception):
    """Gateway is missing configuration it needs to relay (e.g. its own key)."""


@dataclass(frozen=True)
class RelayerConfig:
    """Relayer settings, read from the environment (see `.env.example`).

    Lives here rather than in `shared/config.py` because these values are the
    gateway's alone - the device and merchant never hold an RPC URL or the
    gateway key. Move it if a second component ever needs the same values.
    """

    rpc_url: str = "http://127.0.0.1:8545"
    chain_id: int = 31337  # anvil
    payment_channel: str = ZERO_ADDRESS
    gateway_private_key: str | None = None
    device_address: str | None = None
    merchant_address: str | None = None
    confirmations: int = 1

    submit: bool = False
    """Broadcast `settle()`, or stop once the calldata is built?

    Off by default so that importing or constructing a Relayer can never spend
    gas on its own. `scripts/e2e_m1.py` and the experiment runner set
    `GATEWAY_SUBMIT=1`; the unit tests leave it off and stay chain-free.
    """

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RelayerConfig:
        env = os.environ if env is None else env
        defaults = cls()

        def address(name: str, default: str | None) -> str | None:
            raw = env.get(name, "").strip()
            if not raw:
                return default
            if not is_address(raw):
                raise ValueError(f"{name} is not a valid address: {raw!r}")
            return to_checksum_address(raw)

        def boolean(name: str, default: bool) -> bool:
            raw = env.get(name, "").strip().lower()
            if not raw:
                return default
            if raw in ("1", "true", "yes", "on"):
                return True
            if raw in ("0", "false", "no", "off"):
                return False
            raise ValueError(f"{name} must be a boolean, got {raw!r}")

        def integer(name: str, default: int) -> int:
            raw = env.get(name, "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer, got {raw!r}") from exc

        payment_channel = address("PAYMENT_CHANNEL_ADDRESS", defaults.payment_channel)
        if payment_channel == ZERO_ADDRESS:
            log.warning(
                "PAYMENT_CHANNEL_ADDRESS unset; using the zero address as the EIP-712 "
                "verifyingContract. Device signatures only verify if the device signed "
                "against that same address. Set it after the M1 deploy."
            )

        return cls(
            rpc_url=env.get("RPC_URL", "").strip() or defaults.rpc_url,
            chain_id=integer("CHAIN_ID", defaults.chain_id),
            payment_channel=payment_channel or defaults.payment_channel,
            gateway_private_key=env.get("GATEWAY_PRIVATE_KEY", "").strip() or None,
            device_address=address("DEVICE_ADDRESS", defaults.device_address),
            merchant_address=address("MERCHANT_ADDRESS", defaults.merchant_address),
            confirmations=integer("CONFIRMATIONS", defaults.confirmations),
            submit=boolean("GATEWAY_SUBMIT", defaults.submit),
        )


class RelayResult(BaseModel):
    """One settlement attempt - also the row the experiment runner records.

    The voucher is echoed field-by-field rather than nested as a `Voucher`
    because `serviceRef` is raw `bytes`, which has no faithful JSON form; it
    goes back out as a 0x-hex string, the way it arrived.
    """

    accepted: bool
    submitted: bool
    reason: str | None = None

    channel: str
    nonce: int
    cumulativeAmount: int
    serviceRef: str

    device: str
    gateway: str | None = None
    merchant: str | None = None

    txHash: str | None = None
    calldata: str

    # Cost table inputs, straight off the receipt. `l1Fee` is None on chains
    # that have no L1 data fee and must not be read as zero on the ones that
    # do - on Base it is most of the real cost, which is the understatement
    # CLAUDE.md warns about.
    blockNumber: int | None = None
    gasUsed: int | None = None
    effectiveGasPrice: int | None = None
    l1Fee: int | None = None

    # Instrumentation marks. t0/t1 are the device's clock, t2-t5 the gateway's;
    # read the clock-skew warning in CLAUDE.md before subtracting across that line.
    t0: float | None = None  # Pi: sensor event fires
    t1: float | None = None  # Pi: voucher signed
    t2: float | None = None  # gateway: voucher received over transport
    t3: float | None = None  # gateway: tx broadcast, hash returned
    t4: float | None = None  # gateway: receipt received (included in block)
    t5: float | None = None  # gateway: N-confirmation depth reached


class Relayer:
    """Verifies vouchers and turns them into `settle()` transactions.

    Stateless with respect to channels: it tracks neither nonces nor paid
    amounts. Monotonicity and stale-nonce rejection are the contract's job
    on-chain and `gateway/channel.py`'s job off-chain (M4); a third copy of
    that state here would only be one more thing to keep in sync.
    """

    def __init__(self, config: RelayerConfig | None = None, w3: Web3 | None = None) -> None:
        self.config = config or RelayerConfig.from_env()
        self.w3 = w3 or Web3(
            Web3.HTTPProvider(self.config.rpc_url, request_kwargs={"timeout": RPC_TIMEOUT_S})
        )
        self._contract = self.w3.eth.contract(
            address=to_checksum_address(self.config.payment_channel), abi=SETTLE_ABI
        )
        self._account = (
            Account.from_key(self.config.gateway_private_key)
            if self.config.gateway_private_key
            else None
        )

    @property
    def gateway_address(self) -> str | None:
        """The gateway's own signer address, or None if no key is configured."""
        return self._account.address if self._account else None

    # --- signing / recovery -------------------------------------------------

    def signable(self, voucher: Voucher) -> SignableMessage:
        """EIP-712 signable form of `voucher`, bound to this chain and contract."""
        return encode_typed_data(
            full_message=voucher.to_typed_data(
                chain_id=self.config.chain_id,
                verifying_contract=self.config.payment_channel,
            )
        )

    def recover(self, voucher: Voucher, signature: bytes) -> str:
        """Recover the address that signed `voucher`, or raise `VoucherRejected`.

        A malformed signature is a rejection, not a crash - anything at all can
        POST to this gateway.
        """
        signature = HexBytes(signature)
        if len(signature) != SIGNATURE_LENGTH:
            raise VoucherRejected(
                f"signature must be {SIGNATURE_LENGTH} bytes, got {len(signature)}"
            )
        try:
            return Account.recover_message(self.signable(voucher), signature=signature)
        except Exception as exc:  # eth-account raises several unrelated error types here
            raise VoucherRejected(f"signature is not recoverable: {exc}") from exc

    def sign(self, voucher: Voucher) -> HexBytes:
        """Co-sign `voucher` as the gateway (the `sigGateway` argument to settle)."""
        if self._account is None:
            raise RelayerNotReady("GATEWAY_PRIVATE_KEY is not set; the gateway cannot co-sign")
        return HexBytes(Account.sign_message(self.signable(voucher), self._account.key).signature)

    # --- checks -------------------------------------------------------------

    def verify_device(self, voucher: Voucher, sig_device: bytes) -> str:
        """Recover and check the device signature. Returns the device address."""
        device = self.recover(voucher, sig_device)
        expected = self.config.device_address
        if expected is not None and device != expected:
            # Don't echo the recovered address back: the caller is unauthenticated,
            # and "not this one" is free reconnaissance.
            raise VoucherRejected("bad device sig: signer is not the channel's device")
        if expected is None:
            log.warning(
                "DEVICE_ADDRESS unset - accepting a voucher from recovered signer %s without "
                "checking that it owns channel %s. Set it, or read the channel's device from "
                "the contract once PaymentChannel is deployed.",
                device,
                voucher.channel,
            )
        return device

    def verify_merchant(self, voucher: Voucher, sig_merchant: bytes) -> str:
        """Recover and check the merchant countersignature (the third signer)."""
        merchant = self.recover(voucher, sig_merchant)
        expected = self.config.merchant_address
        if expected is not None and merchant != expected:
            raise VoucherRejected("bad merchant sig: signer is not the channel's merchant")
        return merchant

    @staticmethod
    def check_deadline(voucher: Voucher, now: float | None = None) -> None:
        """Reject an already-expired voucher; the contract would revert on it anyway."""
        now = time.time() if now is None else now
        if voucher.deadline < now:
            raise VoucherRejected(
                f"expired: deadline {voucher.deadline} is {now - voucher.deadline:.1f}s in the past"
            )

    # --- transaction --------------------------------------------------------

    def build_settle_tx(
        self,
        voucher: Voucher,
        sig_device: bytes,
        sig_gateway: bytes,
        sig_merchant: bytes,
    ) -> dict[str, Any]:
        """Encode `settle(...)` into an unsigned transaction dict.

        `nonce`, `gas` and the fee fields are left out on purpose - they are
        filled in at submit time, when an RPC is actually reachable, so the
        calldata can be built and tested with no chain at all.
        """
        calldata = self._contract.encode_abi(
            "settle",
            args=[
                (
                    voucher.channel,
                    voucher.nonce,
                    voucher.cumulativeAmount,
                    voucher.serviceRef,
                    voucher.deadline,
                ),
                HexBytes(sig_device),
                HexBytes(sig_gateway),
                HexBytes(sig_merchant),
            ],
        )
        tx: dict[str, Any] = {
            "to": to_checksum_address(self.config.payment_channel),
            "data": calldata,
            "value": 0,
            "chainId": self.config.chain_id,
        }
        if self.gateway_address is not None:
            tx["from"] = self.gateway_address
        return tx

    def _fee_fields(self) -> dict[str, int]:
        """Fee cap for the transaction, EIP-1559 where the chain has a base fee.

        `maxFeePerGas` carries 2x headroom over the current base fee: a rise
        between estimate and inclusion would otherwise strand the transaction
        mid-run and put a hole in the dataset. It is a cap, not a price - the
        cost table reads `effectiveGasPrice` off the receipt.
        """
        base_fee = self.w3.eth.get_block("latest").get("baseFeePerGas")
        if base_fee is None:  # pre-1559 chain, or a node that omits it
            return {"gasPrice": self.w3.eth.gas_price}
        try:
            tip = self.w3.eth.max_priority_fee
        except Exception:  # eth_maxPriorityFeePerGas is not universally served
            tip = DEFAULT_PRIORITY_FEE_WEI
        return {"maxPriorityFeePerGas": tip, "maxFeePerGas": base_fee * 2 + tip}

    def _await_confirmations(self, block_number: int, result: RelayResult) -> None:
        """Stamp t5 once the receipt's block is `confirmations` deep.

        The including block is itself the first confirmation, so the default
        `CONFIRMATIONS=1` stamps t5 immediately and t5 - t4 is ~0. A timeout
        leaves t5 None rather than recording a time that means nothing: a
        missing mark is honest, a wrong one poisons the CSV.
        """
        target = block_number + self.config.confirmations - 1
        deadline = time.monotonic() + RECEIPT_TIMEOUT_S
        while self.w3.eth.block_number < target:
            if time.monotonic() > deadline:
                log.warning(
                    "timed out waiting for %d confirmations on block %d; t5 left unset",
                    self.config.confirmations,
                    block_number,
                )
                return
            time.sleep(CONFIRMATION_POLL_S)
        result.t5 = time.time()

    def _submit(self, tx: dict[str, Any], result: RelayResult) -> RelayResult:
        """Broadcast `settle()`, wait for the receipt, stamp t3-t5.

        With `submit` off this returns early: the voucher was accepted and the
        calldata built, nothing was sent. That is not a rejection and not an
        error, so it stays a 200 with `submitted=False`.

        t3 is stamped *after* `send_raw_transaction` returns, matching
        CLAUDE.md's "tx broadcast, hash returned". That puts nonce lookup, gas
        estimation and signing into t3 - t2, and leaves t4 - t3 as inclusion
        time alone, which is the term the L1/L2 comparison actually turns on.
        """
        if not self.config.submit:
            result.submitted = False
            result.reason = "not submitted: GATEWAY_SUBMIT is off (calldata built only)"
            log.info(
                "built settle() calldata for channel=%s nonce=%s (%d bytes); submission is off",
                result.channel,
                result.nonce,
                len(HexBytes(tx["data"])),
            )
            return result

        if self._account is None:
            raise RelayerNotReady("GATEWAY_PRIVATE_KEY is not set; the gateway cannot submit")

        tx = dict(tx)
        tx["from"] = self._account.address
        tx["nonce"] = self.w3.eth.get_transaction_count(self._account.address)
        try:
            # Estimating first turns a would-be revert into a named rejection,
            # instead of a mined failed transaction that still costs gas. The
            # contract remains the authority; this only saves the fee (H2).
            tx["gas"] = int(self.w3.eth.estimate_gas(tx) * GAS_LIMIT_MARGIN)
        except ContractLogicError as exc:
            raise VoucherRejected(f"settle() would revert: {exc}") from exc
        tx.update(self._fee_fields())

        signed = self._account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        result.t3 = time.time()
        result.txHash = HexBytes(tx_hash).to_0x_hex()
        result.submitted = True

        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT_S)
        result.t4 = time.time()

        result.blockNumber = receipt["blockNumber"]
        result.gasUsed = receipt["gasUsed"]
        effective = receipt.get("effectiveGasPrice")
        result.effectiveGasPrice = int(effective) if effective is not None else None
        # OP-stack only (Base): the L1 data-availability fee. Absent elsewhere.
        l1_fee = receipt.get("l1Fee")
        result.l1Fee = int(l1_fee) if l1_fee is not None else None

        if receipt["status"] != 1:
            # estimate_gas passed and it still reverted - state moved underneath
            # us (someone settled a higher voucher first, most likely).
            result.reason = "settle() reverted on-chain despite a clean gas estimate"
            log.error("settle() reverted: tx=%s block=%s", result.txHash, result.blockNumber)
            return result

        log.info(
            "settled channel=%s nonce=%s tx=%s gasUsed=%s",
            result.channel,
            result.nonce,
            result.txHash,
            result.gasUsed,
        )
        self._await_confirmations(receipt["blockNumber"], result)
        return result

    # --- entry point --------------------------------------------------------

    def relay(
        self,
        voucher: Voucher,
        sig_device: bytes,
        sig_merchant: bytes | None = None,
        *,
        t0: float | None = None,
        t1: float | None = None,
        t2: float | None = None,
    ) -> RelayResult:
        """Verify, co-sign, build, and (eventually) submit one voucher.

        Raises `VoucherRejected` if the voucher would revert on-chain, and
        `RelayerNotReady` if the gateway itself is misconfigured - the caller
        maps those to different status codes, because they are different
        people's problems.
        """
        self.check_deadline(voucher)
        device = self.verify_device(voucher, sig_device)
        merchant = self.verify_merchant(voucher, sig_merchant) if sig_merchant else None
        if merchant is None and self.config.submit:
            # settle() is 3-of-3. Sending without the third signature would buy
            # a guaranteed revert - the same H2 property, seen from the gas side.
            raise VoucherRejected("missing merchant signature: settle() is 3-of-3")

        sig_gateway = self.sign(voucher)
        tx = self.build_settle_tx(voucher, sig_device, sig_gateway, sig_merchant or b"")

        result = RelayResult(
            accepted=True,
            submitted=False,
            channel=voucher.channel,
            nonce=voucher.nonce,
            cumulativeAmount=voucher.cumulativeAmount,
            serviceRef=HexBytes(voucher.serviceRef).to_0x_hex(),
            device=device,
            gateway=self.gateway_address,
            merchant=merchant,
            calldata=tx["data"],
            t0=t0,
            t1=t1,
            t2=t2,
        )
        return self._submit(tx, result)

    # --- health -------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Config snapshot plus a cheap RPC probe, for `GET /health`.

        Reports the observed chain id next to the configured one: a mismatch
        silently invalidates every signature, because `chainId` sits inside the
        EIP-712 domain. `contractDeployed` is a live `eth_getCode` probe for the
        same reason - pointing at an address with no code produces a whole CSV
        of plausible-looking failures.
        """
        observed: int | None = None
        deployed: bool | None = None
        rpc_error: str | None = None
        try:
            observed = self.w3.eth.chain_id
            deployed = bool(
                self.w3.eth.get_code(to_checksum_address(self.config.payment_channel))
            )
        except Exception as exc:
            rpc_error = str(exc)

        return {
            "rpcUrl": self.config.rpc_url,
            "rpcReachable": observed is not None,
            "rpcError": rpc_error,
            "chainId": self.config.chain_id,
            "observedChainId": observed,
            # None, not True, when the RPC is down: unknown is not agreement.
            "chainIdMatches": None if observed is None else observed == self.config.chain_id,
            "paymentChannel": self.config.payment_channel,
            # None when the RPC never answered: unknown is not "absent".
            "contractDeployed": deployed,
            "submit": self.config.submit,
            "gateway": self.gateway_address,
            "device": self.config.device_address,
            "merchant": self.config.merchant_address,
            "confirmations": self.config.confirmations,
            "canSign": self._account is not None,
        }
