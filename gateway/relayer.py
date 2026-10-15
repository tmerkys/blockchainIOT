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
import threading
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
from web3.exceptions import ContractLogicError, TimeExhausted

from shared.rpc import redact, rpc_host
from shared.voucher import Voucher

log = logging.getLogger(__name__)

ZERO_ADDRESS = "0x" + "00" * 20
SIGNATURE_LENGTH = 65  # r + s + v
RPC_TIMEOUT_S = 10.0
"""Per-request HTTP timeout to the RPC endpoint. 2 s was fine against a local
anvil on loopback and is not fine against a public endpoint like Base Sepolia,
where a single `eth_estimateGas` crossing the internet under load can exceed it
— and a timeout mid-run surfaces as a failed settlement, not a slow one.
Override with `RPC_TIMEOUT_S` for a particularly slow provider."""

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
    },
    {
        "type": "function",
        "name": "channels",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [
            {"name": "device", "type": "address"},
            {"name": "gateway", "type": "address"},
            {"name": "merchant", "type": "address"},
            {"name": "funder", "type": "address"},
            {"name": "deposit", "type": "uint256"},
            {"name": "paid", "type": "uint256"},
            {"name": "nonce", "type": "uint256"},
            {"name": "closed", "type": "bool"},
        ],
    },
]

# `close()` takes the same arguments as `settle()`; only the selector differs.
SETTLE_ABI.append({**SETTLE_ABI[0], "name": "close"})


