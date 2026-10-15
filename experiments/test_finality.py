"""finality.py observer logic against a fake RPC and a fake clock."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from finality import (  # noqa: E402
    FIN_COLUMNS,
    CsvTail,
    Observer,
    RpcError,
    main,
    read_env_var,
    rpc_host,
    run,
)


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeRpc:
    """Chain whose tag heads and receipts the test sets; can be told to fail."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.heads = {"safe": 0, "finalized": 0}
        self.receipts: dict[str, int | None] = {}
        self.fail: set[str] = set()  # methods/tags that raise
        self.calls: list[tuple[str, list]] = []

    def call(self, method, params):
        self.calls.append((method, params))
        self.clock.t += 0.05  # each call takes 50 ms
        if method in self.fail or (params and params[0] in self.fail):
            raise RpcError(f"{method} boom")
        if method == "eth_getBlockByNumber":
            return {"number": hex(self.heads[params[0]])}
        if method == "eth_getTransactionReceipt":
            b = self.receipts.get(params[0])
            return None if b is None else {"blockNumber": hex(b)}
        raise AssertionError(method)


def runner_row(tx: str, block: int | str, nonce: int = 1, status: str = "ok") -> dict:
    return {
        "session": "S1", "chain": "anvil", "chainId": "31337", "transport": "eth",
        "contract": "0xC0", "channel": "0xCH", "nonce": str(nonce), "run": str(nonce),
        "status": status, "txHash": tx, "blockNumber": str(block), "t4": "999.0", "t5": "999.5",
        "latency_ms": "900",
    }


@pytest.fixture
def env():
    clock = Clock()
    rpc = FakeRpc(clock)
    rows: list[dict] = []
    obs = Observer(rpc, rows.append, poll_interval=2.0, timeout=100.0, host="h", clock=clock)
    return clock, rpc, rows, obs


def step(clock, obs, dt=2.0):
    obs.poll_once()
    clock.t += dt


def test_first_crossing_bracketed_and_finalized(env):
    clock, rpc, rows, obs = env
    rpc.receipts["0xaa"] = 10
    assert obs.add(runner_row("0xAA", 10))
    rpc.heads = {"safe": 5, "finalized": 1}
    step(clock, obs)
    t_before = clock.t
    rpc.heads = {"safe": 12, "finalized": 3}
    step(clock, obs)
    assert not rows
    rpc.heads = {"safe": 20, "finalized": 10}
    step(clock, obs)
    assert len(rows) == 1
    r = rows[0]
    assert set(r) == set(FIN_COLUMNS)
    assert r["status"] == "finalized"
    assert r["txHash"] == "0xaa"
    assert r["safeBlock"] == 12 and r["finalizedBlock"] == 10
    assert r["safeBound"] == r["finalizedBound"] == "bracketed"
    # safe crossed between the first and second poll
    assert float(r["t_safe_before"]) < t_before < float(r["t_safe"])
    assert float(r["t_finalized_before"]) < float(r["t_finalized"])
    assert r["receiptBlockAtFinal"] == 10
    assert r["pollInterval_s"] == 2.0
    assert r["session"] == "S1" and r["nonce"] == "1" and r["runnerStatus"] == "ok"
    assert not obs.pending


def test_already_past_is_upper_bound(env):
    clock, rpc, rows, obs = env
    rpc.receipts["0xbb"] = 3
    rpc.heads = {"safe": 50, "finalized": 40}
    obs.add(runner_row("0xbb", 3))
    step(clock, obs)
    assert rows[0]["safeBound"] == "upper" and rows[0]["t_safe_before"] == ""


def test_rows_without_tx_and_duplicates_ignored(env):
    _, _, _, obs = env
    assert not obs.add(runner_row("", ""))
    assert obs.add(runner_row("0x1", 5))
    assert not obs.add(runner_row("0x1", 5))
    assert len(obs.pending) == 1


def test_timeout_keeps_safe_and_writes_row(env):
    clock, rpc, rows, obs = env
    obs.add(runner_row("0xcc", 10))
    rpc.heads = {"safe": 11, "finalized": 2}
    for _ in range(60):
        step(clock, obs)
        if rows:
            break
    assert rows[0]["status"] == "timeout"
    assert rows[0]["t_safe"] and not rows[0]["t_finalized"]


def test_rpc_errors_counted_not_fatal(env):
    clock, rpc, rows, obs = env
    rpc.receipts["0xdd"] = 10
    obs.add(runner_row("0xdd", 10))
    rpc.fail = {"eth_getBlockByNumber"}
    step(clock, obs)
    step(clock, obs)
    assert not rows
    rpc.fail = set()
    rpc.heads = {"safe": 10, "finalized": 10}
    step(clock, obs)
    assert rows[0]["status"] == "finalized"
    assert rows[0]["rpcErrors"] == 4  # two tags x two failed polls
    assert rows[0]["error"] == ""


def test_one_tag_failing_does_not_block_other(env):
    clock, rpc, rows, obs = env
    obs.add(runner_row("0xee", 10))
    rpc.fail = {"finalized"}
    rpc.heads = {"safe": 10, "finalized": 10}
    step(clock, obs)
    assert obs.pending["0xee"].obs["safe"].get("t") is not None
    assert "t" not in obs.pending["0xee"].obs["finalized"]


def test_reorg_detected(env):
    clock, rpc, rows, obs = env
    rpc.receipts["0xff"] = 11  # receipt now in another block
    obs.add(runner_row("0xff", 10))
    rpc.heads = {"safe": 20, "finalized": 20}
    step(clock, obs)
    assert rows[0]["status"] == "reorged" and rows[0]["receiptBlockAtFinal"] == 11


