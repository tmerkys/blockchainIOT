"""The load driver's CSV columns must match what device/run_load.py emits.

concurrency.py refuses a row whose keys differ from RECORD_COLUMNS, so a
column added to run_batch.build_record (as exp/rpc did) without updating the
load driver would abort a live session at its first row. Checked here instead.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "device", ROOT / "shared", ROOT / "experiments", ROOT / "gateway"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import concurrency  # noqa: E402
import run_load  # noqa: E402
from run_batch import build_record  # noqa: E402


def test_load_record_shape_matches_driver_columns():
    record = build_record(run=1, iface="wlan0", status="error", error="x", cpu={})
    record.update({"device": 0, "deviceAddress": None, "channel": None, "closed": False})
    for key in run_load.LOAD_FIELDS:
        record[key] = None
    assert set(record) == set(concurrency.RECORD_COLUMNS)


def _rows(session, host, t0, block0, n, status="ok"):
    return [{
        "chain": "base-sepolia", "transport": "wifi", "deviceHost": "pi", "pipeline": True,
        "level": 8, "session": session, "status": status, "rpcHost": host if status == "ok" else None,
        "t0_wall": t0 + i, "latency_ms": 900.0, "t4": t0 + i + 0.9, "t5": t0 + i + 2.0,
        "blockNumber": block0 + i // 2, "gapBehind": None, "nonceResyncs": 0, "inflight": 2,
        "commit": "abcdef01",
    } for i in range(n)]


def test_sessions_of_one_level_are_not_pooled():
    """Two K=8 sessions a day apart on different providers stay two rows, so the
    throughput span never stretches across them."""
    import pandas as pd

    df = pd.DataFrame(_rows("A", "x.alchemy.com", 1000.0, 10, 8)
                      + _rows("A", None, 1009.0, 0, 2, status="rpc_error")
                      + _rows("B", "y.publicnode.com", 90_000.0, 50_000, 8))
    s = concurrency.summarise_levels(df).set_index("session")
    assert list(s.index) == ["A", "B"]
    assert s.loc["A", "rpcHost"] == "x.alchemy.com" and s.loc["A", "attempted"] == 10
    assert s.loc["B", "rpcHost"] == "y.publicnode.com"
    assert (s["span_s"] < 20).all() and (s["settles_per_block"] == 2).all()