def receipt_int(value: Any) -> int | None:
    """Coerce a receipt field to int, whatever shape the node sent it in.

    web3 normalises the receipt fields it knows about, but OP-stack extras like
    `l1Fee` are not in its schema and arrive as raw hex strings ("0x0"). A bare
    `int()` on those raises, and it would raise *after* the transaction was
    already mined — turning a successful settlement into a crashed run and a
    missing row. Returns None for a genuinely absent field, which must stay
    distinct from a zero fee.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(value, "big")
    text = str(value).strip()
    return int(text, 16) if text.lower().startswith("0x") else int(text)


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
    rpc_timeout: float = RPC_TIMEOUT_S

    # --- exp/rpc: per-row receipt definition check -------------------------
    record_block_timestamp: bool = True
    """After t5, read the receipt block's timestamp (one `eth_getBlockByNumber`).

    Outside the measured span: it runs after t5, and the device's reported
    latency subtracts everything between t4 and t6. Lets the analysis tell a
    Flashblocks pre-confirmation (receipt before the block's timestamp) from a
    sealed-block receipt, per row (docs/measurements.md, 2026-10-15
    correction). Env `RECORD_BLOCK_TIMESTAMP=0` turns it off.
    """
    # --- end exp/rpc ------------------------------------------------------

    submit: bool = False
    """Broadcast `settle()`, or stop once the calldata is built?

    Off by default so that importing or constructing a Relayer can never spend
    gas on its own. `scripts/e2e_m1.py` and the experiment runner set
    `GATEWAY_SUBMIT=1`; the unit tests leave it off and stay chain-free.
    """

    # --- concurrency experiment (exp/concurrency); inert unless enabled ----
    pipeline: bool = False
    """Pipelining mode (env `GATEWAY_PIPELINE`), off by default. When on, the
    gateway allocates transaction nonces from a local counter so several
    vouchers can be in flight at once, and the API runs `relay()` in a thread
    pool. See `NonceManager` for the nonce-gap policy. When off, nothing in the
    default path changes."""

    receipt_timeout: float = RECEIPT_TIMEOUT_S
    """Receipt wait in pipelining mode only (env `GATEWAY_RECEIPT_TIMEOUT_S`);
    the default path keeps the module constant."""

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

        def number(name: str, default: float) -> float:
            raw = env.get(name, "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be a number, got {raw!r}") from exc

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
            rpc_timeout=number("RPC_TIMEOUT_S", defaults.rpc_timeout),
            submit=boolean("GATEWAY_SUBMIT", defaults.submit),
            record_block_timestamp=boolean(
                "RECORD_BLOCK_TIMESTAMP", defaults.record_block_timestamp
            ),
            pipeline=boolean("GATEWAY_PIPELINE", defaults.pipeline),
            receipt_timeout=number("GATEWAY_RECEIPT_TIMEOUT_S", defaults.receipt_timeout),
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
    closed: bool = False
    """True when this transaction was `close()` rather than `settle()`."""

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
    l1GasUsed: int | None = None
    l1GasPrice: int | None = None

    # Instrumentation marks. t0/t1 are the device's clock, t2-t5 the gateway's;
    # read the clock-skew warning in CLAUDE.md before subtracting across that line.
    t0: float | None = None  # Pi: sensor event fires
    t1: float | None = None  # Pi: voucher signed
    t2: float | None = None  # gateway: voucher received over transport
    t3: float | None = None  # gateway: tx broadcast, hash returned
    t4: float | None = None  # gateway: receipt received (included in block)
    t5: float | None = None  # gateway: N-confirmation depth reached

    # Not one of CLAUDE.md's marks. Stamped by the API handler just before the
    # response goes back, purely so the device can pair (t2, t6) with its own
    # (send, receive) instants and estimate the clock offset between the two
    # machines - the skew sanity check CLAUDE.md asks for from M2 onward.
    # Nothing is ever computed by subtracting it from a device-side mark.
    t6: float | None = None  # gateway: response handed back to the transport

    # --- exp/rpc: which RPC answered, and which receipt definition it used ---
    # Host only, never the URL: a dedicated endpoint's URL carries its API key.
    rpcHost: str | None = None
    # Timestamp (chain clock, integer seconds) of the block the receipt names,
    # read after t5. A receipt held before this instant is a pre-confirmation.
    blockTimestamp: int | None = None
    # First `eth_blockNumber` the gateway saw after the receipt (the first t5
    # poll). Below `blockNumber` means the receipt arrived before its block was
    # the node's head — a pre-confirmation, decided without any clock.
    headAtReceipt: int | None = None
    # --- end exp/rpc ------------------------------------------------------


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
            Web3.HTTPProvider(
                self.config.rpc_url, request_kwargs={"timeout": self.config.rpc_timeout}
            )
        )
        self._contract = self.w3.eth.contract(
            address=to_checksum_address(self.config.payment_channel), abi=SETTLE_ABI
        )
        self._account = (
            Account.from_key(self.config.gateway_private_key)
            if self.config.gateway_private_key
            else None
        )
        # Pipelining mode only (exp/concurrency): created lazily on first use.
        self._nonces: NonceManager | None = None
        self._channel_devices: dict[str, str] = {}
        self._pipeline_lock = threading.Lock()

    @property
    def rpc_host(self) -> str | None:
        """Host of the configured RPC endpoint — the only part of it ever shown."""
        return rpc_host(self.config.rpc_url)

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
        *,
        close: bool = False,
    ) -> dict[str, Any]:
        """Encode `settle(...)` — or `close(...)` — into an unsigned transaction.

        `close` takes the identical arguments and the identical 3-of-3 checks;
        it additionally returns the unspent deposit to the funder and marks the
        channel finished. It is what M4 submits after 200 ticks have
        accumulated off-chain.

        `nonce`, `gas` and the fee fields are left out on purpose - they are
        filled in at submit time, when an RPC is actually reachable, so the
        calldata can be built and tested with no chain at all.
        """
        calldata = self._contract.encode_abi(
            "close" if close else "settle",
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
        while (head := self.w3.eth.block_number) < target:
            if result.headAtReceipt is None:
                result.headAtReceipt = head  # exp/rpc: first poll only
            if time.monotonic() > deadline:
                log.warning(
                    "timed out waiting for %d confirmations on block %d; t5 left unset",
                    self.config.confirmations,
                    block_number,
                )
                return
            time.sleep(CONFIRMATION_POLL_S)
        result.t5 = time.time()
        if result.headAtReceipt is None:
            result.headAtReceipt = head  # exp/rpc: head was already there at t4

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

        result.blockNumber = receipt_int(receipt["blockNumber"])
        result.gasUsed = receipt_int(receipt["gasUsed"])
        result.effectiveGasPrice = receipt_int(receipt.get("effectiveGasPrice"))
        # OP-stack only (Base): the L1 data-availability fee, and the L1 gas and
        # price it is computed from. On Base this is most of the real cost — the
        # understatement CLAUDE.md warns about — and it arrives as a hex string.
        result.l1Fee = receipt_int(receipt.get("l1Fee"))
        result.l1GasUsed = receipt_int(receipt.get("l1GasUsed"))
        result.l1GasPrice = receipt_int(receipt.get("l1GasPrice"))

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
        self._record_block_timestamp(result)  # exp/rpc: after t5, outside the span
        return result

    # --- exp/rpc: receipt definition check, after the measured span ---------

    def _record_block_timestamp(self, result: RelayResult) -> None:
        """Read the receipt block's timestamp into `result`, best effort.

        A failure leaves it None and costs the row nothing else: the
        settlement is already sealed and its marks are already stamped.
        """
        if not self.config.record_block_timestamp or result.blockNumber is None:
            return
        try:
            block = self.w3.eth.get_block(result.blockNumber)
            result.blockTimestamp = receipt_int(block["timestamp"])
        except Exception as exc:  # noqa: BLE001 — diagnostic only
            log.warning(
                "could not read timestamp of block %s: %s",
                result.blockNumber,
                redact(str(exc), self.config.rpc_url),
            )

    # --- end exp/rpc --------------------------------------------------------

    # --- entry point --------------------------------------------------------

    def relay(
        self,
        voucher: Voucher,
        sig_device: bytes,
        sig_merchant: bytes | None = None,
        *,
        close: bool = False,
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
        if self.config.pipeline:  # exp/concurrency; off by default
            return self._relay_pipelined(
                voucher, sig_device, sig_merchant, close=close, t0=t0, t1=t1, t2=t2
            )
        self.check_deadline(voucher)
        device = self.verify_device(voucher, sig_device)
        merchant = self.verify_merchant(voucher, sig_merchant) if sig_merchant else None
        if merchant is None and self.config.submit:
            # settle() is 3-of-3. Sending without the third signature would buy
            # a guaranteed revert - the same H2 property, seen from the gas side.
            raise VoucherRejected("missing merchant signature: settle() is 3-of-3")

        sig_gateway = self.sign(voucher)
        tx = self.build_settle_tx(
            voucher, sig_device, sig_gateway, sig_merchant or b"", close=close
        )

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
            closed=close,
            calldata=tx["data"],
            t0=t0,
            t1=t1,
            t2=t2,
            rpcHost=self.rpc_host,  # exp/rpc
        )
        return self._submit(tx, result)

    # --- pipelining (exp/concurrency; GATEWAY_PIPELINE=1, off by default) ----
    #
    # Everything in this block runs only when `config.pipeline` is on. The
    # default path above (`relay` -> `_submit`) is untouched: one fresh
    # `get_transaction_count` per voucher, serialised by the event loop.

    def _nonce_manager(self) -> NonceManager:
        """The process-wide nonce counter, created on first use."""
        with self._pipeline_lock:
            if self._nonces is None:
                if self._account is None:
                    raise RelayerNotReady("GATEWAY_PRIVATE_KEY is not set; the gateway cannot submit")
                address = self._account.address
                self._nonces = NonceManager(
                    lambda: self.w3.eth.get_transaction_count(address, "pending")
                )
            return self._nonces

    def _channel_device(self, channel: str) -> str:
        """The `device` signer the contract registered for `channel`, cached.

        With K load-test devices there is no single `DEVICE_ADDRESS`. Instead
        of accepting any recovered signer, the pipelined path binds the
        signature to the channel's own device, read once from the contract
        (it is immutable after `open()`), so the check stays as strict as the
        single-device one and costs one RPC per channel, not per voucher.
        """
        key = to_checksum_address(channel)
        with self._pipeline_lock:
            cached = self._channel_devices.get(key)
        if cached is not None:
            return cached
        state = self.channel_state(key)
        if not state["exists"]:
            raise VoucherRejected("unknown channel")
        with self._pipeline_lock:
            self._channel_devices[key] = state["device"]
        return state["device"]

    def _relay_pipelined(
        self,
        voucher: Voucher,
        sig_device: bytes,
        sig_merchant: bytes | None,
        *,
        close: bool,
        t0: float | None,
        t1: float | None,
        t2: float | None,
    ) -> PipelinedResult:
        """`relay()` for pipelining mode: same checks, local nonce allocation."""
        self.check_deadline(voucher)
        if self.config.device_address is not None:
            device = self.verify_device(voucher, sig_device)
        else:
            device = self.recover(voucher, sig_device)
            if device != self._channel_device(voucher.channel):
                raise VoucherRejected("bad device sig: signer is not the channel's device")
        merchant = self.verify_merchant(voucher, sig_merchant) if sig_merchant else None
        if merchant is None and self.config.submit:
            raise VoucherRejected("missing merchant signature: settle() is 3-of-3")

        sig_gateway = self.sign(voucher)
        tx = self.build_settle_tx(
            voucher, sig_device, sig_gateway, sig_merchant or b"", close=close
        )
        result = PipelinedResult(
            accepted=True,
            submitted=False,
            channel=voucher.channel,
            nonce=voucher.nonce,
            cumulativeAmount=voucher.cumulativeAmount,
            serviceRef=HexBytes(voucher.serviceRef).to_0x_hex(),
            device=device,
            gateway=self.gateway_address,
            merchant=merchant,
            closed=close,
            calldata=tx["data"],
            t0=t0,
            t1=t1,
            t2=t2,
            rpcHost=self.rpc_host,  # exp/rpc
        )
        return self._submit_pipelined(tx, result)

    def _fail(self, result: PipelinedResult, failure: str, detail: str) -> PipelinedResult:
        result.failure = failure
        # RPC exceptions can quote the endpoint URL, and a keyed URL carries
        # its API key in the path (exp/rpc): host only, in rows and in logs.
        result.reason = f"{failure}: {redact(detail, self.config.rpc_url)}"
        log.error(
            "pipelined relay failed: channel=%s nonce=%s %s",
            result.channel, result.nonce, result.reason,
        )
        return result

    def _submit_pipelined(self, tx: dict[str, Any], result: PipelinedResult) -> PipelinedResult:
        """Broadcast with a locally allocated nonce; never retries a voucher.

        Order matters for the nonce-gap problem: gas estimation and the fee
        lookup run *before* a nonce is allocated, so a voucher that would
        revert, or an RPC hiccup at that stage, never consumes one. Only the
        broadcast and the receipt wait can fail while holding a nonce, and
        each such failure goes to `NonceManager.fail()`, which resyncs and
        counts it (see its docstring for the policy).
        """
        if not self.config.submit:
            return self._submit(tx, result)  # calldata-only: identical to default
        if self._account is None:
            raise RelayerNotReady("GATEWAY_PRIVATE_KEY is not set; the gateway cannot submit")

        tx = dict(tx)
        tx["from"] = self._account.address
        try:
            # No nonce in the estimate on purpose: with others in flight the
            # local nonce is ahead of the chain's, and the estimate runs on
            # the latest state either way.
            tx["gas"] = int(self.w3.eth.estimate_gas(tx) * GAS_LIMIT_MARGIN)
        except ContractLogicError as exc:
            raise VoucherRejected(f"settle() would revert: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - a row, not a 500
            return self._fail(result, FAIL_RPC, f"estimate_gas: {exc}")
        try:
            tx.update(self._fee_fields())
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, FAIL_RPC, f"fee lookup: {exc}")

        try:
            nonces = self._nonce_manager()
        except RelayerNotReady:
            raise
        except Exception as exc:  # noqa: BLE001 - the initial pending-nonce read failed
            return self._fail(result, FAIL_RPC, f"initial nonce read: {exc}")

        nonce, epoch = nonces.allocate()
        tx["nonce"] = nonce
        result.txNonce = nonce
        result.nonceEpoch = epoch
        signed = self._account.sign_transaction(tx)
        try:
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as exc:  # noqa: BLE001
            failure = classify_send_error(exc)
            nonces.fail(nonce, failure)
            result.gapBehind = nonces.release(nonce)
            result.nonceResyncs = nonces.resyncs
            return self._fail(result, failure, f"broadcast of nonce {nonce}: {exc}")
        result.t3 = time.time()
        result.txHash = HexBytes(tx_hash).to_0x_hex()
        result.submitted = True
        result.inflight = nonces.broadcast()

        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=self.config.receipt_timeout
            )
        except Exception as exc:  # noqa: BLE001
            failure = FAIL_NOT_MINED if isinstance(exc, TimeExhausted) else FAIL_RECEIPT
            nonces.landed()
            nonces.fail(nonce, failure)
            result.gapBehind = nonces.release(nonce)
            result.nonceResyncs = nonces.resyncs
            return self._fail(result, failure, f"tx {result.txHash} nonce {nonce}: {exc}")
        result.t4 = time.time()
        nonces.landed()
        result.gapBehind = nonces.release(nonce)
        result.nonceResyncs = nonces.resyncs

        result.blockNumber = receipt_int(receipt["blockNumber"])
        result.gasUsed = receipt_int(receipt["gasUsed"])
        result.effectiveGasPrice = receipt_int(receipt.get("effectiveGasPrice"))
        result.l1Fee = receipt_int(receipt.get("l1Fee"))
        result.l1GasUsed = receipt_int(receipt.get("l1GasUsed"))
        result.l1GasPrice = receipt_int(receipt.get("l1GasPrice"))

        if receipt["status"] != 1:
            # Same 200 + reason as the default path; `failure` names it too.
            result.reason = "settle() reverted on-chain despite a clean gas estimate"
            result.failure = FAIL_REVERTED
            log.error("settle() reverted: tx=%s block=%s", result.txHash, result.blockNumber)
            return result

        self._await_confirmations(receipt["blockNumber"], result)
        self._record_block_timestamp(result)  # exp/rpc: after t5, outside the span
        return result

    # --- channel state ------------------------------------------------------

    def channel_state(self, channel: str) -> dict[str, Any]:
        """Read one channel's on-chain state, for the device to resume from.

        The device holds no RPC connection — that is the architecture, not an
        oversight — so it cannot see how much of its channel has already been
        settled. Without this it restarts every session believing nothing has
        been paid, and the second voucher it ever sends is non-monotonic and
        rejected. That is invisible on a channel settled once and fatal to
        M5's N>=30 runs, which settle the same channel repeatedly.

        The contract stays the single source of truth for `paid` and `nonce`;
        this only relays what it says.
        """
        address = to_checksum_address(channel)
        try:
            (
                device,
                gateway,
                merchant,
                funder,
                deposit,
                paid,
                nonce,
                closed,
            ) = self._contract.functions.channels(address).call()
        except Exception as exc:
            raise RelayerNotReady(
                f"could not read channel {address}: {redact(str(exc), self.config.rpc_url)}"
            ) from exc

        return {
            "channel": address,
            # The zero device address is how the contract spells "no such
            # channel"; `settle()` rejects it with "unknown channel".
            "exists": device != ZERO_ADDRESS,
            "device": device,
            "gateway": gateway,
            "merchant": merchant,
            "funder": funder,
            "deposit": deposit,
            "paid": paid,
            "nonce": nonce,
            "closed": closed,
            # What the device may still accumulate against. A voucher above this
            # reverts as "exceeds deposit", and the device cannot work that out
            # for itself without an RPC.
            "remaining": deposit - paid,
        }

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
            rpc_error = redact(str(exc), self.config.rpc_url)

        return {
            # Host only (exp/rpc): a dedicated endpoint's URL is a credential.
            "rpcHost": self.rpc_host,
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
            # Only present in pipelining mode, so the default /health is unchanged.
            **(
                {
                    "pipeline": True,
                    "nonceManager": self._nonces.snapshot() if self._nonces else None,
                }
                if self.config.pipeline
                else {}
            ),
        }


# =============================================================================
# Pipelining mode (exp/concurrency). Inert unless GATEWAY_PIPELINE=1.
# =============================================================================

# Row statuses for failures that only exist once several transactions share
# one sender. Each is a distinct value in the `failure` field, never folded
# into a generic error, and no voucher is ever retried behind the device's back.
FAIL_NONCE_TOO_LOW = "nonce_too_low"  # node: nonce already used (or our counter is behind)
FAIL_REPLACEMENT_UNDERPRICED = "replacement_underpriced"  # same nonce already pending
FAIL_ALREADY_KNOWN = "already_known"  # identical tx already in the pool
FAIL_NONCE_TOO_HIGH = "nonce_too_high"  # node refuses a gapped nonce
FAIL_INSUFFICIENT_FUNDS = "insufficient_funds"
FAIL_UNDERPRICED = "underpriced"  # fee cap below base fee / pool minimum
FAIL_SEND_TIMEOUT = "send_timeout"  # broadcast timed out: ambiguous, may have landed
FAIL_SEND = "send_error"  # any other broadcast error
FAIL_NOT_MINED = "not_mined"  # broadcast ok, no receipt within the timeout (dropped/stuck)
FAIL_RECEIPT = "receipt_error"  # RPC error while waiting for the receipt
FAIL_RPC = "rpc_error"  # estimate/fee/nonce read failed before a nonce was held
FAIL_REVERTED = "reverted"  # mined, status 0 (also has the default path's reason)

NONCE_OCCUPIED = frozenset({FAIL_NONCE_TOO_LOW, FAIL_REPLACEMENT_UNDERPRICED, FAIL_ALREADY_KNOWN})
"""Broadcast failures that prove the nonce is already used: no gap is left."""


def classify_send_error(exc: BaseException) -> str:
    """Map a `send_raw_transaction` error onto one of the FAIL_* statuses.

    Matches on the node's message text, which is the only thing geth, reth,
    anvil and op-geth agree on (codes differ). Unknown messages fall through
    to `send_error`, with the full text kept in `reason`.
    """
    msg = str(exc).lower()
    if "nonce too low" in msg or "nonce is too low" in msg or "old nonce" in msg:
        return FAIL_NONCE_TOO_LOW
    if "replacement" in msg and ("underpriced" in msg or "fee too low" in msg):
        return FAIL_REPLACEMENT_UNDERPRICED
    if "already known" in msg or "already imported" in msg or "known transaction" in msg:
        return FAIL_ALREADY_KNOWN
    if "nonce too high" in msg or "nonce gap" in msg:
        return FAIL_NONCE_TOO_HIGH
    if "insufficient funds" in msg:
        return FAIL_INSUFFICIENT_FUNDS
    if "underpriced" in msg or "fee cap" in msg or "less than block base fee" in msg:
        return FAIL_UNDERPRICED
    if "timed out" in msg or "timeout" in msg or type(exc).__name__ in ("ReadTimeout", "Timeout"):
        return FAIL_SEND_TIMEOUT
    return FAIL_SEND


class NonceManager:
    """Thread-safe local nonce counter for one sending key.

    Initialised from `get_transaction_count(addr, "pending")` and incremented
    per allocation, so N vouchers can be in flight without each waiting for
    the previous receipt.

    **The nonce-gap problem, and the policy.** If the transaction holding
    nonce n fails to broadcast (or is broadcast and never mined), every
    transaction already sent with a nonce above n sits in the node's queue
    and cannot be mined until n is used. The policy is:

    1. *Never retry the failed voucher.* Its row records the failure status.
    2. *Resync immediately.* The counter is re-read from the node's pending
       nonce, which stops at the first gap, so the *next* voucher fills n and
       the queued ones behind it become minable. Nonces still held by
       in-flight transactions are skipped, so a resync can never hand out a
       nonce that another thread is already using.
    3. *Record it.* Every resync is counted (`resyncs`, carried on each row
       as `nonceResyncs`) and logged in `events`; every nonce that was in
       flight above the failed one at the time is marked, and its row carries
       `gapBehind = n`. Those rows' latencies include waiting for the gap to
       be filled and must be reported separately, not silently pooled.

    A resync can be wrong on a load-balanced public RPC whose nodes disagree
    about the pending nonce; that shows up as `nonce_too_low` on the next
    voucher, which triggers another (recorded) resync rather than a retry.
    """

    MAX_EVENTS = 200

    def __init__(self, fetch_pending: Any) -> None:
        self._fetch = fetch_pending
        self._lock = threading.Lock()
        self._next = int(fetch_pending())
        self.initial = self._next
        self.epoch = 0
        self.resyncs = 0
        self.allocated = 0
        self._held: set[int] = set()
        self._inflight = 0
        self._affected: dict[int, int] = {}
        self.events: list[dict[str, Any]] = []

    def allocate(self) -> tuple[int, int]:
        """Next free nonce and the epoch it was allocated in."""
        with self._lock:
            while self._next in self._held:
                self._next += 1
            nonce = self._next
            self._next += 1
            self._held.add(nonce)
            self.allocated += 1
            return nonce, self.epoch

    def broadcast(self) -> int:
        """Count a transaction as in flight; returns how many are now."""
        with self._lock:
            self._inflight += 1
            return self._inflight

    def landed(self) -> None:
        """A broadcast transaction is no longer awaited (receipt or timeout)."""
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    def fail(self, nonce: int, failure: str) -> None:
        """Nonce `nonce` was not (or may not have been) used: resync, record."""
        with self._lock:
            # A nonce the node says is taken leaves no gap behind it; any other
            # failure may have left `nonce` unused, stalling everything above.
            occupied = failure in NONCE_OCCUPIED
            behind = [] if occupied else sorted(n for n in self._held if n > nonce)
            for n in behind:
                self._affected.setdefault(n, nonce)
            self._held.discard(nonce)
            before = self._next
            error: str | None = None
            try:
                self._next = int(self._fetch())
            except Exception as exc:  # noqa: BLE001 - keep the counter, record why
                error = str(exc)
                self._next = min(before, nonce)
            self.epoch += 1
            self.resyncs += 1
            event = {
                "time": time.time(),
                "failure": failure,
                "nonce": nonce,
                "counterBefore": before,
                "counterAfter": self._next,
                "nonceOccupied": occupied,
                "inFlightBehind": behind,
                "resyncError": error,
            }
            self.events.append(event)
            del self.events[: -self.MAX_EVENTS]
        log.warning("nonce resync after %s on nonce %d: %s", failure, nonce, event)

    def release(self, nonce: int) -> int | None:
        """Drop `nonce` from the held set; return the gap it sat behind, if any."""
        with self._lock:
            self._held.discard(nonce)
            return self._affected.pop(nonce, None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "initial": self.initial,
                "next": self._next,
                "allocated": self.allocated,
                "held": sorted(self._held),
                "inflight": self._inflight,
                "epoch": self.epoch,
                "resyncs": self.resyncs,
                "events": list(self.events[-20:]),
            }


class PipelinedResult(RelayResult):
    """`RelayResult` plus the fields only pipelining mode produces.

    A subclass, returned only when `GATEWAY_PIPELINE` is on, so the default
    response shape stays exactly what it was.
    """

    failure: str | None = None
    """One of the FAIL_* statuses, or None for a clean settlement."""
    txNonce: int | None = None
    """Gateway account nonce this voucher's transaction used."""
    nonceEpoch: int | None = None
    """Resync epoch the nonce was allocated in (0 until the first resync)."""
    inflight: int | None = None
    """Transactions in flight (incl. this one) right after this broadcast."""
    gapBehind: int | None = None
    """Set when a lower nonce failed while this one was in flight: that nonce."""
    nonceResyncs: int | None = None
    """Total nonce resyncs this gateway process had done when this row closed."""
