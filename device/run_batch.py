"""M5 device leg: N ticks, each settled on-chain, one JSON line per run.

    ./venv/bin/python device/run_batch.py --n 30 --expect-iface wlan0

Not meant to be run by hand in a measurement session — `experiments/runner.py`
on the laptop drives it over SSH, switches the Pi's link first, and turns these
lines into CSV rows. It is runnable by hand for a smoke test.

One process for the whole batch, on purpose. `run_tick.py` decrypts the
keystore (scrypt, ~1 s on a Pi 4) and opens a fresh TCP connection every time
it starts; neither is part of the latency a deployed device would see, and
neither sits inside `t0..t4`, but a batch of 30 process starts would be 30
minutes of nothing. Here the key is decrypted once and the HTTP session is
reused, which is also what a long-lived device agent would do.

**Stdout is the data channel.** Every run — successful or not — produces exactly
one line of JSON on stdout, flushed immediately, so a batch killed halfway
still leaves every finished run on the laptop. Everything meant for a human
goes to stderr. The same lines are appended to a local `.jsonl` as a second
copy, in case the SSH session dies before the laptop has read them.

**The interface is measured, not assumed.** Before each tick the script asks
the kernel which device the route to the gateway leaves by (`ip route get`)
and records it. A "WiFi" run that actually went out over `eth0` — because the
cable was still connected and wins on route metric — looks perfectly plausible
in the CSV and is wrong, so `--expect-iface` refuses to start a run on the
wrong link rather than flag it afterwards. The check runs before `t0`, so it
costs nothing inside the measured span.

**Spacing.** Runs are separated by a random gap, uniform in `[gap_min,
gap_max]`. Base produces a block every 2 s, so a tick that arrives at a fixed
offset from the last one lands at a correlated point in the block period, and
the inclusion term `t4 - t3` stops being the uniform draw it is supposed to be.
A random gap of at least one block period decorrelates consecutive runs. The
seed is recorded, so the schedule is reproducible.

**CPU time, as the device-cost figure (M7 fallback).** Without an inline power
meter, the device's cost per payment is reported as the CPU time this process
spends on it — a proxy for energy, and stated as one. Read with
`time.process_time_ns()` (`CLOCK_PROCESS_CPUTIME_ID`: user + system, all
threads, scheduler-precise — unlike `getrusage`, which is tick-sampled and
useless at the millisecond scale). Three numbers per run:

  * `cpu_sign_ms` — sensor tick, voucher build, EIP-712 hash and signature;
    the CPU behind the wall-clock `signing_ms`;
  * `cpu_countersign_ms` — the merchant's signature. It runs in this process
    only because the merchant is simulated here (see `run_tick.py`); it is
    the charging station's work, not the device's, so it is kept apart;
  * `cpu_device_ms` — the whole device-side operation, tick to parsed reply,
    *minus* the countersignature: signing, request building, JSON, HTTP send
    and receive. The ~1 s spent blocked on the gateway costs (almost) no CPU,
    which is the point of offloading the chain to it.

Not counted, by construction: kernel network work done in softirq context
(not charged to the process — notably WiFi driver and radio work), the radio's
own power draw, and the one-off keystore decryption at batch start. Those are
why this is a lower bound on the device's cost and not an energy measurement.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent / "shared"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from agent import AgentConfig, DeviceAgent, load_account  # noqa: E402
from run_tick import load_env, merchant_countersigner  # noqa: E402
from sensor import KwhMeter  # noqa: E402
from transport.http_client import DeviceMarks, GatewayError, HttpTransport  # noqa: E402

LOG_DIR = _HERE.parent / "experiments" / "device_log"

STATUS_OK = "ok"
STATUS_REVERTED = "reverted"  # mined, status 0
STATUS_NOT_SUBMITTED = "not_submitted"  # gateway in calldata-only mode
STATUS_REFUSED = "refused"  # gateway answered non-200
STATUS_UNREACHABLE = "unreachable"  # no answer at all, incl. timeout
STATUS_WRONG_IFACE = "wrong_iface"  # route left by the wrong device; not attempted
STATUS_ERROR = "error"  # anything else on the device side

_ROUTE_DEV = re.compile(r"\bdev\s+(\S+)")


def route_iface(host: str) -> str | None:
    """The interface the kernel would send a packet to `host` out of."""
    try:
        out = subprocess.run(
            ["ip", "route", "get", host], capture_output=True, text=True, timeout=5, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = _ROUTE_DEV.search(out)
    return match.group(1) if match else None


def classify(result: dict[str, Any]) -> str:
    """Status of a run the gateway answered with HTTP 200."""
    if not result.get("submitted"):
        return STATUS_NOT_SUBMITTED
    if result.get("reason"):
        # The relayer returns 200 for a mined-but-reverted settle(), with the
        # reason set. Counting that as a success would put a reverted tx's
        # latency and gas into the H1 dataset.
        return STATUS_REVERTED
    return STATUS_OK


def build_record(
    *,
    run: int,
    iface: str | None,
    signed: Any = None,
    marks: DeviceMarks | None = None,
    result: dict[str, Any] | None = None,
    status: str,
    error: str | None = None,
    cpu: dict[str, int | None] | None = None,
) -> dict[str, Any]:
    """Flatten one run into the record the laptop writes as a CSV row.

    Device-clock spans are computed here, on the device, because only the
    monotonic readings make them skew-free and those never leave the Pi.
    Gateway marks are passed through raw (float seconds, gateway clock) so the
    laptop can still form gateway-internal spans. Fields that a failed run
    could not produce are present and None — every row has the same shape.
    """
    result = result or {}
    cpu = cpu or {}
    t2, t4, t6 = result.get("t2"), result.get("t4"), result.get("t6")
    t0_mono = signed.event.t0_mono_ns if signed is not None else None

    def ns_to_ms(ns: int | None) -> float | None:
        return None if ns is None else round(ns / 1e6, 3)

    reported = (
        marks.reported_latency_ns(t0_mono, t4, t6) if marks and t0_mono is not None else None
    )
    return {
        "run": run,
        "status": status,
        "error": error,
        "iface": iface,
        # device clock, wall (for cross-checking only) and the spans that matter
        "t0_wall": signed.event.t0_ns / 1e9 if signed is not None else None,
        "t1_wall": signed.t1_ns / 1e9 if signed is not None else None,
        "signing_ms": ns_to_ms(signed.signing_ns) if signed is not None else None,
        "rtt_ms": ns_to_ms(marks.rtt_ns) if marks else None,
        "latency_ms": ns_to_ms(reported) if status == STATUS_OK else None,
        "network_ms": ns_to_ms(marks.network_ns(t2, t6)) if marks else None,
        "clock_offset_ms": ns_to_ms(marks.clock_offset_ns(t2, t6)) if marks else None,
        # gateway clock, raw
        "t2": t2,
        "t3": result.get("t3"),
        "t4": t4,
        "t5": result.get("t5"),
        "t6": t6,
        # chain
        "nonce": signed.voucher.nonce if signed is not None else None,
        "cumulativeAmount": str(signed.voucher.cumulativeAmount) if signed is not None else None,
        "serviceRef": "0x" + signed.voucher.serviceRef.hex() if signed is not None else None,
        "txHash": result.get("txHash"),
        "blockNumber": result.get("blockNumber"),
        "gasUsed": result.get("gasUsed"),
        "effectiveGasPrice": result.get("effectiveGasPrice"),
        "l1Fee": result.get("l1Fee"),
        "l1GasUsed": result.get("l1GasUsed"),
        "l1GasPrice": result.get("l1GasPrice"),
        "gatewayReason": result.get("reason"),
        # device CPU time, ms — see the module docstring
        "cpu_sign_ms": ns_to_ms(cpu.get("sign")),
        "cpu_countersign_ms": ns_to_ms(cpu.get("countersign")),
        "cpu_device_ms": ns_to_ms(cpu.get("device")),
    }


class CpuTimedCountersign:
    """Wraps the merchant countersigner and accumulates the CPU it uses, so
    the device's figure can exclude work that belongs to another party."""

    def __init__(self, countersign: Any) -> None:
        self.countersign = countersign
        self.ns = 0

    def __call__(self, voucher: Any) -> str:
        c0 = time.process_time_ns()
        try:
            return self.countersign(voucher)
        finally:
            self.ns += time.process_time_ns() - c0


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="N settled ticks from the Pi (M5).")
    parser.add_argument("--n", type=int, required=True, help="runs in this batch")
    parser.add_argument(
        "--expect-iface", default=None, help="refuse to run a tick unless the route leaves by this"
    )
    parser.add_argument("--gap-min", type=float, default=2.0, help="min seconds between runs")
    parser.add_argument("--gap-max", type=float, default=4.0, help="max seconds between runs")
    parser.add_argument("--seed", type=int, default=None, help="seed for the gap schedule and meter")
    parser.add_argument("--power-kw", type=float, default=50.0)
    parser.add_argument("--tick-seconds", type=float, default=2.0)
    parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=5,
        help="abort the batch after this many failed runs in a row",
    )
    parser.add_argument("--tag", default="batch", help="names the local .jsonl backup")
    parser.add_argument(
        "--timeout",
        type=float,
        default=150.0,
        help="HTTP timeout per voucher; the gateway holds the request until inclusion "
        "(12 s blocks on L1, longer under congestion)",
    )
    args = parser.parse_args(argv)

    env = load_env()
    gateway_url = env["GATEWAY_URL"]
    gateway_host = urlparse(gateway_url).hostname or ""
    config = AgentConfig.from_env()
    account = load_account()
    countersign, _ = merchant_countersigner(
        env["MERCHANT_PRIVATE_KEY"], config.chain_id, config.verifying_contract
    )
    countersign = CpuTimedCountersign(countersign)
    transport = HttpTransport(gateway_url, countersign=countersign, timeout=args.timeout)

    state = transport.channel_state(config.channel)
    if not state.get("exists") or state.get("closed"):
        raise SystemExit(f"channel {config.channel} is missing or closed; open a new one")
    agent = DeviceAgent(account=account, config=config, transport=transport)
    agent.resume_from(paid=int(state["paid"]), nonce=int(state["nonce"]))

    rng = random.Random(args.seed)
    meter = KwhMeter(power_kw=args.power_kw, tick_seconds=args.tick_seconds, seed=args.seed)
    meter.start_session()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    backup = (LOG_DIR / f"{args.tag}.jsonl").open("a", encoding="utf-8")
    say(
        f"batch {args.tag}: {args.n} runs, chain {config.chain_id}, "
        f"channel {config.channel}, nonce from {state['nonce']}"
    )

    failures = 0
    for run in range(1, args.n + 1):
        if run > 1:
            time.sleep(rng.uniform(args.gap_min, args.gap_max))

        iface = route_iface(gateway_host)
        signed = marks = result = None
        error = None
        cpu: dict[str, int | None] = {}
        if args.expect_iface and iface != args.expect_iface:
            status = STATUS_WRONG_IFACE
            error = f"route to {gateway_host} leaves by {iface}, expected {args.expect_iface}"
        else:
            countersign.ns = 0
            c0 = time.process_time_ns()
            try:
                signed = agent.handle_event(meter.tick())
                cpu["sign"] = time.process_time_ns() - c0
                result = transport.send(signed)
                status = classify(result)
            except GatewayError as exc:
                status = STATUS_REFUSED if exc.status else STATUS_UNREACHABLE
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 — a row, not a crash
                status = STATUS_ERROR
                error = f"{type(exc).__name__}: {exc}"
            c_end = time.process_time_ns()
            if signed is not None:
                cpu["countersign"] = countersign.ns
                cpu["device"] = (c_end - c0) - countersign.ns
            marks = transport.last_marks if signed is not None else None
            # last_marks persists across sends; only trust it if this send stamped it.
            if marks is not None and signed is not None and marks.t_send_ns < signed.t1_ns:
                marks = None

        record = build_record(
            run=run, iface=iface, signed=signed, marks=marks, result=result,
            status=status, error=error, cpu=cpu,
        )
        line = json.dumps(record, separators=(",", ":"))
        print(line, flush=True)
        backup.write(line + "\n")
        backup.flush()

        latency = record["latency_ms"]
        say(f"  run {run:>3}/{args.n}  {status:<13} {iface or '?':<6} "
            + (f"{latency:8.1f} ms" if latency is not None else (error or record["gatewayReason"] or "")))

        failures = 0 if status == STATUS_OK else failures + 1
        if failures >= args.max_consecutive_failures:
            say(f"aborting: {failures} consecutive failures")
            return 2

    backup.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
