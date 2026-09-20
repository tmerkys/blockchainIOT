"""M4: 200 service ticks, one on-chain settlement.

    ./venv/bin/python device/run_channel.py --ticks 200

CLAUDE.md's M4 is "done when amortised on-chain cost per micropayment drops by
~2 orders of magnitude, measured". This is the command that measures it.

The mechanism is the whole point of the voucher design. `cumulativeAmount` is
the total owed so far, not a per-tick delta, so voucher #200 *supersedes* #199
rather than adding to it. Every tick is signed on the Pi and costs nothing on
chain; only the highest voucher is ever submitted, and `close()` submits it
once, pays the merchant the whole accumulated total in a single transfer, and
returns the unspent deposit to the funder.

So the comparison this script prints is between two ways of buying the same
200 micropayments:

  per-tick settlement  (M1/M2/M3)   200 x settle()   =  200 transactions
  channel              (M4)         1 x close()      =    1 transaction

and the amortised figure is the second divided by 200. Gas units are the
reproducible number; wei follows the gas price of the moment and EUR follows
the ETH price of the day, which is why the gas column is the one the thesis
argues from.

The 200 signatures are not free — they cost the Pi CPU time and, at M7, energy.
That cost is reported here too, because "the micropayment became free" is only
true on chain, and the honest claim is that the cost moved off it.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent / "shared"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from agent import AgentConfig, DeviceAgent, load_account  # noqa: E402
from run_tick import load_env, merchant_countersigner, ms  # noqa: E402
from sensor import KwhMeter  # noqa: E402
from transport.http_client import GatewayError, HttpTransport  # noqa: E402

PER_TICK_SETTLE_GAS = 75_029
"""Steady-state `settle()` cost measured on Base Sepolia at M3 (docs/measurements.md).

