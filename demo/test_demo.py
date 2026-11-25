"""Demo tests that need no chain: the decomposition and the server."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from demo.device_flow import decompose  # noqa: E402
from demo.modes import Mode  # noqa: E402
from device.transport.http_client import DeviceMarks  # noqa: E402


def test_decompose_keeps_every_span_on_one_clock():
    # Device: t0 at mono 0, reply landed at mono 1.2 s; gateway: t2..t6 on its own clock.
    t0_mono = 1_000_000_000
    marks = DeviceMarks(t_send_ns=0, t_send_mono_ns=t0_mono + 20_000_000,
                        t4_dev_ns=0, t4_dev_mono_ns=t0_mono + 1_200_000_000)
    gw = 5000.0  # an arbitrary epoch, to prove no device instant is subtracted from it
    result = {"t2": gw, "t3": gw + 0.7, "t4": gw + 0.9, "t5": gw + 1.0, "t6": gw + 1.15,
              "gasUsed": 77_000, "effectiveGasPrice": 6_000_000, "l1Fee": 300,
              "cumulativeAmount": 123, "txHash": "0xabc", "nonce": 5}
    signed = SimpleNamespace(event=SimpleNamespace(t0_mono_ns=t0_mono), signing_ns=20_000_000)
    d = decompose(result, signed, marks, signing_ms=13.0)
    assert round(d["build_ms"]) == 700 and round(d["inclusion_ms"]) == 200
    # latency = reply wait (1200) - gateway's post-receipt hold (t6 - t4 = 250)
    assert round(d["latency_ms"]) == 950
    # network = device RTT (1180) - gateway occupancy (t6 - t2 = 1150)
    assert round(d["network_ms"]) == 30
    assert d["fee_wei"] == str(77_000 * 6_000_000 + 300)
    assert d["signing_ms"] == 13.0


class FakeMode(Mode):
    """Stands in for a live mode so the server test spends no testnet gas."""

    def __init__(self, key: str) -> None:
        self.key = key

    def pay(self, emit) -> None:
        emit({"type": "tick", "kwh": 0.03})


def test_server_end_to_end(monkeypatch):
    from fastapi.testclient import TestClient

    from demo.server import Demo, create_app, parse_args

    monkeypatch.setattr(Demo, "make_mode", lambda self, key: FakeMode(key))
    app = create_app(parse_args([]))
    with TestClient(app) as client:
        for _ in range(50):
            if client.get("/api/state").json()["status"] == "ready":
                break
            time.sleep(0.1)
        state = client.get("/api/state").json()
        assert state["status"] == "ready" and state["info"]["mode"] == "pi"
        assert state["modes"] == ["pi", "laptop"]
        assert client.get("/").status_code == 200
        assert "latency" in client.get("/api/reference").json()
        assert client.post("/api/mode/replay").status_code == 400
        assert client.post("/api/mode/mainnet").status_code == 400
        assert client.post("/api/pay").status_code == 200


def test_open_refuses_clearly_when_the_funder_is_short():
    import pytest

    from demo.modes import LiveMode

    m = LiveMode("laptop", env_file=Path("unused.env"))
    m.deposits = {"demo": 9_000_000_000_000}
    m.funder = SimpleNamespace(address="0xFunder")
    opened = []
    m.chain = SimpleNamespace(balance=lambda a: 3_000_000_000_000, open=lambda *a: opened.append(a))
    m.runner = SimpleNamespace(address="0xDevice")
    with pytest.raises(RuntimeError, match="not enough testnet ETH"):
        m._open("demo", lambda e: None, "live")
    assert not opened


def test_a_channel_funded_by_someone_else_is_replaced():
    from demo.modes import LiveMode

    m = LiveMode("pi", env_file=Path("unused.env"))
    m.deposits = {"tick": 2_000_000_000_000}
    m.funder = SimpleNamespace(address="0xEvOwner")
    m.runner = SimpleNamespace(address="0xDevice")
    m.state = {"tick": {"channel": "0xOldChannel"}}
    m.chain = SimpleNamespace(channel=lambda c: {"device": "0xDevice", "closed": False, "deposit": 2_000_000_000_000,
                                                 "paid": 120_000_000_000, "nonce": 3, "funder": "0xGateway"})
    opened = []
    m._open = lambda which, emit, where: opened.append(which)
    m._ensure_channel("tick", lambda e: None, fresh=False)
    assert opened == ["tick"]
