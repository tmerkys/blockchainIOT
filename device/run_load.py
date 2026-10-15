"""Concurrency experiment, device leg: one of K simulated devices.

    ./venv/bin/python device/run_load.py --device-index 3 \\
        --env-overlay .env.load.dev3 --env-overlay .env.load.dev3.channel \\
        --n 10 --start-at 1760000000.0

Driven by `experiments/concurrency.py`, which starts K of these at once — on
the Pi for a measurement session (so every voucher is still signed on the real
device hardware; the K processes share the Pi's 4 cores), or locally on the
laptop for a smoke test against anvil. Each process is one device with its
own key, its own channel and its own overlay env file; they share the Pi's
base `.env` only for `GATEWAY_URL` and the merchant countersigning key.

Same contract as `run_batch.py`, whose record builder it reuses: one JSON
line per run on stdout, flushed; humans on stderr; a `.jsonl` backup. The
extra fields are the device index and what the pipelining gateway reports
about its nonce handling (`failure`, `txNonce`, `nonceEpoch`, `inflight`,
`gapBehind`, `nonceResyncs`), so every failure lands as a distinct status.

**Env precedence**: real environment > overlays (in the order given) > the
base `.env` (if present). Each overlay is read with the same minimal reader
as `run_tick.py`; secrets in them (the keystore passphrase) are never printed.

**Start barrier**: `--start-at` is a wall-clock epoch (this machine's clock).
All K processes decrypt their keystore and read their channel state first,
then sleep until that instant, so the measured window starts with all K
devices ready instead of staggered by scrypt.
"""

from __future__ import annotations

import argparse
import json
import random
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
from run_batch import (  # noqa: E402
    STATUS_ERROR,
    STATUS_REFUSED,
    STATUS_UNREACHABLE,
    STATUS_WRONG_IFACE,
    CpuTimedCountersign,
    build_record,
    classify,
    route_iface,
)
from run_tick import ENV_FILE, load_env  # noqa: E402
from sensor import KwhMeter  # noqa: E402
from transport.http_client import GatewayError, HttpTransport  # noqa: E402

LOG_DIR = _HERE.parent / "experiments" / "device_log"

LOAD_FIELDS = ["failure", "txNonce", "nonceEpoch", "inflight", "gapBehind", "nonceResyncs"]
"""Fields the pipelining gateway adds to its result; copied onto each record."""


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def status_of(result: dict[str, Any]) -> str:
    """Status of a run the gateway answered with HTTP 200."""
    if result.get("failure"):
        return str(result["failure"])
    return classify(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One of K concurrent load devices.")
    parser.add_argument("--device-index", type=int, required=True)
    parser.add_argument("--env-overlay", type=Path, action="append", default=[])
    parser.add_argument("--base-env", type=Path, default=ENV_FILE,
                        help="read last, if it exists; 'none' to skip")
    parser.add_argument("--n", type=int, required=True, help="runs for this device")
    parser.add_argument("--start-at", type=float, default=None, help="wall-clock epoch to begin")
    parser.add_argument("--gap-min", type=float, default=0.0)
    parser.add_argument("--gap-max", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--expect-iface", default=None)
    parser.add_argument("--timeout", type=float, default=150.0)
    parser.add_argument("--max-consecutive-failures", type=int, default=5)
    parser.add_argument("--tag", default="load")
    parser.add_argument("--power-kw", type=float, default=50.0)
    parser.add_argument("--tick-seconds", type=float, default=2.0)
    parser.add_argument("--close-last", action="store_true",
                        help="send the last voucher as close() (teardown; refunds the deposit)")
    args = parser.parse_args(argv)

    for overlay in args.env_overlay:
        load_env(overlay)
    if str(args.base_env).lower() != "none" and args.base_env.exists():
        load_env(args.base_env)

    import os

    gateway_url = os.environ["GATEWAY_URL"]
    gateway_host = urlparse(gateway_url).hostname or ""
    config = AgentConfig.from_env()
    account = load_account()
    from run_tick import merchant_countersigner  # same import run_batch uses

    countersign, _ = merchant_countersigner(
        os.environ["MERCHANT_PRIVATE_KEY"], config.chain_id, config.verifying_contract
    )
    countersign = CpuTimedCountersign(countersign)
    transport = HttpTransport(gateway_url, countersign=countersign, timeout=args.timeout)

    state = transport.channel_state(config.channel)
    if not state.get("exists") or state.get("closed"):
        raise SystemExit(f"channel {config.channel} is missing or closed")
    if str(state.get("device", "")).lower() != account.address.lower():
        raise SystemExit(f"channel {config.channel} names another device")
    agent = DeviceAgent(account=account, config=config, transport=transport)
    agent.resume_from(paid=int(state["paid"]), nonce=int(state["nonce"]))

    seed = args.seed if args.seed is not None else int.from_bytes(os.urandom(4), "big")
    rng = random.Random(seed)
    meter = KwhMeter(power_kw=args.power_kw, tick_seconds=args.tick_seconds, seed=seed)
    meter.start_session()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    backup = (LOG_DIR / f"{args.tag}-dev{args.device_index}.jsonl").open("a", encoding="utf-8")
    say(f"dev{args.device_index} {account.address} channel {config.channel} "
        f"nonce from {state['nonce']}, remaining {state['remaining']} wei")

    if args.start_at is not None:
        wait = args.start_at - time.time()
        if wait < 0:
            say(f"dev{args.device_index}: start barrier already passed by {-wait:.1f} s")
        else:
            time.sleep(wait)

    failures = 0
    for run in range(1, args.n + 1):
        if run > 1 and args.gap_max > 0:
            time.sleep(rng.uniform(args.gap_min, args.gap_max))
        close = args.close_last and run == args.n
        iface = route_iface(gateway_host) if args.expect_iface else None
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
                result = transport.send(signed, close=close)
                status = status_of(result)
            except GatewayError as exc:
                body = exc.body if isinstance(exc.body, dict) else {}
                if body.get("failure"):
                    # Accepted, then the transaction failed: the pipelining
                    # gateway's own status, with whatever marks it got to.
                    status = str(body["failure"])
                    result = body
                else:
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
            if marks is not None and signed is not None and marks.t_send_ns < signed.t1_ns:
                marks = None

        record = build_record(
            run=run, iface=iface, signed=signed, marks=marks, result=result,
            status=status, error=error, cpu=cpu,
        )
        record["device"] = args.device_index
        record["deviceAddress"] = account.address
        record["channel"] = config.channel
        record["closed"] = bool(close)
        for key in LOAD_FIELDS:
            record[key] = (result or {}).get(key)
        line = json.dumps(record, separators=(",", ":"))
        print(line, flush=True)
        backup.write(line + "\n")
        backup.flush()

        latency = record["latency_ms"]
        say(f"  dev{args.device_index} run {run:>3}/{args.n}  {status:<22} "
            + (f"{latency:8.1f} ms" if latency is not None else (error or record["gatewayReason"] or "")))

        failures = 0 if status == "ok" else failures + 1
        if failures >= args.max_consecutive_failures:
            say(f"dev{args.device_index}: aborting after {failures} consecutive failures")
            return 2

    backup.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