The *subsequent*-settlement figure, not the first (134,229): the per-tick baseline
this is compared against would be 200 settlements on an already-open channel, of
which 199 are subsequent ones. Using the cold-slot number would flatter M4."""


def report_accumulation(signing_ns: list[int], final: Any, ticks: int) -> None:
    """What the 200 off-chain ticks cost the device."""
    total = sum(signing_ns)
    print("\n--- off-chain accumulation (Pi) ----------------------------------")
    print(f"  ticks signed            {ticks}")
    print(f"  total signing time      {ms(total)}")
    print(f"  mean per tick           {ms(round(statistics.mean(signing_ns)))}")
    print(f"  median per tick         {ms(round(statistics.median(signing_ns)))}")
    print(f"  slowest tick            {ms(max(signing_ns))}")
    print(f"  on-chain transactions   0")
    print(f"  final voucher           nonce {final.voucher.nonce}, "
          f"{final.voucher.cumulativeAmount:,} wei cumulative")


def report_amortisation(result: dict[str, Any], ticks: int, open_gas: int | None) -> None:
    """The M4 claim, stated in gas and in fees, with the baseline it beats."""
    gas = result.get("gasUsed")
    price = result.get("effectiveGasPrice")
    l1_fee = result.get("l1Fee") or 0
    if not gas:
        print("\n  no gasUsed on the receipt — cannot compute the amortisation")
        return

    total_fee = (gas * price if price else 0) + l1_fee
    baseline_gas = PER_TICK_SETTLE_GAS

    print("\n--- on-chain cost ------------------------------------------------")
    print(f"  close() gasUsed         {gas:,}")
    print(f"  l1Fee                   {l1_fee:,} wei")
    print(f"  total fee               {total_fee:,} wei")

    print("\n--- amortisation over {0} micropayments -------------------------".format(ticks))
    marginal = gas / ticks
    print(f"  baseline, per-tick settle()      {baseline_gas:>12,} gas/tick")
    print(f"  channel, close() / {ticks:<4}          {marginal:>12,.1f} gas/tick")
    print(f"  reduction                        {baseline_gas / marginal:>12,.1f}x  "
          f"({_orders(baseline_gas / marginal)} orders of magnitude)")

    if open_gas:
        # Full lifecycle: a channel is not free to create, and quoting only the
        # closing transaction would hide its setup. Reported separately rather
        # than folded in, because the two answer different questions.
        lifecycle = (open_gas + gas) / ticks
        base_lifecycle = (open_gas + baseline_gas * ticks) / ticks
        print(f"\n  including one-time open() ({open_gas:,} gas):")
        print(f"    baseline  (open + {ticks} settles)/{ticks}   {base_lifecycle:>12,.1f} gas/tick")
        print(f"    channel   (open + close)/{ticks}       {lifecycle:>12,.1f} gas/tick")
        print(f"    reduction                        {base_lifecycle / lifecycle:>12,.1f}x  "
              f"({_orders(base_lifecycle / lifecycle)} orders of magnitude)")

    if price:
        print(f"\n  fee per micropayment             {total_fee / ticks:>12,.1f} wei")


def _orders(ratio: float) -> str:
    import math

    return f"{math.log10(ratio):.2f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M4: N ticks off-chain, one settlement.")
    parser.add_argument("--ticks", type=int, default=200, help="service ticks to accumulate")
    parser.add_argument("--power-kw", type=float, default=50.0, help="nominal charger power")
    parser.add_argument("--tick-seconds", type=float, default=2.0, help="metering interval")
    parser.add_argument("--seed", type=int, default=None, help="meter seed, for a repeatable run")
    parser.add_argument(
        "--realtime",
        action="store_true",
        help="pace ticks at --tick-seconds of wall time (200 x 2 s = ~7 min)",
    )
    parser.add_argument(
        "--settle-only",
        action="store_true",
        help="submit settle() instead of close(), leaving the channel open",
    )
    parser.add_argument(
        "--open-gas",
        type=int,
        default=None,
        help="gasUsed by this channel's open(), from scripts/m2_setup.py, for the "
        "full-lifecycle amortisation. The device cannot read it: that receipt "
        "predates the channel and the Pi has no RPC.",
    )
    args = parser.parse_args(argv)

    env = load_env()
    gateway_url = env.get("GATEWAY_URL")
    merchant_key = env.get("MERCHANT_PRIVATE_KEY")
    if not gateway_url or not merchant_key:
        raise SystemExit("device .env needs GATEWAY_URL and MERCHANT_PRIVATE_KEY")

    config = AgentConfig.from_env()
    account = load_account()
    countersign, merchant_address = merchant_countersigner(
        merchant_key, config.chain_id, config.verifying_contract
    )
    transport = HttpTransport(gateway_url, countersign=countersign)

    print("=== 1. channel ===================================================")
    print(f"  device           {account.address}")
    print(f"  channel          {config.channel}")
    print(f"  price            {config.price_wei_per_kwh:,} wei/kWh")

    health = transport.health()
    if health.get("status") != "ok":
        raise SystemExit(f"  gateway is {health.get('status')}: {health}")

    state = transport.channel_state(config.channel)
    if not state.get("exists"):
        raise SystemExit(f"  channel {config.channel} does not exist on chain {config.chain_id}")
    if state.get("closed"):
        raise SystemExit(f"  channel {config.channel} is already closed — open a new one")
    if state["device"] != account.address:
        raise SystemExit(f"  channel device is {state['device']}, this Pi is {account.address}")

    deposit, paid = int(state["deposit"]), int(state["paid"])
    print(f"  deposit          {deposit:,} wei")
    print(f"  already paid     {paid:,} wei   (headroom {deposit - paid:,} wei)")
    print(f"  merchant         {merchant_address}")

    agent = DeviceAgent(account=account, config=config, transport=transport)
    agent.resume_from(paid=paid, nonce=int(state["nonce"]))

    print(f"\n=== 2. accumulate {args.ticks} ticks off-chain ==========================")
    meter = KwhMeter(power_kw=args.power_kw, tick_seconds=args.tick_seconds, seed=args.seed)
    session = meter.start_session()
    print(f"  session          {session}")
    if args.realtime:
        print(f"  pacing           realtime, ~{args.ticks * args.tick_seconds / 60:.1f} min")

    signing_ns: list[int] = []
    final = None
    started = time.monotonic()
    for event in meter.stream(args.ticks, realtime=args.realtime):
        # handle_event signs but does NOT transmit: this is the whole point.
        # Each voucher supersedes the last, so 199 of these are never sent
        # anywhere and never touch the chain.
        final = agent.handle_event(event)
        signing_ns.append(final.signing_ns)
        if final.voucher.nonce % 50 == 0 or final.voucher.nonce == 1:
            print(f"    tick {len(signing_ns):>4}  nonce {final.voucher.nonce:>4}  "
                  f"{final.voucher.cumulativeAmount:>18,} wei  "
                  f"({event.cumulative_kwh:.4f} kWh)")

    assert final is not None
    elapsed = time.monotonic() - started

    if final.voucher.cumulativeAmount > deposit:
        # Caught here rather than as an on-chain "exceeds deposit" revert, which
        # would cost gas to discover and leave the channel mid-session.
        raise SystemExit(
            f"\n  accumulated {final.voucher.cumulativeAmount:,} wei but the deposit is only "
            f"{deposit:,} wei.\n  Lower PRICE_WEI_PER_KWH, shorten the run, or open a larger "
            "channel — nothing was submitted."
        )

    report_accumulation(signing_ns, final, args.ticks)
    print(f"  wall time for accumulation {elapsed:.2f} s")

    action = "settle()" if args.settle_only else "close()"
    print(f"\n=== 3. one on-chain transaction: {action} =========================")
    try:
        result = transport.send(final, close=not args.settle_only)
    except GatewayError as exc:
        print(f"\n  GATEWAY REFUSED: {exc}")
        if exc.body:
            print(f"  body: {json.dumps(exc.body, indent=2)}")
        return 1

    if not result.get("submitted"):
        print(f"\n  accepted but not submitted: {result.get('reason')}")
        return 1

    print(f"  tx               {result.get('txHash')}")
    print(f"  block            {result.get('blockNumber')}")
    print(f"  nonce settled    {result.get('nonce')}")
    print(f"  cumulativeAmount {int(result['cumulativeAmount']):,} wei")

    report_amortisation(result, args.ticks, args.open_gas)

    after = transport.channel_state(config.channel)
    print("\n--- channel after -----------------------------------------------")
    print(f"  paid             {int(after['paid']):,} wei")
    print(f"  nonce            {after['nonce']}")
    print(f"  closed           {after['closed']}")
    print(f"  refunded to funder {int(after['deposit']) - int(after['paid']):,} wei")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
