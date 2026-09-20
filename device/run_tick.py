"""M2 device leg: one service tick, originating on the Pi.

    ./venv/bin/python device/run_tick.py

CLAUDE.md's M2 is "done when the same flow runs with the sensor event
physically originating on the Pi". This is that command. It runs on the Pi,
holds the device key, and talks to a gateway on another machine; it deploys
nothing, funds nothing, and never sends a transaction.

**The clock.** This is what separates M2's instrumentation from M1's. In M1
every mark came off one machine, so `t4 - t0` was a subtraction of two
readings of the same clock. Here `t0` is the Pi's and the gateway's marks are
the laptop's, and subtracting across that boundary would silently fold the NTP
offset between the two boxes into the headline H1 number — in exactly the
direction nobody would notice, because a plausible latency stays plausible
after a few milliseconds of skew are added to it.

So the reported number never crosses the boundary. The gateway holds the HTTP
request open until it has the receipt, which means the reply landing on the Pi
*is* the Pi's own observation of "settled in a block". `HttpTransport` stamps
that instant (`t4_dev`) on the same monotonic clock `sensor.py` stamps `t0` on,
and

    reported latency = t4_dev - t0        both on the Pi, monotonic

has no clock-offset term in it at all. It is a slight over-measure of the
gateway's `t4` — it includes the reply's trip back down the wire — and that is
the right direction to be wrong in: it cannot flatter the result.

The gateway's own t2-t5 still come back and are still used, but only in
differences among themselves, which share the laptop's clock. The offset
between the two machines is estimated and printed as a diagnostic (CLAUDE.md
asks for it in `docs/measurements.md`); nothing is corrected by it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent / "shared"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402

from agent import AgentConfig, DeviceAgent, load_account  # noqa: E402
from sensor import KwhMeter  # noqa: E402
from transport.http_client import DeviceMarks, GatewayError, HttpTransport  # noqa: E402
from voucher import Voucher  # noqa: E402

ENV_FILE = _HERE.parent / ".env"


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    """Read the Pi's `.env` into a dict and into `os.environ`.

    Same minimal `KEY=value` reader as `scripts/e2e_m1.py`, duplicated rather
    than imported: only `device/` and `shared/` are meant to survive onto the
    Pi, and a device script that imports from `scripts/` would quietly make
    the whole repo a deployment dependency.
    """
    if not path.exists():
        raise SystemExit(f"no {path} — copy .env.example and fill in the device section")

    values: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"{path}:{lineno}: expected KEY=value, got {raw!r}")
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    # Real environment variables win, so a run can be overridden without an edit.
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def merchant_countersigner(key: str, chain_id: int, verifying_contract: str):
    """The charging station operator's third signature.

    Collected on the device side because the two parties are physically
    together — the car is plugged into the station — so the countersignature
    rides along with the voucher rather than costing a second round trip. It
    is a stand-in for `merchant/signer.py` as its own process, and the key it
    uses is an anvil dev key, unlike the device key beside it.
    """
    merchant = Account.from_key(key)

    def countersign(voucher: Voucher) -> str:
        signable = encode_typed_data(
            full_message=voucher.to_typed_data(
                chain_id=chain_id, verifying_contract=verifying_contract
            )
        )
        return "0x" + Account.sign_message(signable, merchant.key).signature.hex()

    return countersign, merchant.address


def ms(ns: int | None) -> str:
    return "      -- " if ns is None else f"{ns / 1e6:8.1f} ms"


def gateway_ms(a: float | None, b: float | None) -> str:
    """Format a gateway-internal span. Both marks are the laptop's clock."""
    if a is None or b is None:
        return "      -- "
    return f"{(a - b) * 1000:8.1f} ms"


