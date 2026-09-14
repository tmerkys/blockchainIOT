"""Gateway relayer: verify a device voucher, build the `settle()` transaction.

Import path: run from the repo root so `shared/` and `gateway/` resolve as
namespace packages (`uvicorn gateway.api:app`).

Instrumentation (CLAUDE.md, "Instrumentation"): this module owns marks t2-t5.
`t0`/`t1` are the Pi's clock and ride along in the request body untouched, so
the experiment runner can write one CSV row per settlement attempt and still
decompose signing vs transport vs chain cost.

**Submission is deliberately not wired up.** `PaymentChannel.sol` does not
exist yet, so there is nothing to send the transaction to: `relay()` stops
after the calldata is built and returns `submitted=False`. `_submit()` holds
the exact shape of what replaces that at M1/M3, including the receipt fields
the cost table needs (`gasUsed`, `effectiveGasPrice`, and `l1Fee` on Base).
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

from shared.voucher import Voucher

log = logging.getLogger(__name__)

ZERO_ADDRESS = "0x" + "00" * 20
SIGNATURE_LENGTH = 65  # r + s + v
RPC_TIMEOUT_S = 2.0

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

    def _submit(self, tx: dict[str, Any], result: RelayResult) -> RelayResult:
        """Placeholder for broadcast -> receipt -> confirmations.

        Not implemented: `PaymentChannel.sol` does not exist, so `tx["to"]` is
        the zero address and there is no `settle` to call. When M1 lands, this
        becomes, in order::

            tx["nonce"] = self.w3.eth.get_transaction_count(self.gateway_address)
            tx["gas"]   = self.w3.eth.estimate_gas(tx)
            tx |= fee fields (maxFeePerGas / maxPriorityFeePerGas)
            signed  = self._account.sign_transaction(tx)
            result.t3 = time.time()
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
            result.t4 = time.time()
            ... wait for self.config.confirmations blocks ...
            result.t5 = time.time()

        The receipt also carries the cost table's inputs - `gasUsed`,
        `effectiveGasPrice`, and on OP-stack chains `l1Fee`, without which
        Base's real cost is understated. Widen `RelayResult` with those fields
        at the same time.
        """
        result.submitted = False
        result.reason = "not submitted: PaymentChannel is not deployed yet (M1)"
        log.info(
            "built settle() calldata for channel=%s nonce=%s (%d bytes); submission is stubbed",
            result.channel,
            result.nonce,
            len(HexBytes(tx["data"])),
        )
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
        EIP-712 domain.
        """
        observed: int | None = None
        rpc_error: str | None = None
        try:
            observed = self.w3.eth.chain_id
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
            "contractDeployed": False,  # M1 - nothing to submit to yet
            "gateway": self.gateway_address,
            "device": self.config.device_address,
            "merchant": self.config.merchant_address,
            "confirmations": self.config.confirmations,
            "canSign": self._account is not None,
        }
