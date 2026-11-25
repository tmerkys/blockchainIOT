"""Runs ON the Raspberry Pi during the demo (mode 1).

The demo server starts this over SSH and keeps it running:

    ssh magis-pi 'cd ~/magis && venv/bin/python -u demo/pi_agent.py --gateway-url ...'

It loads the Pi's own key once (from the keystore the Pi generated; the key
never leaves the Pi), then reads one JSON command per line on stdin and prints
one JSON event per line on stdout. Commands:

    {"cmd": "tick", "channel": "0x..."}
    {"cmd": "channel", "channel": "0x...", "ticks": 200}
    {"cmd": "quit"}

The payment logic is demo/device_flow.py, the same code the laptop modes run.
"""

from __future__ import annotations

import argparse
import json
import platform
import socket
import sys
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from demo.device_flow import DEMO_PRICE_WEI_PER_KWH, DeviceSession  # noqa: E402
from device.agent import load_account  # noqa: E402


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def emit(event: dict[str, Any]) -> None:
    print(json.dumps(event), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Demo device agent, on the Pi.")
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--chain-id", type=int, required=True)
    parser.add_argument("--price", type=int, default=DEMO_PRICE_WEI_PER_KWH)
    args = parser.parse_args(argv)

    env = parse_env(REPO_ROOT / ".env")
    merchant_key = env.get("MERCHANT_PRIVATE_KEY")
    if not merchant_key:
        emit({"type": "fatal", "message": "~/magis/.env has no MERCHANT_PRIVATE_KEY"})
        return 1
    try:
        account = load_account(env)
    except Exception as exc:  # noqa: BLE001 - reported to the demo server, not swallowed
        emit({"type": "fatal", "message": f"could not load the device key: {exc}"})
        return 1

    session = DeviceSession(
        account=account,
        contract=args.contract,
        chain_id=args.chain_id,
        gateway_url=args.gateway_url,
        merchant_key=merchant_key,
        price_wei_per_kwh=args.price,
    )
    emit(
        {
            "type": "hello",
            "host": socket.gethostname(),
            "machine": platform.machine(),
            "system": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "device": account.address,
            "keystore": bool(env.get("DEVICE_KEYSTORE")),
        }
    )

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
            if cmd.get("cmd") == "quit":
                break
            if cmd.get("cmd") == "tick":
                session.pay_once(cmd["channel"], emit)
            elif cmd.get("cmd") == "channel":
                session.run_channel(cmd["channel"], int(cmd.get("ticks", 200)), emit)
            else:
                emit({"type": "error", "message": f"unknown command {cmd!r}"})
        except Exception as exc:  # noqa: BLE001
            emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            traceback.print_exc(file=sys.stderr)
        emit({"type": "done"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
