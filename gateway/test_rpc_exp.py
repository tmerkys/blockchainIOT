"""exp/rpc gateway changes: host-only /health, redacted errors, receipt-definition fields."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gateway.api import create_app  # noqa: E402
from gateway.relayer import Relayer, RelayerConfig, RelayResult  # noqa: E402

KEY = "SeCrEtApIkEy0123456789"
URL = f"https://base-sepolia.g.alchemy.com/v2/{KEY}"
HOST = "base-sepolia.g.alchemy.com"
PAYMENT_CHANNEL = "0x00000000000000000000000000000000000000cD"
CHANNEL = "0x00000000000000000000000000000000000000AB"
GATEWAY = Account.create()


def _relayer(**overrides) -> Relayer:
    kwargs = dict(
        rpc_url=URL, chain_id=84532, payment_channel=PAYMENT_CHANNEL,
        gateway_private_key=GATEWAY.key.hex(),
    )
    kwargs.update(overrides)
    return Relayer(config=RelayerConfig(**kwargs))


@pytest.fixture
def client():
    with TestClient(create_app(_relayer()), raise_server_exceptions=False) as c:
        yield c


def test_from_env_reads_rpc_url_and_block_timestamp_switch():
    cfg = RelayerConfig.from_env({"RPC_URL": URL, "PAYMENT_CHANNEL_ADDRESS": PAYMENT_CHANNEL})
    assert cfg.rpc_url == URL and cfg.record_block_timestamp is True
    cfg = RelayerConfig.from_env({"RPC_URL": URL, "RECORD_BLOCK_TIMESTAMP": "0",
                                  "PAYMENT_CHANNEL_ADDRESS": PAYMENT_CHANNEL})
    assert cfg.record_block_timestamp is False


def test_health_exposes_host_not_url(client, monkeypatch):
    relayer = client.app.state.relayer

    def boom(self):
        raise ConnectionError(f"HTTPSConnectionPool(host='{HOST}', port=443): "
                              f"Max retries exceeded with url: /v2/{KEY}")

    monkeypatch.setattr(type(relayer.w3.eth), "chain_id", property(boom))
    resp = client.get("/health")
    body = resp.json()
    assert "rpcUrl" not in body
    assert body["rpcHost"] == HOST
    assert body["rpcReachable"] is False
    assert KEY not in resp.text
    assert HOST in body["rpcError"]


def test_not_ready_reason_is_redacted(client, monkeypatch):
    relayer = client.app.state.relayer

    def boom(*a, **k):
        raise ConnectionError(f"Max retries exceeded with url: /v2/{KEY}")

    monkeypatch.setattr(relayer._contract.functions, "channels", lambda addr: SimpleNamespace(call=boom))
    resp = client.get(f"/channel/{CHANNEL}")
    assert resp.status_code == 503
    assert KEY not in resp.text


def test_unhandled_error_is_redacted(client, monkeypatch, caplog):
    relayer = client.app.state.relayer

    def boom(channel):
        raise RuntimeError(f"requests failed for {URL}")

    monkeypatch.setattr(relayer, "channel_state", boom)
    resp = client.get(f"/channel/{CHANNEL}")
    assert resp.status_code == 500
    assert KEY not in resp.text
    assert HOST in resp.text
    assert all(KEY not in r.getMessage() for r in caplog.records)


class FakeEth:
    def __init__(self, heads, timestamp=1_792_000_000):
        self.heads = list(heads)
        self.timestamp = timestamp
        self.blocks_read = []

    @property
    def block_number(self):
        return self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]

    def get_block(self, n):
        self.blocks_read.append(n)
        return {"timestamp": self.timestamp, "number": n}


def _result(block=100):
    return RelayResult(accepted=True, submitted=True, channel=CHANNEL, nonce=1, cumulativeAmount=1,
                       serviceRef="0x" + "11" * 32, device=GATEWAY.address, calldata="0x",
                       blockNumber=block)


@pytest.mark.parametrize("heads,expected", [([99, 99, 100], 99), ([100], 100), ([101], 101)])
def test_head_at_receipt_is_the_first_poll(monkeypatch, heads, expected):
    relayer = _relayer()
    fake = FakeEth(heads)
    relayer.w3 = SimpleNamespace(eth=fake)
    monkeypatch.setattr("gateway.relayer.time.sleep", lambda s: None)
    result = _result()
    relayer._await_confirmations(100, result)
    assert result.headAtReceipt == expected
    assert result.t5 is not None


def test_block_timestamp_read_after_t5_and_switchable():
    relayer = _relayer()
    fake = FakeEth([100], timestamp=1_792_000_123)
    relayer.w3 = SimpleNamespace(eth=fake)
    result = _result(100)
    relayer._record_block_timestamp(result)
    assert result.blockTimestamp == 1_792_000_123 and fake.blocks_read == [100]

    off = _relayer(record_block_timestamp=False)
    fake2 = FakeEth([100])
    off.w3 = SimpleNamespace(eth=fake2)
    r2 = _result(100)
    off._record_block_timestamp(r2)
    assert r2.blockTimestamp is None and fake2.blocks_read == []


def test_block_timestamp_failure_leaves_none_and_logs_no_key(caplog):
    relayer = _relayer()

    def boom(n):
        raise ConnectionError(f"Max retries exceeded with url: /v2/{KEY}")

    relayer.w3 = SimpleNamespace(eth=SimpleNamespace(get_block=boom))
    result = _result(100)
    relayer._record_block_timestamp(result)
    assert result.blockTimestamp is None
    assert all(KEY not in r.getMessage() for r in caplog.records)


def test_relay_result_carries_rpc_host_only():
    assert _relayer().rpc_host == HOST
    assert "rpcHost" in RelayResult.model_fields
