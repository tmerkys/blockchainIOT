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
