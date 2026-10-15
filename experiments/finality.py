"""Time to finality: watch settled transactions until the chain calls them final.

    # alongside a live session (second terminal, started before or after runner.py):
    python experiments/finality.py --follow experiments/results/m5_<...>.csv
    python experiments/finality.py --follow-latest m5     # the newest m5_*.csv

    # after the fact, for transactions you already have:
    python experiments/finality.py --txs txs.csv --chain-id 84532

**What it measures.** Per settled transaction, the first time the chain's
`safe` and `finalized` block tags reach the block that holds it:

    t_safe       first poll at which eth_getBlockByNumber("safe").number      >= blockNumber
    t_finalized  first poll at which eth_getBlockByNumber("finalized").number >= blockNumber

Both are `time.time()` on the laptop — the clock the gateway stamps t3/t4/t5
with — so `t_safe − t4` and `t_finalized − t4` are single-clock durations with
no cross-machine term, and `latency_ms + (t_finalized − t4)` (device span +
gateway-clock span) is the time from the sensor event to finality, still free of
clock skew (thesis brief §1.4). *Run it on the gateway machine.*

What the tags mean: on Ethereum, `safe` is the latest justified checkpoint and
`finalized` the latest finalized one (Casper FFG, ~2 epochs ≈ 13 min). On Base
(OP Stack) `safe` is the head derived from batch data already included on L1,
and `finalized` the head derived from L1 blocks that are themselves finalized —
so Base finality is L1 finality plus the batcher's posting delay.

**Resolution.** A tag is only seen when it is polled, so every first observation
is late by up to one poll interval (plus the RPC round trip, recorded per row as
`*_rttMs`). Each row carries the poll interval and, per tag, `t_*_before` — the
last poll at which the tag was still *below* the block — so the crossing lies in
`(t_*_before, t_*]`. If `t_*_before` is empty the tag had already passed the
block on the first poll after the transaction was added: `t_*` is then only an
upper bound, and the row says so in `safeBound` / `finalizedBound` (`upper`
instead of `bracketed`). Tags advance in jumps (an epoch on L1, a batch on Base),
so a 2 s poll is fine-grained against what it measures. Behind a load balancer
whose backends disagree (`tagRegressions` > 0), `t_*_before` is "below, according
to the backend that answered", so the bracket is only as good as the slowest
backend; `t_*` — the first time *any* answer reached the block — is unaffected.

**Never writes into the runner's CSV.** The observed file is opened read-only
and re-read whole on each poll (sessions are ~30 rows); a trailing line without
its newline is a row still being written and is left for the next poll. Output
is its own file, created exclusively (`fin_<chain>_<transport>_<ts>-<id>.csv`):
header first, then one row per transaction, flushed as each resolves —
`finalized`, `timeout`, `reorged`, `no_block` or `interrupted` in `status`. An
observer killed with Ctrl-C still writes a row for everything it was tracking.
RPC errors are counted per row (`rpcErrors`) and retried on the next poll; they
never end the run.

Joinable to the runner row it observes on (chainId, contract, channel, nonce,
txHash); `session` is the observed CSV's session id.

**When it stops.** Following a CSV, it keeps reading new rows until the file has
not grown for `--idle` seconds (default 600: longer than the runner's 150 s
per-run timeout plus a gap) or `--expect N` rows have arrived; then it keeps
polling until every transaction is resolved. A transaction is given up
(`timeout`) `--timeout` seconds after it was added. Defaults per chain come from
what the publicnode endpoints showed on 2026-10-15, 16:00-16:19 UTC (read-only):
Ethereum Sepolia `safe` 384-768 s and `finalized` 768-1152 s behind latest
(sawtooth, one 32-slot epoch per step); Base Sepolia `safe` 60-162 s, and
`finalized` 854-1110 s on the leading backend (advancing ~190 blocks once per L1
epoch) but 1216-1460 s on a second backend one step behind it.

**RPC.** `--rpc URL`, else `RPC_URL` from `--rpc-env FILE` (e.g. `.env.sepolia`,
the gateway's own L1 endpoint), else `$FINALITY_RPC_URL`, else the publicnode
endpoint for the chain. Only the host is ever printed or recorded — a provider
key in the URL path never reaches the CSV.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "experiments" / "results"

CHAINS = {31337: "anvil", 84532: "base-sepolia", 11155111: "sepolia"}
DEFAULT_RPC = {
    84532: "https://base-sepolia-rpc.publicnode.com",
    11155111: "https://ethereum-sepolia-rpc.publicnode.com",
    31337: "http://127.0.0.1:8545",
}
# Seconds after a transaction is added before it is given up. Observed lag of
# `finalized` behind `latest` on 2026-10-15: at most ~19 min (Sepolia) and ~24 min
# (Base Sepolia, lagging backend). 60 min leaves room for a slow epoch or batch.
DEFAULT_TIMEOUT = {11155111: 3600.0, 84532: 3600.0, 31337: 300.0}
DEFAULT_TIMEOUT_OTHER = 3600.0
DEFAULT_POLL = 2.0
DEFAULT_IDLE = 600.0

TAGS = ("safe", "finalized")

FIN_COLUMNS = [
    # key + context, copied from the observed row
    "session", "chain", "chainId", "transport", "contract", "channel", "nonce", "run",
    "runnerStatus", "txHash", "blockNumber", "t4", "t5",
    # observer
    "observer", "rpcHost", "pollInterval_s", "addedAt",
    "t_safe_before", "t_safe", "safeBlock", "safe_rttMs", "safeBound",
    "t_finalized_before", "t_finalized", "finalizedBlock", "finalized_rttMs", "finalizedBound",
    "receiptBlockAtFinal", "status", "error", "rpcErrors", "tagRegressions",
]
"""`safeBlock` / `finalizedBlock`: the tag's block number at the first poll that
reached the transaction. `receiptBlockAtFinal`: the receipt's block re-read once
the transaction is finalized (a mismatch is `reorged`). `tagRegressions`: polls,
while this transaction was pending, at which a tag came back *lower* than the
highest value seen before — a load-balanced endpoint answering from backends at
different heights (seen on Base Sepolia publicnode's `finalized`, 2026-10-15).
The first observation is still valid (some node had finalized that far); the
count says how noisy the endpoint was."""

KEY = ("chainId", "contract", "channel", "nonce", "txHash")


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --- RPC -------------------------------------------------------------------


class RpcError(Exception):
    pass


class Rpc(Protocol):
    def call(self, method: str, params: list[Any]) -> Any: ...


class HttpRpc:
    """Minimal JSON-RPC over HTTP: read-only calls only, no web3 needed."""

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout
        self._id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}).encode()
        req = urllib.request.Request(
            self.url, body, {"content-type": "application/json", "user-agent": "magis-finality"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.load(resp)
        except Exception as exc:  # noqa: BLE001 — network errors are retried by the caller
            raise RpcError(f"{method}: {type(exc).__name__}: {exc}") from exc
        if payload.get("error") is not None:
            raise RpcError(f"{method}: {payload['error']}")
        return payload.get("result")


def rpc_host(url: str) -> str:
    """Host only: never record a path or query, where provider keys live."""
    return urllib.parse.urlsplit(url).hostname or "?"


def read_env_var(path: Path, name: str) -> str | None:
    """`NAME=value` from a dotenv file, without echoing anything."""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip("'\"") or None
    return None


def resolve_rpc(chain_id: int, cli: str | None, env_file: Path | None) -> str:
    if cli:
        return cli
    if env_file is not None:
        url = read_env_var(env_file, "RPC_URL")
        if not url:
            raise SystemExit(f"{env_file} has no RPC_URL")
        return url
    if os.environ.get("FINALITY_RPC_URL"):
        return os.environ["FINALITY_RPC_URL"]
    if chain_id in DEFAULT_RPC:
        return DEFAULT_RPC[chain_id]
    raise SystemExit(f"no default RPC for chain {chain_id}: pass --rpc")


# --- tracking ----------------------------------------------------------------


@dataclass
class Tracked:
    row: dict[str, Any]  # FIN_COLUMNS context, copied from the source row
    block: int | None
    added_at: float
    deadline: float
    obs: dict[str, dict[str, Any]] = field(default_factory=lambda: {t: {} for t in TAGS})
    """per tag: before (last time seen below), t, block, rtt_ms"""
    rpc_errors: int = 0
    regressions: int = 0
    error: str = ""

    @property
    def tx(self) -> str:
        return str(self.row["txHash"])


class Observer:
    """Polls `safe` / `finalized` and stamps, per transaction, when each reaches it.

    Pure logic plus an injected RPC and clock, so tests drive it with fakes.
    `write` receives one finished row (a dict over FIN_COLUMNS) per transaction.
    """

    def __init__(
        self,
        rpc: Rpc,
        write: Callable[[dict[str, Any]], None],
        *,
        poll_interval: float,
        timeout: float,
        host: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.rpc = rpc
        self.write = write
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.host = host
        self.clock = clock
        self.pending: dict[str, Tracked] = {}
        self.seen: set[str] = set()
        self.written = 0
        self.max_seen: dict[str, int] = {}
        self.regressions = 0

    # -- input

    def add(self, src: dict[str, Any]) -> bool:
        """Track one source row; False if it has no transaction or is a duplicate."""
        tx = (src.get("txHash") or "").strip().lower()
        if not tx or tx in self.seen:
            return False
        self.seen.add(tx)
        now = self.clock()
        row = {c: src.get(c, "") for c in FIN_COLUMNS}
        row["txHash"] = tx
        row["runnerStatus"] = src.get("status", src.get("runnerStatus", ""))
        block = _int_or_none(src.get("blockNumber"))
        self.pending[tx] = Tracked(row=row, block=block, added_at=now, deadline=now + self.timeout)
        return True

    # -- one poll

    def _block_number(self, tag: str) -> tuple[int, float, float]:
        sent = self.clock()
        block = self.rpc.call("eth_getBlockByNumber", [tag, False])
        got = self.clock()
        if not block or block.get("number") is None:
            raise RpcError(f"tag {tag!r} returned no block (unsupported?)")
        return int(block["number"], 16), got, (got - sent) * 1000

    def _receipt_block(self, tx: str) -> int | None:
        receipt = self.rpc.call("eth_getTransactionReceipt", [tx])
        if not receipt or receipt.get("blockNumber") is None:
            return None
        return int(receipt["blockNumber"], 16)

    def poll_once(self) -> None:
        if not self.pending:
            return
        # Transactions recorded without a block (gateway timed out on the receipt).
        for t in self.pending.values():
            if t.block is None:
                try:
                    t.block = self._receipt_block(t.tx)
                except RpcError as exc:
                    t.rpc_errors += 1
                    t.error = str(exc)[:200]

        heads: dict[str, tuple[int, float, float]] = {}
        for tag in TAGS:
            try:
                heads[tag] = self._block_number(tag)
                num = heads[tag][0]
                if num < self.max_seen.get(tag, -1):
                    self.regressions += 1
                    for t in self.pending.values():
                        t.regressions += 1
                self.max_seen[tag] = max(num, self.max_seen.get(tag, -1))
            except RpcError as exc:
                for t in self.pending.values():
                    t.rpc_errors += 1
                    t.error = str(exc)[:200]

        for t in list(self.pending.values()):
            if t.block is not None:
                for tag, (num, at, rtt) in heads.items():
                    o = t.obs[tag]
                    if "t" in o:
                        continue
                    if num >= t.block:
                        o.update(t=at, block=num, rtt_ms=rtt)
                    else:
                        o["before"] = at
            if "t" in t.obs["finalized"]:
                self._finish_finalized(t)
            elif self.clock() >= t.deadline:
                self._finish(t, "timeout" if t.block is not None else "no_block")

    def _finish_finalized(self, t: Tracked) -> None:
        # The block is final; confirm the transaction is still in it.
        try:
            final = self._receipt_block(t.tx)
        except RpcError as exc:
            t.rpc_errors += 1
            t.error = str(exc)[:200]
            if self.clock() >= t.deadline:
                self._finish(t, "finalized", receipt_block=None)
            return  # retry the receipt on the next poll
        if final == t.block:
            self._finish(t, "finalized", receipt_block=final)
        else:
            self._finish(t, "reorged", receipt_block=final)

    def _finish(self, t: Tracked, status: str, receipt_block: int | None = None) -> None:
        row = dict(t.row)
        row.update(
            observer="finality.py",
            rpcHost=self.host,
            pollInterval_s=self.poll_interval,
            addedAt=f"{t.added_at:.6f}",
            blockNumber=t.block if t.block is not None else row.get("blockNumber", ""),
            receiptBlockAtFinal="" if receipt_block is None else receipt_block,
            status=status,
            error="" if status == "finalized" else t.error,
            rpcErrors=t.rpc_errors,
            tagRegressions=t.regressions,
        )
        for tag in TAGS:
            o = t.obs[tag]
            row[f"t_{tag}_before"] = f"{o['before']:.6f}" if "before" in o else ""
            row[f"t_{tag}"] = f"{o['t']:.6f}" if "t" in o else ""
            row[f"{tag}Block"] = o.get("block", "")
            row[f"{tag}_rttMs"] = f"{o['rtt_ms']:.1f}" if "rtt_ms" in o else ""
            row[f"{tag}Bound"] = ("bracketed" if "before" in o else "upper") if "t" in o else ""
        self.write(row)
        self.written += 1
        del self.pending[t.tx]

    def abandon_all(self, status: str = "interrupted") -> None:
        for t in list(self.pending.values()):
            self._finish(t, status)


def _int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(float(s)) if not s.startswith("0x") else int(s, 16)
    except ValueError:
        return None


# --- sources ---------------------------------------------------------------


class CsvTail:
    """Rows appended to a CSV another process is writing. Read-only, re-read whole.

    Only complete records count: a file that does not end in a newline has a row
    mid-write, which is left for the next read. Rows whose field count does not
    match the header are deferred the same way.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.consumed = 0
        self.last_size = -1
        self.last_growth: float | None = None

    def new_rows(self, now: float) -> list[dict[str, str]]:
        try:
            with self.path.open("r", newline="", encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            return []
        if len(text) != self.last_size:
            self.last_size = len(text)
            self.last_growth = now
        if not text.endswith("\n"):
            text = text[: text.rfind("\n") + 1]
        records = list(csv.reader(text.splitlines(keepends=True)))
        if not records:
            return []
        header, body = records[0], records[1:]
        out = []
        for rec in body[self.consumed:]:
            if len(rec) != len(header):
                break
            out.append(dict(zip(header, rec)))
        self.consumed += len(out)
        return out

    def idle_for(self, now: float) -> float:
        return 0.0 if self.last_growth is None else now - self.last_growth


def newest(prefix: str, results_dir: Path) -> Path:
    files = sorted(glob.glob(str(results_dir / f"{prefix}_*.csv")), key=os.path.getmtime)
    if not files:
        raise SystemExit(f"no {prefix}_*.csv in {results_dir}")
    return Path(files[-1])


def peek_first_row(path: Path, wait: float, poll: float) -> dict[str, str]:
    """The first data row (for chain/transport/session), waiting for it to land."""
    end = time.time() + wait
    while True:
        rows = CsvTail(path).new_rows(time.time())
        if rows:
            return rows[0]
        if time.time() >= end:
            raise SystemExit(f"{path} has no complete data row after {wait:.0f} s")
        time.sleep(poll)


# --- main ------------------------------------------------------------------


def output_path(out_dir: Path, chain: str, transport: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return out_dir / f"fin_{chain}_{transport}_{stamp}-{uuid.uuid4().hex[:6]}.csv"


def run(
    observer: Observer,
    *,
    tail: CsvTail | None,
    initial: Iterable[dict[str, Any]],
    expect: int | None,
    idle: float,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    for r in initial:
        observer.add(r)
    following = tail is not None
    rows_in = 0
    try:
        while True:
            now = observer.clock()
            if following:
                assert tail is not None
                for r in tail.new_rows(now):
                    rows_in += 1
                    if observer.add(r):
                        say(f"  + {r.get('txHash', '')[:12]}... block {r.get('blockNumber')} (run {r.get('run', '?')})")
                if (expect is not None and rows_in >= expect) or tail.idle_for(now) >= idle:
                    following = False
                    say(f"  stopped following ({rows_in} rows read); {len(observer.pending)} pending")
            before = observer.written
            observer.poll_once()
            if observer.written != before:
                say(f"  resolved {observer.written - before}; {len(observer.pending)} pending")
            if not following and not observer.pending:
                return
            sleep(observer.poll_interval)
    except KeyboardInterrupt:
        say("  interrupted: writing a row for every pending transaction")
        observer.abandon_all("interrupted")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Time to safe/finalized per settled transaction.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--follow", type=Path, help="runner CSV to tail (live or finished)")
    src.add_argument("--follow-latest", metavar="PREFIX", help="tail the newest <PREFIX>_*.csv in --results-dir")
    src.add_argument("--txs", type=Path, help="CSV with at least txHash (blockNumber optional)")
    p.add_argument("--chain-id", type=int, help="required with --txs")
    p.add_argument("--transport", default="na", help="file-name label with --txs")
    p.add_argument("--rpc", help="JSON-RPC URL (default: publicnode for the chain)")
    p.add_argument("--rpc-env", type=Path, help="read RPC_URL from this dotenv file, e.g. .env.sepolia")
    p.add_argument("--poll", type=float, default=DEFAULT_POLL, help="seconds between polls = resolution")
    p.add_argument("--timeout", type=float, default=None, help="seconds per tx (default: per chain)")
    p.add_argument("--idle", type=float, default=DEFAULT_IDLE, help="stop following after this long without growth")
    p.add_argument("--expect", type=int, default=None, help="stop following after this many rows")
    p.add_argument("--wait-first", type=float, default=900.0, help="seconds to wait for the first row")
    p.add_argument("--results-dir", type=Path, default=RESULTS_DIR, help="where --follow-latest looks")
    p.add_argument("--out-dir", type=Path, default=RESULTS_DIR, help="where the fin_ CSV goes")
    args = p.parse_args(argv)

    tail: CsvTail | None = None
    initial: list[dict[str, Any]] = []
    if args.txs is not None:
        if args.chain_id is None:
            p.error("--txs needs --chain-id")
        with args.txs.open(newline="", encoding="utf-8") as fh:
            initial = [
                {**r, "chainId": r.get("chainId") or args.chain_id,
                 "chain": r.get("chain") or CHAINS.get(args.chain_id, str(args.chain_id)),
                 "transport": r.get("transport") or args.transport}
                for r in csv.DictReader(fh)
            ]
        chain_id, transport = args.chain_id, args.transport
    else:
        path = args.follow if args.follow is not None else newest(args.follow_latest, args.results_dir)
        if path.name.startswith("fin_"):
            raise SystemExit("refusing to follow a fin_ file: point --follow at the runner's CSV")
        say(f"following {path}")
        first = peek_first_row(path, args.wait_first, args.poll)
        chain_id, transport = int(first["chainId"]), first.get("transport") or "na"
        if args.chain_id is not None and args.chain_id != chain_id:
            raise SystemExit(f"--chain-id {args.chain_id} but {path.name} is on {chain_id}")
        tail = CsvTail(path)

    url = resolve_rpc(chain_id, args.rpc, args.rpc_env)
    rpc = HttpRpc(url)
    host = rpc_host(url)
    actual = int(rpc.call("eth_chainId", []), 16)
    if actual != chain_id:
        raise SystemExit(f"RPC at {host} is chain {actual}, transactions are on {chain_id}")
    for tag in TAGS:  # fail now, not after an hour, if the node lacks a tag
        if not rpc.call("eth_getBlockByNumber", [tag, False]):
            raise SystemExit(f"RPC at {host} does not serve the {tag!r} tag")

    timeout = args.timeout if args.timeout is not None else DEFAULT_TIMEOUT.get(chain_id, DEFAULT_TIMEOUT_OTHER)
    chain = CHAINS.get(chain_id, str(chain_id))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = output_path(args.out_dir, chain, transport)
    say(f"chain {chain} via {host}, poll {args.poll} s, timeout {timeout:.0f} s/tx -> {out}")

    with out.open("x", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIN_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        fh.flush()

        def write(row: dict[str, Any]) -> None:
            writer.writerow(row)
            fh.flush()

        observer = Observer(rpc, write, poll_interval=args.poll, timeout=timeout, host=host)
        run(observer, tail=tail, initial=initial, expect=args.expect, idle=args.idle)
    say(f"wrote {out} ({observer.written} rows; tag regressions seen: {observer.regressions})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
