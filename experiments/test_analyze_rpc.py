"""analyze.py exp/rpc additions: provider dimension, t4_preconf flag, rpc outputs."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import analyze  # noqa: E402

RESULTS = _HERE / "results"


def _session(name, n, *, host=None, preconf=True, latency=950.0, build=700.0, seed=0,
             offset_ms=5.0, with_cols=True):
    """Synthetic M5 rows. preconf: receipt 1.2 s before the block timestamp and
    head one block behind; else receipt 0.4 s after and head at the block."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        t2 = 1_792_000_000.0 + 10 * i
        t3 = t2 + (build + rng.normal(0, 30)) / 1000
        t4 = t3 + 0.25
        block_ts = round(t4 + (1.2 if preconf else -0.4) - offset_ms / 1000)  # chain clock ≈ NTP
        t4 = block_ts - ((1.2 if preconf else -0.4)) + offset_ms / 1000       # keep lead exact
        t5 = t4 + (1.6 if preconf else 0.05)
        row = {
            "session": name, "transport": "wifi", "chain": "base-sepolia", "chainId": 84532,
            "contract": "0xc", "channel": "0xa", "commit": "abcdef0" + name, "piCommit": "x",
            "seed": 1, "startedUtc": "2026-10-16", "wifiPowersave": "disable", "cpuGovernor": "performance",
            "run": i + 1, "status": "ok", "error": None, "iface": "wlan0",
            "t0_wall": t2 - 0.03, "t1_wall": t2 - 0.02, "signing_ms": 13.5, "rtt_ms": 2000,
            "latency_ms": latency + rng.normal(0, 40), "network_ms": 13.0, "clock_offset_ms": offset_ms,
            "t2": t2, "t3": t3, "t4": t4, "t5": t5, "t6": t5 + 0.01,
            "nonce": i, "cumulativeAmount": "1", "serviceRef": "0x", "txHash": "0x", "blockNumber": 1000 + i,
            "gasUsed": 77_000, "effectiveGasPrice": 6_000_000, "l1Fee": 1e9, "l1GasUsed": 1, "l1GasPrice": 1,
            "gatewayReason": None, "cpu_sign_ms": 13.5, "cpu_countersign_ms": 13.2, "cpu_device_ms": 16.1,
        }
        if with_cols:
            row.update({"rpcHost": host, "blockTimestamp": block_ts,
                        "headAtReceipt": 1000 + i - (1 if preconf else 0)})
        rows.append(row)
    return pd.DataFrame(rows)


def _write(tmp: Path, frames: dict[str, pd.DataFrame]) -> Path:
    d = tmp / "results"
    d.mkdir(parents=True)
    for name, f in frames.items():
        f.to_csv(d / f"m5_base-sepolia_wifi_{name}.csv", index=False)
    return d


def test_flag_rules():
    df = _session("s", 4, host="h.publicnode.com", preconf=True)
    df = pd.concat([df, _session("t", 4, host="x.alchemy.com", preconf=False)], ignore_index=True)
    out = analyze.add_rpc_columns(df)
    assert out.loc[out.session == "s", "t4_preconf"].tolist() == [True] * 4
    assert out.loc[out.session == "t", "t4_preconf"].tolist() == [False] * 4
    assert (out.loc[out.session == "s", "t4_lead_ms"].round() == 1200).all()

    # head check alone decides when there is no timestamp
    d = df.copy()
    d["blockTimestamp"] = np.nan
    assert analyze.add_rpc_columns(d)["t4_preconf"].tolist() == [True] * 4 + [False] * 4
    # timestamp alone decides beyond the margin
    d = df.copy()
    d["headAtReceipt"] = np.nan
    assert analyze.add_rpc_columns(d)["t4_preconf"].tolist() == [True] * 4 + [False] * 4
    # head says pre-confirmed (lagging pool) but timestamp says well after: unknown
    d = _session("u", 3, host="x.alchemy.com", preconf=False)
    d["headAtReceipt"] = d["blockNumber"] - 1
    assert analyze.add_rpc_columns(d)["t4_preconf"].isna().all()
    # failed runs carry no flag
    d = df.copy()
    d.loc[0, "status"] = "refused"
    assert pd.isna(analyze.add_rpc_columns(d).loc[0, "t4_preconf"])


