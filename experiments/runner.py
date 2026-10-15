"""M5 experiment runner: N settled runs per condition, one CSV per session.

    python experiments/runner.py --transport wifi --n 30

Runs on the laptop, next to the gateway. Drives the Pi over SSH:

  1. preflight — the gateway is healthy and on the chain it claims, both
     machines are on the same clean commit, the Pi has both links up;
  2. link switch — takes the Pi's *other* interface down, so the condition's
     interface is the only route to the gateway (see below);
  3. batch — runs `device/run_batch.py` on the Pi and turns each JSON line it
     prints into a CSV row the moment it arrives;
  4. restore — brings the other interface back, whatever happened in 2–3.

**Why the other link goes down.** The Pi and the laptop share one /24, and
both `eth0` (metric 100) and `wlan0` (metric 600) carry a default route into
it. With both up, the kernel sends everything over `eth0`, so a WiFi session
with the cable plugged in would be an Ethernet session with a WiFi label.
Unplugging cables by hand between conditions is how that mistake gets made,
so the runner does it with `nmcli` — and the batch still checks the route
before every tick and refuses a run on the wrong interface.

**SSH goes over the link that stays up.** Taking `eth0` down from a session
that is running over `eth0` kills the session. The runner learns both of the
Pi's addresses during preflight and, for each condition, connects to the
address on the interface being *measured*. That also means a dead measured
link fails loudly at connect time instead of halfway through the batch.
The SSH control traffic shares that link with the vouchers, but it is idle
while a tick is in flight — the batch only prints between runs.

**The dataset is append-only and never overwritten.** One file per session,
created exclusively; a header first, then one row per run, flushed as it
lands. A session killed at run 17 leaves 17 valid rows. Failed runs are rows
too, with `status` saying why — `analyze.py` filters on `status == "ok"`, and
the failure count per condition is itself worth reporting.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import statistics
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.rpc import provider_of, rpc_host  # noqa: E402  (exp/rpc)

RESULTS_DIR = REPO_ROOT / "experiments" / "results"

PI_ALIAS = "magis-pi"
"""~/.ssh/config host. Its HostName is the mDNS name; the runner overrides
HostName with a per-interface IP and pins the host key to the mDNS name."""
PI_HOSTKEY_ALIAS = "magis-pi.local"
PI_REPO = "~/magis"

TRANSPORTS = {
    # condition: (interface measured, interface taken down)
    "eth": ("eth0", "wlan0"),
    "wifi": ("wlan0", "eth0"),
}

CHAINS = {31337: "anvil", 84532: "base-sepolia", 11155111: "sepolia"}

# Seconds between runs, uniform. At least one block period, so consecutive runs
# land at uncorrelated points in the block cycle (Base 2 s, Ethereum 12 s).
DEFAULT_GAP = {"sepolia": (12.0, 24.0)}
DEFAULT_GAP_L2 = (2.0, 4.0)


def pi_overlay(chain: str) -> str:
    """Per-chain overrides on the Pi, sourced over its `.env` for one session.

    Only CHAIN_ID / VERIFYING_CONTRACT / CHANNEL_ADDRESS differ between chains;
    the device key, gateway URL and merchant key do not, so they stay in the
    one `.env` and the overlay holds just the three that do. Base Sepolia,
    where M2–M5 started, is the no-overlay case.
    """
    return f".env.{chain}"

SESSION_COLUMNS = [
    "session",
    "transport",
    "chain",
    "chainId",
    "contract",
    "channel",
    "commit",
    "piCommit",
    "seed",
    "startedUtc",
    "wifiPowersave",
    "cpuGovernor",
]
"""`wifiPowersave` and `cpuGovernor` were added after the first M5 pair
(02b8bb5); those CSVs lack them and ran with NM `default` / `ondemand`."""
RECORD_COLUMNS = [
    "run", "status", "error", "iface",
    "t0_wall", "t1_wall", "signing_ms", "rtt_ms", "latency_ms", "network_ms", "clock_offset_ms",
    "t2", "t3", "t4", "t5", "t6",
    "nonce", "cumulativeAmount", "serviceRef", "txHash", "blockNumber",
    "gasUsed", "effectiveGasPrice", "l1Fee", "l1GasUsed", "l1GasPrice", "gatewayReason",
    "cpu_sign_ms", "cpu_countersign_ms", "cpu_device_ms",
]
"""Must match `device/run_batch.py:build_record()` key for key; checked per row.
The three `cpu_*` columns were added after the M5 L2/L1 sessions (a0ea140);
earlier CSVs lack them."""

# --- exp/rpc ------------------------------------------------------------------
RPC_RECORD_COLUMNS = ["rpcHost", "blockTimestamp", "headAtReceipt"]
"""Added for the dedicated-RPC experiment; earlier CSVs lack them and
`analyze.py` reads a missing `rpcHost` as publicnode. `rpcHost` is the
gateway's RPC **host only** — never the URL, which for a dedicated endpoint
holds the API key. Filled from `/health` when a failed run got no reply."""
RECORD_COLUMNS = RECORD_COLUMNS + RPC_RECORD_COLUMNS
# --- end exp/rpc --------------------------------------------------------------


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --- SSH -------------------------------------------------------------------


def ssh_argv(command: str, *, host: str | None = None) -> list[str]:
    argv = ["ssh", "-o", "BatchMode=yes"]
    if host:
        argv += ["-o", f"HostName={host}", "-o", f"HostKeyAlias={PI_HOSTKEY_ALIAS}"]
    return argv + [PI_ALIAS, command]


def ssh(command: str, *, host: str | None = None, timeout: float = 60) -> str:
    proc = subprocess.run(
        ssh_argv(command, host=host), capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ssh `{command}` failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def pi_addresses() -> dict[str, str]:
    """IPv4 address of each Pi interface that has one."""
    out = ssh("ip -4 -o addr show")
    addrs = {}
    for line in out.splitlines():
        parts = line.split()
        # "<idx>: <dev> inet <addr>/<len> ..."
        if len(parts) >= 4 and parts[2] == "inet":
            addrs[parts[1]] = parts[3].split("/")[0]
    return addrs


def link(action: str, iface: str, *, host: str) -> None:
    """`nmcli device connect|disconnect`, via the passwordless sudoers rule."""
    ssh(f"sudo -n nmcli --wait 30 device {action} {iface}", host=host, timeout=60)


# --- preflight -------------------------------------------------------------


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def commit_id(head: str, dirty: bool) -> str:
    return head + ("-dirty" if dirty else "")


def gateway_health(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{url}/health", timeout=10) as resp:
        return json.load(resp)


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    head = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    if dirty and not args.allow_dirty:
        raise SystemExit("working tree is dirty: commit first, or pass --allow-dirty for a smoke run")

    pi_head = ssh(f"cd {PI_REPO} && git rev-parse HEAD").strip()
    pi_dirty = bool(ssh(f"cd {PI_REPO} && git status --porcelain --untracked-files=no").strip())
    if pi_head != head and not args.allow_dirty:
        raise SystemExit(f"Pi is on {pi_head[:7]}, laptop on {head[:7]}: sync the Pi first")

    health = gateway_health(args.gateway)
    if health.get("status") != "ok":
        raise SystemExit(f"gateway is {health.get('status')}: {health}")
    if not health.get("submit"):
        raise SystemExit("gateway is in calldata-only mode (GATEWAY_SUBMIT=0); nothing would settle")

    # exp/rpc: which RPC the gateway settles through — host only. A gateway
    # older than exp/rpc reports `rpcUrl`; reduce that to its host here too.
    gateway_rpc_host = health.get("rpcHost") or rpc_host(health.get("rpcUrl"))
    provider = provider_of(gateway_rpc_host)
    say(f"  gateway RPC host {gateway_rpc_host} (provider {provider})")
    if args.expect_provider and provider != args.expect_provider:
        raise SystemExit(
            f"gateway RPC provider is {provider} ({gateway_rpc_host}), expected {args.expect_provider}"
        )

    chain_id = int(health["chainId"])
    chain = CHAINS.get(chain_id, str(chain_id))
    overlay = pi_overlay(chain)
    # Overlay lines come second, so they win in the dict — same precedence the
    # batch gets from sourcing the overlay into the environment.
    pi_env = dict(
        line.split("=", 1)
        for line in ssh(
            f"cd {PI_REPO} && grep -hE '^(CHAIN_ID|VERIFYING_CONTRACT|CHANNEL_ADDRESS)=' "
            f".env $(test -f {overlay} && echo {overlay})"
        ).splitlines()
        if "=" in line
    )
    if int(pi_env.get("CHAIN_ID", -1)) != chain_id:
        raise SystemExit(f"Pi signs for chain {pi_env.get('CHAIN_ID')}, gateway is on {chain_id}")
    if pi_env.get("VERIFYING_CONTRACT", "").lower() != str(health["paymentChannel"]).lower():
        raise SystemExit(
            f"Pi signs for {pi_env.get('VERIFYING_CONTRACT')}, gateway submits to "
            f"{health['paymentChannel']}: every signature would fail on-chain"
        )

    # Both links up, so either can carry the control session for its condition.
    # Only touch an interface that is down: re-activating a connected one drops
    # it briefly, and it may be the one this very SSH session is riding on.
    states = dict(
        line.split(":", 1) for line in ssh("nmcli -t -f DEVICE,STATE device").splitlines() if ":" in line
    )
    for iface in TRANSPORTS[args.transport]:
        if states.get(iface) != "connected":
            say(f"  bringing {iface} up")
            link("connect", iface, host=None)
    addrs = pi_addresses()
    for iface in TRANSPORTS[args.transport]:
        if iface not in addrs:
            raise SystemExit(f"Pi {iface} has no IPv4 address after connect")

    # Pi settings that measurably move the numbers (docs/measurements.md, M5):
    # WiFi power save delays the reply leg, the governor slows signing.
    powersave = ssh(
        "nmcli -g 802-11-wireless.powersave connection show "
        "\"$(nmcli -g GENERAL.CONNECTION device show wlan0)\""
    ).strip()
    governors = set(ssh("cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor").split())
    say(f"  Pi config: wifi powersave={powersave}, cpu governor={'/'.join(sorted(governors))}")

    return {
        "wifiPowersave": powersave,
        "cpuGovernor": "/".join(sorted(governors)),
        "commit": commit_id(head, dirty),
        "piCommit": commit_id(pi_head, pi_dirty),
        "chainId": chain_id,
        "chain": chain,
        "contract": health["paymentChannel"],
        "channel": pi_env["CHANNEL_ADDRESS"],
        "addrs": addrs,
        "rpcHost": gateway_rpc_host,  # exp/rpc
    }


# --- batch -----------------------------------------------------------------


def stream_batch(command: str, *, host: str) -> Iterator[dict[str, Any]]:
    """Run the batch on the Pi and yield each record as soon as it is printed.

    The Pi's stderr (progress lines) is passed straight through to ours.
    """
    proc = subprocess.Popen(
        ssh_argv(command, host=host), stdout=subprocess.PIPE, stderr=None, text=True, bufsize=1
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("{"):
                yield json.loads(line)
    finally:
        proc.stdout.close()
        code = proc.wait()
        if code not in (0, None):
            say(f"batch exited with code {code}")


def run_session(args: argparse.Namespace, meta: dict[str, Any]) -> Path:
    measured, other = TRANSPORTS[args.transport]
    host = meta["addrs"][measured]
    seed = args.seed if args.seed is not None else int.from_bytes(os.urandom(4), "big")
    started = datetime.now(timezone.utc)
    session = f"{started:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"

    session_row = {
        "session": session,
        "transport": args.transport,
        "chain": meta["chain"],
        "chainId": meta["chainId"],
        "contract": meta["contract"],
        "channel": meta["channel"],
        "commit": meta["commit"],
        "piCommit": meta["piCommit"],
        "seed": seed,
        "startedUtc": started.isoformat(timespec="seconds"),
        "wifiPowersave": meta["wifiPowersave"],
        "cpuGovernor": meta["cpuGovernor"],
    }

    args.results_dir.mkdir(parents=True, exist_ok=True)
    path = args.results_dir / f"{args.experiment}_{meta['chain']}_{args.transport}_{session}.csv"

    gap_min, gap_max = (
        (args.gap_min, args.gap_max)
        if args.gap_min is not None and args.gap_max is not None
        else DEFAULT_GAP.get(meta["chain"], DEFAULT_GAP_L2)
    )
    overlay = pi_overlay(meta["chain"])
    batch = " ".join(
        [
            f"cd {PI_REPO} &&",
            # Environment beats .env in load_env(), so sourcing the overlay
            # retargets the device without editing its .env.
            f"set -a && {{ test -f {overlay} && . ./{overlay} || true; }} && set +a &&",
            "./venv/bin/python device/run_batch.py",
            f"--n {args.n}",
            f"--expect-iface {measured}",
            f"--gap-min {gap_min} --gap-max {gap_max}",
            f"--timeout {args.timeout}",
            f"--seed {seed}",
            f"--tag {shlex.quote(session)}",
        ]
    )

    say(f"session {session}: {args.transport} ({measured}), {meta['chain']}, n={args.n}")
    say(f"  taking {other} down, control over {measured} @ {host}")
    link("disconnect", other, host=host)
    latencies: list[float] = []
    statuses: dict[str, int] = {}
    try:
        with path.open("x", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=SESSION_COLUMNS + RECORD_COLUMNS)
            writer.writeheader()
            fh.flush()
            for record in stream_batch(batch, host=host):
                if set(record) != set(RECORD_COLUMNS):
                    raise RuntimeError(
                        f"record shape drifted from runner columns: {sorted(set(record) ^ set(RECORD_COLUMNS))}"
                    )
                if not record.get("rpcHost"):  # exp/rpc: no reply, so no host from it
                    record["rpcHost"] = meta.get("rpcHost")
                writer.writerow({**session_row, **record})
                fh.flush()
                statuses[record["status"]] = statuses.get(record["status"], 0) + 1
                if record["status"] == "ok":
                    latencies.append(record["latency_ms"])
    finally:
        say(f"  restoring {other}")
        try:
            link("connect", other, host=host)
        except Exception as exc:  # noqa: BLE001 — report, don't mask the real error
            say(f"  WARNING: could not bring {other} back up: {exc}")

    say(f"wrote {path}")
    say(f"  statuses {statuses}")
    if latencies:
        mean = statistics.mean(latencies)
        sd = statistics.stdev(latencies) if len(latencies) > 1 else 0.0
        say(f"  latency  n={len(latencies)} mean {mean:.1f} ms  sd {sd:.1f} ms")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M5: N settled runs for one condition.")
    parser.add_argument("--transport", choices=[*TRANSPORTS, "ble"], required=True)
    parser.add_argument("--n", type=int, default=30, help="runs (CLAUDE.md: N>=30)")
    parser.add_argument("--gateway", default="http://127.0.0.1:8000", help="gateway URL, laptop side")
    parser.add_argument("--gap-min", type=float, default=None, help="default: per chain")
    parser.add_argument("--gap-max", type=float, default=None, help="default: per chain")
    parser.add_argument(
        "--timeout",
        type=float,
        default=150.0,
        help="Pi-side HTTP timeout; must outlast the gateway's 120 s receipt wait on L1",
    )
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--experiment",
        default="m5",
        choices=["m5", "tp"],
        help="file prefix. m5: the latency conditions analyze.py pools. tp: the H2 "
        "back-to-back throughput burst (--gap-min 0 --gap-max 0), which experiments/"
        "throughput.py reads and analyze.py must not pool with spaced runs",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=RESULTS_DIR,
        help="where the CSV goes; point smoke runs elsewhere so they never enter the dataset",
    )
    parser.add_argument(
        "--expect-provider",
        default=None,
        help="exp/rpc: refuse to start unless the gateway's RPC host maps to this provider "
        "(e.g. publicnode, alchemy — see shared/rpc.py:provider_of)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="smoke runs only: allow uncommitted changes and a Pi on another commit "
        "(recorded as -dirty / the Pi's own hash in every row)",
    )
    args = parser.parse_args(argv)

    if args.transport == "ble":
        raise SystemExit("BLE transport is not implemented yet (device/transport/ble_client.py)")
    if args.n < 30 and not args.allow_dirty:
        say(f"note: n={args.n} is below CLAUDE.md's N>=30 for a reported condition")

    meta = preflight(args)
    run_session(args, meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