def report(result: dict[str, Any], signed: Any, marks: DeviceMarks) -> None:
    """Print the decomposition, keeping each span's clock explicit."""
    t2, t3, t4, t5, t6 = (result.get(k) for k in ("t2", "t3", "t4", "t5", "t6"))
    signing_ns = signed.signing_ns
    reported_ns = marks.t4_dev_mono_ns - signed.event.t0_mono_ns

    print("\n--- device clock (Pi, monotonic) ---------------------------------")
    print(f"  signing          t1 - t0       {ms(signing_ns)}")
    print(f"  round trip       t4_dev - send {ms(marks.rtt_ns)}")
    print(f"  REPORTED         t4_dev - t0   {ms(reported_ns)}   <- the H1 number")

    print("\n--- gateway clock (laptop, internal spans only) -------------------")
    print(f"  build + broadcast  t3 - t2     {gateway_ms(t3, t2)}")
    print(f"  inclusion          t4 - t3     {gateway_ms(t4, t3)}")
    print(f"  confirmation       t5 - t4     {gateway_ms(t5, t4)}")
    print(f"  gateway total      t4 - t2     {gateway_ms(t4, t2)}")

    print("\n--- derived (no clock-offset term) -------------------------------")
    network_ns = marks.network_ns(t2, t4)
    print(f"  network + framing, both legs   {ms(network_ns)}")
    print("    = device round trip - gateway total; each span is one clock,")
    print("      so the two machines' offsets cancel instead of accumulating.")

    print("\n--- clock skew check (diagnostic only) ---------------------------")
    offset_ns = marks.clock_offset_ns(t2, t6)
    if offset_ns is None:
        print("  gateway did not return t2/t6 — cannot estimate the offset")
    else:
        print(f"  estimated offset, gateway - device  {offset_ns / 1e6:+.1f} ms")
        print("  Not subtracted from anything above. Log it in docs/measurements.md.")

    print("\n--- cost ---------------------------------------------------------")
    gas_used, price, l1_fee = result.get("gasUsed"), result.get("effectiveGasPrice"), result.get("l1Fee")
    print(f"  gasUsed             {gas_used:,}" if gas_used else "  gasUsed             --")
    print(f"  effectiveGasPrice   {price:,} wei" if price else "  effectiveGasPrice   --")
    if gas_used and price:
        print(f"  execution fee       {gas_used * price} wei")
    print(f"  l1Fee               {l1_fee:,} wei" if l1_fee else "  l1Fee               n/a (not an OP-stack chain)")

    print("\n--- settlement ---------------------------------------------------")
    print(f"  tx               {result.get('txHash')}")
    print(f"  block            {result.get('blockNumber')}")
    print(f"  channel          {result.get('channel')}")
    print(f"  nonce            {result.get('nonce')}")
    print(f"  cumulativeAmount {int(result['cumulativeAmount']):,} wei")
    # The chain's view of the service fact, against what this Pi hashed and signed.
    expected = "0x" + signed.voucher.serviceRef.hex()
    verdict = "matches the voucher this Pi signed" if result.get("serviceRef") == expected else "MISMATCH"
    print(f"  serviceRef       {result.get('serviceRef')}")
    print(f"  serviceRef check {verdict}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One device tick, from the Pi (M2).")
    parser.add_argument("--power-kw", type=float, default=50.0, help="nominal charger power")
    parser.add_argument("--tick-seconds", type=float, default=2.0, help="metering interval")
    parser.add_argument("--seed", type=int, default=None, help="meter seed, for a repeatable run")
    parser.add_argument("--session", default=None, help="reuse a session id across runs")
    args = parser.parse_args(argv)

    env = load_env()
    gateway_url = env.get("GATEWAY_URL")
    if not gateway_url:
        raise SystemExit(f"{ENV_FILE} is missing GATEWAY_URL (e.g. http://192.168.32.54:8000)")
    merchant_key = env.get("MERCHANT_PRIVATE_KEY")
    if not merchant_key:
        raise SystemExit(f"{ENV_FILE} is missing MERCHANT_PRIVATE_KEY; settle() is 3-of-3")

    config = AgentConfig.from_env()
    # The key is only ever read here, from the keystore this Pi generated.
    account = load_account()

    print("=== 1. device ====================================================")
    print(f"  address          {account.address}")
    print(f"  keystore         {env.get('DEVICE_KEYSTORE', '(raw key from .env)')}")
    print(f"  channel          {config.channel}")
    print(f"  verifyingContract {config.verifying_contract}  chainId {config.chain_id}")

    countersign, merchant_address = merchant_countersigner(
        merchant_key, config.chain_id, config.verifying_contract
    )
    print(f"  merchant         {merchant_address} (countersigned on-device)")

    print("\n=== 2. gateway ===================================================")
    transport = HttpTransport(gateway_url, countersign=countersign)
    try:
        health = transport.health()
    except GatewayError as exc:
        raise SystemExit(f"  {exc}\n  Is uvicorn bound to 0.0.0.0 and the port open on the laptop?")
    print(f"  url              {gateway_url}")
    print(
        f"  health           status={health['status']} chainIdMatches={health['chainIdMatches']} "
        f"contractDeployed={health['contractDeployed']} submit={health['submit']}"
    )
    if health.get("device") and health["device"] != account.address:
        # The gateway would reject the voucher anyway; saying so now is clearer
        # than a 422 that looks like a signing bug.
        raise SystemExit(
            f"  gateway expects device {health['device']}, this Pi is {account.address}\n"
            "  Re-run scripts/m2_setup.py on the laptop with this Pi's address."
        )

    print("\n=== 3. one tick ==================================================")
    agent = DeviceAgent(account=account, config=config, transport=transport)
    meter = KwhMeter(power_kw=args.power_kw, tick_seconds=args.tick_seconds, seed=args.seed)
    session = meter.start_session(args.session)

    event = meter.tick()
    signed = agent.handle_event(event)
    print(f"  session          {session}")
    print(
        f"  tick {event.sequence}: {event.cumulative_kwh:.6f} kWh cumulative, "
        f"nonce {signed.voucher.nonce}, {signed.voucher.cumulativeAmount:,} wei owed"
    )

    try:
        result = transport.send(signed)
    except GatewayError as exc:
        print(f"\n  GATEWAY REFUSED: {exc}")
        if exc.body:
            print(f"  body: {json.dumps(exc.body, indent=2)}")
        return 1

    if not result.get("submitted"):
        print(f"\n  accepted but not submitted: {result.get('reason')}")
        return 1

    print("\n=== 4. results ===================================================")
    assert transport.last_marks is not None  # send() returned, so it stamped
    report(result, signed, transport.last_marks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