def test_clock_offset_correction_keeps_flag_robust():
    # laptop 0.48 s fast (the 2026-10-15 case): raw lead would be 1.2-0.48 = 0.72 s,
    # under the 0.8 s raw margin; the session offset brings it back to 1.2 s.
    df = _session("s", 5, host="h.publicnode.com", preconf=True, offset_ms=480.0)
    out = analyze.add_rpc_columns(df)
    assert (out["t4_lead_ms"].round() == 1200).all()
    d = df.copy()
    d["headAtReceipt"] = np.nan
    assert analyze.add_rpc_columns(d)["t4_preconf"].all()


def test_legacy_rows_are_publicnode_and_unflagged():
    df = analyze.add_rpc_columns(_session("old", 3, with_cols=False))
    assert (df["provider"] == "publicnode").all()
    assert not df["rpc_instrumented"].any()
    assert df["t4_preconf"].isna().all()


@pytest.mark.skipif(not RESULTS.exists(), reason="no committed results")
def test_real_data_produces_no_rpc_output_and_main_tables_unchanged(tmp_path):
    df = analyze.load(RESULTS)
    assert (df["provider"] == "publicnode").all()
    assert len(analyze.baseline_rows(df)) == len(df)
    assert analyze.rpc_groups(df) == []
    analyze.main(["--no-figures", "--out", str(tmp_path)])
    assert not (tmp_path / "rpc_summary.csv").exists()
    assert not (tmp_path / "rpc_tests.csv").exists()


def test_dedicated_rows_stay_out_of_main_tables(tmp_path):
    legacy = _session("old", 30, with_cols=False, seed=1)
    pn = _session("pn", 30, host="base-sepolia-rpc.publicnode.com", seed=2)
    ded = _session("ded", 30, host="base-sepolia.g.alchemy.com", build=300.0, latency=600.0, seed=3)

    only_base = _write(tmp_path / "a", {"old": legacy, "pn": pn})
    with_ded = _write(tmp_path / "b", {"old": legacy, "pn": pn, "ded": ded})

    analyze.main(["--no-figures", "--results", str(only_base), "--out", str(tmp_path / "oa")])
    analyze.main(["--no-figures", "--results", str(with_ded), "--out", str(tmp_path / "ob")])
    for name in ("summary.csv", "tests.csv"):
        assert (tmp_path / "oa" / name).read_bytes() == (tmp_path / "ob" / name).read_bytes()
    assert not (tmp_path / "oa" / "rpc_summary.csv").exists()

    rsum = pd.read_csv(tmp_path / "ob" / "rpc_summary.csv")
    rtests = pd.read_csv(tmp_path / "ob" / "rpc_tests.csv")
    # baseline side is the instrumented same-day session only, not the legacy rows
    pn_rows = rsum[(rsum.provider == "publicnode") & (rsum.metric == "build_ms")]
    assert int(pn_rows["n"].iloc[0]) == 30 and pn_rows["hosts"].iloc[0] == "base-sepolia-rpc.publicnode.com"
    assert set(rtests["metric"]) == {"sealed_ms", "build_ms", "latency_ms", "inclusion_ms"}
    assert (rtests["t4_definition"] == "preconf").all()
    build = rtests[rtests.metric == "build_ms"].iloc[0]
    assert build.a == "publicnode" and build.b == "alchemy" and build.diff_b_minus_a < -300
    assert "KEY" not in (tmp_path / "ob" / "rpc_summary.csv").read_text()


def test_t4_latency_not_compared_when_definitions_differ(tmp_path):
    pn = _session("pn", 30, host="base-sepolia-rpc.publicnode.com", seed=2)
    ded = _session("ded", 30, host="base-sepolia.g.alchemy.com", preconf=False, seed=3)
    res = _write(tmp_path / "r", {"pn": pn, "ded": ded})
    analyze.main(["--no-figures", "--results", str(res), "--out", str(tmp_path / "o")])
    rtests = pd.read_csv(tmp_path / "o" / "rpc_tests.csv")
    assert set(rtests["metric"]) == {"sealed_ms", "build_ms"}
    assert (rtests["t4_definition"] == "mixed").all()
