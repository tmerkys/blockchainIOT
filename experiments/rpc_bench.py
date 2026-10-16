"""RPC benchmark: the relayer's pre-broadcast calls, publicnode vs a dedicated endpoint.

    python experiments/rpc_bench.py --chain base-sepolia --n 50

Read-only — no transaction is signed or sent. Times exactly the four calls
`gateway/relayer.py:_submit()` / `_fee_fields()` make between receiving a
voucher (t2) and broadcasting (t3), in the same order:

  1. `eth_getTransactionCount(gateway, "latest")`   — tx nonce
  2. `eth_estimateGas(tx)`                          — gas limit
  3. `eth_getBlockByNumber("latest", false)`        — base fee
  4. `eth_maxPriorityFeePerGas`                     — tip

The fifth call on that path, `eth_sendRawTransaction`, writes, so it is not
benchmarked here; it is inside the live `build_ms` (t3 − t2) instead. The
estimate is of a read-only `channels(channel)` call against the deployed
PaymentChannel from the gateway's address: it executes contract code like the
real `settle()` estimate, without needing three valid signatures.

**Connection reuse**, as in the gateway: one `Web3.HTTPProvider` (one
`requests.Session`, kept alive) per endpoint, and warm-up calls that are not
recorded — the M3 benchmark found a fresh TLS handshake per call overstates the
cost ~2x (docs/measurements.md, M3 cleanup §2). Endpoints are **interleaved**
per repetition, so slow minutes on the shared internet path hit both alike.

**Endpoints come from the environment, by name**, never from the command line:

  * `<CHAIN>_PUBLICNODE_RPC_URL` — default the public publicnode URL;
  * `<CHAIN>_DEDICATED_RPC_URL`  — optional; benchmarked when set.

`<CHAIN>` is `BASE_SEPOLIA` or `SEPOLIA`. Values may also sit in the env file
(`--env-file`, default `.env`). Only the **host** of each URL is printed or
written; a dedicated URL's path carries its API key.

Also probes, read-only, whether the endpoint is Flashblocks-aware: it compares
`eth_getBlockByNumber("pending")` with `"latest"`. A node that serves the
sequencer's 200 ms sub-blocks returns a pending block ahead of latest; one that
does not returns latest again (or nothing). This is a hint, not the answer — a
live session's per-row `t4_preconf` (analyze.py) is what decides it.

Outputs, host only: `experiments/analysis/rpc_bench_<chain>_<UTC>.csv` (every
call, raw) and `.json` (medians per endpoint and call, provenance).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from web3 import Web3  # noqa: E402

from gateway.relayer import SETTLE_ABI  # noqa: E402
from shared.rpc import provider_of, redact, rpc_host  # noqa: E402

OUT_DIR = REPO_ROOT / "experiments" / "analysis"

CHAINS = {
    # chain: (env suffix, chainId, public default, M5 contract, M5 channel)
    "base-sepolia": (
        "BASE_SEPOLIA", 84532, "https://base-sepolia-rpc.publicnode.com",
        "0xb746530c7e38aB163dE7238f70FE1e91c5E14bF9", "0x69af732caC97151F7bE06C2DeF0E84F222D420D2",
    ),
    "sepolia": (
        "SEPOLIA", 11155111, "https://ethereum-sepolia-rpc.publicnode.com", None, None,
    ),
}
CALLS = ["getTransactionCount", "estimateGas", "getBlock_latest", "maxPriorityFeePerGas"]


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=value lines, without touching os.environ and without echoing anything."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def endpoints(chain: str, env_file: Path) -> list[tuple[str, str]]:
    """[(label, url)] — publicnode always, dedicated when configured."""
    suffix, _, public_default, _, _ = CHAINS[chain]
    file_env = read_env_file(env_file)

    def get(name: str) -> str | None:
        return os.environ.get(name) or file_env.get(name) or None

    out = [("publicnode", get(f"{suffix}_PUBLICNODE_RPC_URL") or public_default)]
    dedicated = get(f"{suffix}_DEDICATED_RPC_URL")
    if dedicated:
        out.append(("dedicated", dedicated))
    return out


def git_commit() -> str:
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO_ROOT,
                               capture_output=True, text=True, check=True).stdout.strip()
        return head + ("-dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class Endpoint:
    def __init__(self, label: str, url: str, chain_id: int, sender: str, contract: str | None,
                 channel: str | None, timeout: float) -> None:
        self.label, self.url, self.host = label, url, rpc_host(url)
        self.provider = provider_of(self.host)
        self.w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": timeout}))
        observed = self.w3.eth.chain_id
        if observed != chain_id:
            raise SystemExit(f"{self.label} ({self.host}) is on chain {observed}, expected {chain_id}")
        self.sender = Web3.to_checksum_address(sender)
        if contract:
            c = self.w3.eth.contract(address=Web3.to_checksum_address(contract), abi=SETTLE_ABI)
            data = c.encode_abi("channels", args=[Web3.to_checksum_address(channel or contract)])
            self.tx = {"from": self.sender, "to": Web3.to_checksum_address(contract), "data": data, "value": 0}
        else:  # no contract on this chain: a plain transfer still exercises the estimator
            self.tx = {"from": self.sender, "to": self.sender, "value": 0}

    def calls(self) -> list[tuple[str, Callable[[], Any]]]:
        eth = self.w3.eth
        return [
            ("getTransactionCount", lambda: eth.get_transaction_count(self.sender)),
            ("estimateGas", lambda: eth.estimate_gas(self.tx)),
            ("getBlock_latest", lambda: eth.get_block("latest")),
            ("maxPriorityFeePerGas", lambda: eth.max_priority_fee),
        ]

    def flashblocks_probe(self) -> dict[str, Any]:
        """pending vs latest block number, read-only."""
        try:
            latest = self.w3.eth.get_block("latest")
            pending = self.w3.eth.get_block("pending")
        except Exception as exc:  # noqa: BLE001
            return {"error": redact(f"{type(exc).__name__}: {exc}", self.url)}
        return {
            "latest": int(latest["number"]),
            "pending": int(pending["number"]) if pending.get("number") is not None else None,
            "pending_ahead": (int(pending["number"]) - int(latest["number"]))
            if pending.get("number") is not None else None,
            "pending_txs": len(pending.get("transactions", [])),
        }


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Time the relayer's pre-broadcast RPC calls.")
    parser.add_argument("--chain", choices=sorted(CHAINS), default="base-sepolia")
    parser.add_argument("--n", type=int, default=50, help="recorded repetitions per endpoint")
    parser.add_argument("--warmup", type=int, default=3, help="unrecorded repetitions first")
    parser.add_argument("--pause", type=float, default=0.5, help="seconds between repetitions")
    parser.add_argument("--probes", type=int, default=10, help="Flashblocks pending-vs-latest probes")
    parser.add_argument("--from", dest="sender", default=None,
                        help="address for nonce/estimate (default GATEWAY_ADDRESS from the env file)")
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    suffix, chain_id, _, contract, channel = CHAINS[args.chain]
    sender = args.sender or read_env_file(args.env_file).get("GATEWAY_ADDRESS") or contract or (
        "0x" + "00" * 19 + "01")
    eps = []
    for lbl, url in endpoints(args.chain, args.env_file):
        try:
            eps.append(Endpoint(lbl, url, chain_id, sender, contract, channel, args.timeout))
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 — never let a traceback print the URL
            raise SystemExit(f"{lbl} ({rpc_host(url)}) unusable: {redact(f'{type(exc).__name__}: {exc}', url)}")
    for ep in eps:
        print(f"  {ep.label:<10} host {ep.host}  provider {ep.provider}", file=sys.stderr)
    if len(eps) == 1:
        print(f"  {suffix}_DEDICATED_RPC_URL is not set (environment or {args.env_file}): dedicated arm skipped, publicnode only", file=sys.stderr)

    for _ in range(args.warmup):
        for ep in eps:
            for name, fn in ep.calls():
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001 — redact before it can print
                    raise SystemExit(f"{ep.label} ({ep.host}) warm-up {name} failed: "
                                     f"{redact(f'{type(exc).__name__}: {exc}', ep.url)}")

    started = datetime.now(timezone.utc)
    samples: list[dict[str, Any]] = []
    for rep in range(1, args.n + 1):
        for ep in eps:
            total = 0.0
            ok = True
            for name, fn in ep.calls():
                t = time.perf_counter()
                err = None
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001 — a failed call is a sample, not a crash
                    ok, err = False, redact(f"{type(exc).__name__}: {exc}", ep.url)
                ms = (time.perf_counter() - t) * 1000
                total += ms
                samples.append({"endpoint": ep.label, "host": ep.host, "rep": rep, "call": name,
                                "ms": round(ms, 3), "ok": err is None, "error": err})
            samples.append({"endpoint": ep.label, "host": ep.host, "rep": rep, "call": "total4",
                            "ms": round(total, 3), "ok": ok, "error": None})
        time.sleep(args.pause)
    finished = datetime.now(timezone.utc)

    probes = {ep.label: [] for ep in eps}
    for _ in range(args.probes):
        for ep in eps:
            probes[ep.label].append(ep.flashblocks_probe())
        time.sleep(0.3)

    summary: dict[str, Any] = {
        "chain": args.chain, "chainId": chain_id, "commit": git_commit(),
        "startedUtc": started.isoformat(timespec="seconds"),
        "finishedUtc": finished.isoformat(timespec="seconds"),
        "n": args.n, "warmup": args.warmup, "pauseS": args.pause,
        "calls": CALLS, "connectionReuse": True, "interleaved": True,
        "endpoints": {},
    }
    print(f"\n  {'endpoint':<10} {'host':<36} {'call':<22}{'median':>9}{'p10':>9}{'p90':>9}{'fail':>6}")
    for ep in eps:
        rows = [s for s in samples if s["endpoint"] == ep.label]
        per: dict[str, Any] = {}
        for name in CALLS + ["total4"]:
            xs = [s["ms"] for s in rows if s["call"] == name and s["ok"]]
            fails = sum(1 for s in rows if s["call"] == name and not s["ok"])
            per[name] = {"median_ms": round(statistics.median(xs), 3) if xs else None,
                         "p10_ms": round(pct(xs, 0.1), 3), "p90_ms": round(pct(xs, 0.9), 3),
                         "mean_ms": round(statistics.mean(xs), 3) if xs else None,
                         "n": len(xs), "failed": fails}
            print(f"  {ep.label:<10} {ep.host:<36} {name:<22}{per[name]['median_ms'] or float('nan'):>9.1f}"
                  f"{per[name]['p10_ms']:>9.1f}{per[name]['p90_ms']:>9.1f}{fails:>6}")
        ok_probes = [p for p in probes[ep.label] if "error" not in p]
        ahead = [p["pending_ahead"] for p in ok_probes if p.get("pending_ahead") is not None]
        per["flashblocksProbe"] = {
            "probes": len(probes[ep.label]),
            "errors": len(probes[ep.label]) - len(ok_probes),
            "pendingAheadOfLatest": sum(1 for a in ahead if a > 0),
            "pendingAheadValues": sorted(set(ahead)),
            "pendingTxsMedian": statistics.median([p["pending_txs"] for p in ok_probes]) if ok_probes else None,
            "note": "heuristic: pending > latest suggests a Flashblocks-aware node",
        }
        summary["endpoints"][ep.label] = {"host": ep.host, "provider": ep.provider, **per}
        fp = per["flashblocksProbe"]
        print(f"  {ep.label:<10} flashblocks probe: pending ahead of latest in "
              f"{fp['pendingAheadOfLatest']}/{fp['probes'] - fp['errors']} (values {fp['pendingAheadValues']})")

    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"rpc_bench_{args.chain}_{started:%Y%m%dT%H%M%SZ}"
    with (args.out / f"{stem}.csv").open("x", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["endpoint", "host", "rep", "call", "ms", "ok", "error"])
        w.writeheader()
        w.writerows(samples)
    (args.out / f"{stem}.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {args.out / stem}.csv/.json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
