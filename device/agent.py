"""Device payment agent: sensor event -> Voucher -> signature -> transport.

The device leg of the M1/M2 flow. Runs on the Raspberry Pi, holds the device
key, and never touches chain state: it signs a voucher and hands it off. The
gateway pays the gas, and — per H2 — the gateway's signature alone will not
move funds, so this signature is what authorises payment at all.

Marks stamped here: `t0` (in sensor.py, when the event fires) and `t1` (when
the voucher is signed). Everything downstream is the gateway's to record.

Transmission is a placeholder: `Transport` is the seam that
`device/transport/http_client.py` (M1/M2) and `ble_client.py` (M5) plug into.
`LoggingTransport` is the stand-in that keeps the flow runnable today.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Protocol

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount

# `shared/` and `device/` are plain directories, not installed packages — the
# same flat-import convention shared/test_voucher.py already relies on. It also
# survives deployment to the Pi, where only device/ and shared/ are copied over
# and there is no repo root to speak of.
_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent / "shared"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sensor import MWH_PER_KWH, KwhMeter, SensorEvent  # noqa: E402
from voucher import Voucher  # noqa: E402

DEFAULT_DEADLINE_SECONDS = 300
"""How long a voucher stays settleable: long enough to absorb the slowest
condition in the M5 matrix, short enough that a lost voucher expires."""

DEFAULT_PRICE_WEI_PER_KWH = 140_000_000_000_000
"""0.35 EUR/kWh at a nominal 2500 EUR/ETH. A unit for the demo, not a claim —
the thesis reports gas, and EUR only against one pinned price on one date."""


class AgentConfig:
    """Channel parameters the agent needs in order to build a voucher.

    Read from the environment for now; this moves into `shared/config.py` once
    the gateway has to read the same values.
    """

    def __init__(
        self,
        *,
        channel: str,
        verifying_contract: str,
        chain_id: int,
        price_wei_per_kwh: int = DEFAULT_PRICE_WEI_PER_KWH,
        deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
    ) -> None:
        if price_wei_per_kwh <= 0:
            raise ValueError("price_wei_per_kwh must be positive")
        if deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive")

        self.channel = channel
        self.verifying_contract = verifying_contract
        self.chain_id = chain_id
        self.price_wei_per_kwh = price_wei_per_kwh
        self.deadline_seconds = deadline_seconds

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AgentConfig:
        e = os.environ if env is None else env
        required = ("CHANNEL_ADDRESS", "VERIFYING_CONTRACT", "CHAIN_ID")
        missing = [k for k in required if not e.get(k)]
        if missing:
            raise KeyError(f"missing required env vars: {', '.join(missing)}")
        return cls(
            channel=e["CHANNEL_ADDRESS"],
            verifying_contract=e["VERIFYING_CONTRACT"],
            chain_id=int(e["CHAIN_ID"]),
            price_wei_per_kwh=int(e.get("PRICE_WEI_PER_KWH", DEFAULT_PRICE_WEI_PER_KWH)),
            deadline_seconds=int(e.get("VOUCHER_DEADLINE_SECONDS", DEFAULT_DEADLINE_SECONDS)),
        )


class SignedVoucher:
    """A voucher, the device signature over it, and the marks taken so far.

    `to_wire()` is what goes to the gateway. It carries the service fact in the
    clear next to the voucher so the gateway and the merchant can recompute
    `serviceRef` and the amount instead of trusting them.
    """

    def __init__(
        self,
        *,
        voucher: Voucher,
        signature: bytes,
        device_address: str,
        chain_id: int,
        verifying_contract: str,
        event: SensorEvent,
        t1_ns: int,
        t1_mono_ns: int,
    ) -> None:
        self.voucher = voucher
        self.signature = signature
        self.device_address = device_address
        self.chain_id = chain_id
        self.verifying_contract = verifying_contract
        self.event = event
        self.t1_ns = t1_ns
        self.t1_mono_ns = t1_mono_ns

    @property
    def signing_ns(self) -> int:
        """t1 - t0 on the monotonic clock: the device-side signing cost, which
        is the term ch. 4 decomposes out of the end-to-end latency."""
        return self.t1_mono_ns - self.event.t0_mono_ns

    def to_wire(self) -> dict[str, Any]:
        return {
            "voucher": {
                "channel": self.voucher.channel,
                "nonce": self.voucher.nonce,
                # uint256 as a decimal string: JSON numbers are IEEE 754 doubles
                # in most parsers and wei amounts do not survive that.
                "cumulativeAmount": str(self.voucher.cumulativeAmount),
                "serviceRef": "0x" + self.voucher.serviceRef.hex(),
                "deadline": self.voucher.deadline,
            },
            "sigDevice": "0x" + self.signature.hex(),
            "deviceAddress": self.device_address,
            "chainId": self.chain_id,
            "verifyingContract": self.verifying_contract,
            "service": {
                "sessionId": self.event.session_id,
                "sequence": self.event.sequence,
                "energyMwh": self.event.energy_mwh,
                "cumulativeMwh": self.event.cumulative_mwh,
                "timestamp": self.event.timestamp,
            },
            "marks": {"t0_ns": self.event.t0_ns, "t1_ns": self.t1_ns},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_wire(), separators=(",", ":"))


class Transport(Protocol):
    """Device -> gateway link. Implemented by device/transport/*.py."""

    def send(self, signed: SignedVoucher) -> Any: ...


class LoggingTransport:
    """Placeholder transport: prints the wire payload, sends nothing.

    Keeps the agent runnable and the M1 flow demonstrable before
    `transport/http_client.py` exists. Replace it, do not build on it.
    """

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def send(self, signed: SignedVoucher) -> dict[str, Any]:
        print(signed.to_json(), file=self._stream, flush=True)
        return {"transport": "logging", "delivered": False}


class DeviceAgent:
    """Turns metered energy into signed vouchers for one channel.

    Voucher state lives here and nowhere else: `cumulativeAmount` is monotonic
    and the nonce strictly increases, which is exactly what lets 200 charging
    ticks settle as a single transaction (M4).
    """

    def __init__(
        self,
        *,
        account: LocalAccount,
        config: AgentConfig,
        transport: Transport | None = None,
    ) -> None:
        self.account = account
        self.config = config
        self.transport: Transport = transport if transport is not None else LoggingTransport()
        self.nonce = 0
        self.cumulative_amount = 0
        self.settled_baseline = 0
        """Wei already released on this channel before this agent started.

        `cumulativeAmount` is cumulative over the *channel*, not the session,
        so an agent that restarts against a channel which has already settled
        must price from where the chain left off. Left at 0 the next voucher
        repeats an amount the contract has already paid and reverts as "not
        monotonic" — see `resume_from()`."""

    @property
    def address(self) -> str:
        return self.account.address

    def price(self, cumulative_mwh: int) -> int:
        """Amount owed, in wei, for `cumulative_mwh` of delivered energy.

        Integer floor division: the rounding loss is under 1 wei and always in
        the payer's favour, and it can never push the cumulative total
        backwards, which the contract would reject.
        """
        return cumulative_mwh * self.config.price_wei_per_kwh // MWH_PER_KWH

    def resume_from(self, *, paid: int, nonce: int) -> None:
        """Adopt a channel's settled state as this agent's starting point.

        Takes the contract's `paid` and `nonce`, normally relayed by the
        gateway's `GET /channel/{id}` — the device has no RPC of its own. The
        contract remains the authority; this only stops the device from
        proposing a voucher it already knows would revert.

        Without it, every restart begins at zero and the first voucher of the
        second session is worth less than what the channel has already paid.
        On a channel settled once that looks like a puzzling rejection; across
        M5's N>=30 repeats it is every run after the first.
        """
        if paid < 0 or nonce < 0:
            raise ValueError("paid and nonce cannot be negative")
        self.settled_baseline = paid
        self.cumulative_amount = paid
        self.nonce = nonce

    def build_voucher(self, event: SensorEvent, *, now: int | None = None) -> Voucher:
        """Build the next voucher for this channel from a service event.

        The amount is what the channel had already paid plus the value of the
        energy this session has metered, keeping `cumulativeAmount` monotonic
        across restarts as well as within a session.
        """
        amount = self.settled_baseline + self.price(event.cumulative_mwh)
        if amount <= self.cumulative_amount:
            # The contract rejects this as "not monotonic"; catching it here
            # stops a tick too small to bill from burning a nonce.
            raise ValueError(
                f"cumulativeAmount {amount} does not exceed the previous "
                f"{self.cumulative_amount} — tick delivered no billable energy"
            )

        issued_at = int(time.time()) if now is None else now
        return Voucher(
            channel=self.config.channel,
            nonce=self.nonce + 1,
            cumulativeAmount=amount,
            serviceRef=event.service_ref,
            deadline=issued_at + self.config.deadline_seconds,
        )

    def sign_voucher(self, voucher: Voucher) -> bytes:
        """Sign the EIP-712 digest. The domain pins chainId and the settlement
        contract, so the signature is worthless on another chain or channel.
        """
        typed_data = voucher.to_typed_data(
            chain_id=self.config.chain_id,
            verifying_contract=self.config.verifying_contract,
        )
        signable = encode_typed_data(full_message=typed_data)
        return bytes(self.account.sign_message(signable).signature)

    def handle_event(self, event: SensorEvent) -> SignedVoucher:
        """Sensor event -> signed voucher. Stamps `t1`.

        Channel state advances only after signing succeeds, so a failure here
        leaves the agent able to retry the same tick.
        """
        voucher = self.build_voucher(event)
        signature = self.sign_voucher(voucher)
        t1_ns, t1_mono_ns = time.time_ns(), time.monotonic_ns()

        self.nonce = voucher.nonce
        self.cumulative_amount = voucher.cumulativeAmount

        return SignedVoucher(
            voucher=voucher,
            signature=signature,
            device_address=self.address,
            chain_id=self.config.chain_id,
            verifying_contract=self.config.verifying_contract,
            event=event,
            t1_ns=t1_ns,
            t1_mono_ns=t1_mono_ns,
        )

    def run_tick(self, event: SensorEvent) -> SignedVoucher:
        """The whole device-side leg: sign, then hand to the transport."""
        signed = self.handle_event(event)
        self.transport.send(signed)
        return signed


def load_account(env: dict[str, str] | None = None) -> LocalAccount:
    """Load the device key.

    The order matches M2's requirement that the key is generated on the Pi and
    never leaves it:

    1. `DEVICE_KEYSTORE` — an encrypted JSON keystore under device/keystore/
       (gitignored), decrypted with `DEVICE_KEYSTORE_PASSWORD`.
    2. `DEVICE_PRIVATE_KEY` — a raw key from `.env`, for local M1 work only.
    """
    e = os.environ if env is None else env

    keystore_path = e.get("DEVICE_KEYSTORE")
    if keystore_path:
        password = e.get("DEVICE_KEYSTORE_PASSWORD")
        if not password:
            raise KeyError("DEVICE_KEYSTORE is set but DEVICE_KEYSTORE_PASSWORD is not")
        keyfile = json.loads(Path(keystore_path).read_text(encoding="utf-8"))
        return Account.from_key(Account.decrypt(keyfile, password))

    private_key = e.get("DEVICE_PRIVATE_KEY")
    if private_key:
        return Account.from_key(private_key)

    raise KeyError("no device key: set DEVICE_KEYSTORE (+ password) or DEVICE_PRIVATE_KEY")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Device payment agent (M1 demo).")
    parser.add_argument("--ticks", type=int, default=3, help="charging ticks to emit")
    parser.add_argument("--power-kw", type=float, default=50.0, help="nominal charger power")
    parser.add_argument("--tick-seconds", type=float, default=2.0, help="metering interval")
    parser.add_argument("--seed", type=int, default=None, help="seed the meter for a repeatable run")
    parser.add_argument(
        "--realtime",
        action="store_true",
        help="pace ticks at --tick-seconds of wall time instead of running flat out",
    )
    args = parser.parse_args(argv)

    agent = DeviceAgent(account=load_account(), config=AgentConfig.from_env())
    meter = KwhMeter(power_kw=args.power_kw, tick_seconds=args.tick_seconds, seed=args.seed)
    session = meter.start_session()

    # Vouchers go to stdout, progress to stderr, so the payloads stay pipeable.
    print(f"device {agent.address} session {session}", file=sys.stderr)
    for event in meter.stream(args.ticks, realtime=args.realtime):
        signed = agent.run_tick(event)
        print(
            f"  tick {event.sequence}: {event.cumulative_kwh:.4f} kWh cumulative, "
            f"nonce {signed.voucher.nonce}, {signed.voucher.cumulativeAmount} wei, "
            f"signed in {signed.signing_ns / 1e6:.2f} ms",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
