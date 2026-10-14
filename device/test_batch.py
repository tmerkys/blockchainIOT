"""run_batch.py record building — the M5 row contract between Pi and laptop."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent / "shared", _HERE.parent / "experiments"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from run_batch import (  # noqa: E402
    STATUS_ERROR,
    STATUS_NOT_SUBMITTED,
    STATUS_OK,
    STATUS_REVERTED,
    _ROUTE_DEV,
    build_record,
    classify,
)
from runner import RECORD_COLUMNS  # noqa: E402
from transport.http_client import DeviceMarks  # noqa: E402

T0_MONO = 1_000_000_000


def fake_signed():
    return SimpleNamespace(
        event=SimpleNamespace(t0_ns=1_700_000_000_000_000_000, t0_mono_ns=T0_MONO),
        t1_ns=1_700_000_000_015_000_000,
        signing_ns=15_000_000,
        voucher=SimpleNamespace(nonce=7, cumulativeAmount=123, serviceRef=b"\x11" * 32),
    )


def fake_marks():
    # send 20 ms after t0, reply 1500 ms after t0
    return DeviceMarks(
        t_send_ns=1_700_000_000_020_000_000,
        t_send_mono_ns=T0_MONO + 20_000_000,
        t4_dev_ns=1_700_000_001_500_000_000,
        t4_dev_mono_ns=T0_MONO + 1_500_000_000,
    )


def ok_result():
    base = 1_700_000_000.0
    return {
        "submitted": True, "reason": None, "txHash": "0xabc", "blockNumber": 5,
        "gasUsed": 75_029, "effectiveGasPrice": 6_000_000, "l1Fee": 18_000_000_000,
        "l1GasUsed": 5_300, "l1GasPrice": 1_000_000_000,
        "t2": base + 0.024, "t3": base + 0.700, "t4": base + 1.400, "t5": base + 1.450, "t6": base + 1.496,
    }


def test_classify():
    assert classify({"submitted": True, "reason": None}) == STATUS_OK
    assert classify({"submitted": False}) == STATUS_NOT_SUBMITTED
    # mined but reverted comes back as HTTP 200 — must not count as ok
    assert classify({"submitted": True, "reason": "settle() reverted"}) == STATUS_REVERTED


def test_record_shape_matches_runner_columns():
    full = build_record(run=1, iface="wlan0", signed=fake_signed(), marks=fake_marks(),
                        result=ok_result(), status=STATUS_OK)
    empty = build_record(run=2, iface=None, status=STATUS_ERROR, error="boom")
    assert list(full) == RECORD_COLUMNS
    assert list(empty) == RECORD_COLUMNS


def test_reported_latency_excludes_post_receipt_hold():
    r = build_record(run=1, iface="eth0", signed=fake_signed(), marks=fake_marks(),
                     result=ok_result(), status=STATUS_OK)
    # t4_dev - t0 = 1500 ms, minus the gateway's t6 - t4 = 96 ms
    assert abs(r["latency_ms"] - 1404.0) < 0.01
    assert r["signing_ms"] == 15.0
    assert r["nonce"] == 7 and r["cumulativeAmount"] == "123"


def test_failed_run_reports_no_latency():
    r = build_record(run=1, iface="eth0", signed=fake_signed(), marks=fake_marks(),
                     result={**ok_result(), "reason": "reverted"}, status=STATUS_REVERTED)
    assert r["latency_ms"] is None
    assert r["gasUsed"] == 75_029  # cost of a reverted tx is still real, keep it


def test_route_parse():
    out = "192.168.32.54 dev wlan0 src 192.168.32.64 uid 1000 \n    cache"
    assert _ROUTE_DEV.search(out).group(1) == "wlan0"