def test_missing_block_resolved_from_receipt(env):
    clock, rpc, rows, obs = env
    obs.add(runner_row("0x77", ""))
    rpc.heads = {"safe": 30, "finalized": 30}
    step(clock, obs)  # no receipt yet
    assert not rows
    rpc.receipts["0x77"] = 25
    step(clock, obs)
    assert rows[0]["status"] == "finalized" and rows[0]["blockNumber"] == 25


def test_no_block_ever_times_out(env):
    clock, rpc, rows, obs = env
    obs.add(runner_row("0x88", ""))
    for _ in range(60):
        step(clock, obs)
    assert rows[0]["status"] == "no_block"


def test_tag_regression_counted_first_crossing_kept(env):
    clock, rpc, rows, obs = env
    rpc.receipts["0x55"] = 10
    obs.add(runner_row("0x55", 10))
    rpc.heads = {"safe": 12, "finalized": 8}
    step(clock, obs)
    t_safe = obs.pending["0x55"].obs["safe"]["t"]
    rpc.heads = {"safe": 9, "finalized": 6}  # a lagging backend answers
    step(clock, obs)
    rpc.heads = {"safe": 12, "finalized": 10}
    step(clock, obs)
    r = rows[0]
    assert r["tagRegressions"] == 2
    assert float(r["t_safe"]) == pytest.approx(t_safe)
    assert r["status"] == "finalized"


def test_abandon_writes_interrupted(env):
    _, _, rows, obs = env
    obs.add(runner_row("0x99", 4))
    obs.abandon_all()
    assert rows[0]["status"] == "interrupted"


# --- CSV tail ----------------------------------------------------------------


def write_rows(path: Path, rows: list[dict], header: bool) -> None:
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else [])
        if header:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def test_tail_incremental_and_partial_line(tmp_path):
    p = tmp_path / "m5.csv"
    tail = CsvTail(p)
    assert tail.new_rows(0) == []  # file not there yet
    write_rows(p, [runner_row("0x1", 1)], header=True)
    assert [r["txHash"] for r in tail.new_rows(1)] == ["0x1"]
    assert tail.new_rows(2) == []
    # half-written row: no trailing newline yet
    with p.open("a", encoding="utf-8") as fh:
        fh.write("S1,anvil,31337,eth,0xC0")
    assert tail.new_rows(3) == []
    with p.open("w", newline="", encoding="utf-8"):
        pass
    write_rows(p, [runner_row("0x1", 1), runner_row("0x2", 2, nonce=2)], header=True)
    assert [r["txHash"] for r in tail.new_rows(4)] == ["0x2"]
    assert tail.idle_for(10) == 6


def test_run_follows_until_expect_then_drains(tmp_path):
    clock = Clock()
    rpc = FakeRpc(clock)
    rows: list[dict] = []
    obs = Observer(rpc, rows.append, poll_interval=2.0, timeout=100.0, host="h", clock=clock)
    p = tmp_path / "m5.csv"
    write_rows(p, [runner_row("0x1", 5), runner_row("", "", nonce=2, status="error")], header=True)
    rpc.receipts.update({"0x1": 5, "0x3": 6})
    sleeps = []

    def sleep(dt):
        sleeps.append(dt)
        clock.t += dt
        if len(sleeps) == 2:  # runner writes its last row mid-observation
            write_rows(p, [runner_row("0x3", 6, nonce=3)], header=False)
        if len(sleeps) == 5:
            rpc.heads = {"safe": 10, "finalized": 10}

    run(obs, tail=CsvTail(p), initial=[], expect=3, idle=1e9, sleep=sleep)
    assert sorted(r["txHash"] for r in rows) == ["0x1", "0x3"]
    assert all(r["status"] == "finalized" for r in rows)
    assert set(sleeps) == {2.0}


def test_never_writes_followed_file(tmp_path, monkeypatch):
    """main() end to end against a fake RPC: output is a new fin_ file."""
    import finality

    clock = Clock()
    rpc = FakeRpc(clock)
    rpc.heads = {"safe": 100, "finalized": 100}
    rpc.receipts["0x1"] = 5

    def fake_call(method, params):
        if method == "eth_chainId":
            return hex(31337)
        return rpc.call(method, params)

    monkeypatch.setattr(finality.HttpRpc, "call", lambda self, m, p: fake_call(m, p))
    monkeypatch.setattr(finality.time, "sleep", lambda s: None)
    src = tmp_path / "m5_anvil_eth_X.csv"
    write_rows(src, [runner_row("0x1", 5)], header=True)
    before = src.read_bytes()
    out = tmp_path / "out"
    assert main(["--follow", str(src), "--rpc", "http://127.0.0.1:1/secret/path",
                 "--out-dir", str(out), "--expect", "1", "--poll", "0"]) == 0
    assert src.read_bytes() == before
    (fin,) = out.glob("fin_anvil_eth_*.csv")
    got = list(csv.DictReader(fin.open(encoding="utf-8")))
    assert got[0]["status"] == "finalized" and got[0]["rpcHost"] == "127.0.0.1"
    assert "secret" not in fin.read_text(encoding="utf-8")


def test_helpers(tmp_path):
    assert rpc_host("https://x.example.com/v2/KEY?k=1") == "x.example.com"
    e = tmp_path / ".env.x"
    e.write_text("FOO=1\nexport RPC_URL='https://a/b'\n", encoding="utf-8")
    assert read_env_var(e, "RPC_URL") == "https://a/b"
    assert read_env_var(e, "NOPE") is None
