"""Pipelining mode (exp/concurrency): nonce manager, failure statuses, API.

Chain-free: the relayer's `w3` is replaced by a small fake that records what
was sent and can be told to fail. The default (non-pipelined) path is covered
by test_relayer.py / test_api.py, which must keep passing unchanged.
"""

import sys
import threading
import time
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak
from fastapi.testclient import TestClient
from hexbytes import HexBytes
from web3.exceptions import TimeExhausted

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gateway import relayer as R  # noqa: E402
from gateway.api import create_app  # noqa: E402
from gateway.relayer import (  # noqa: E402
    NonceManager,
    PipelinedResult,
    Relayer,
    RelayerConfig,
    RelayResult,
    classify_send_error,
)
from shared.voucher import Voucher  # noqa: E402

CHAIN_ID = 84532
PAYMENT_CHANNEL = "0x00000000000000000000000000000000000000cD"
CHANNEL = "0x00000000000000000000000000000000000000AB"
DEVICE = Account.create()
MERCHANT = Account.create()
GATEWAY = Account.create()


class RpcError(Exception):
    pass


class FakeEth:
    def __init__(self, pending: int = 5) -> None:
        self.pending = pending
        self.sent: list[int] = []
        self.fail_send: dict[int, Exception] = {}
        self.no_receipt: set[int] = set()
        self.send_delay = 0.0
        self.block_number = 100
        self.max_priority_fee = 1
        self._lock = threading.Lock()
        self._by_hash: dict[bytes, int] = {}
        self.count_calls: list[str] = []
        self.estimate_nonces: list = []

    def estimate_gas(self, tx):
        self.estimate_nonces.append(tx.get("nonce"))
        return 80_000

    def get_block(self, which):
        return {"baseFeePerGas": 10}

    def get_transaction_count(self, address, block="latest"):
        self.count_calls.append(block)
        return self.pending

    def send_raw_transaction(self, raw):
        nonce = _decode_nonce(raw)
        if self.send_delay:
            time.sleep(self.send_delay)
        if nonce in self.fail_send:
            raise self.fail_send.pop(nonce)
        with self._lock:
            self.sent.append(nonce)
            h = keccak(raw)
            self._by_hash[h] = nonce
        return h

    def wait_for_transaction_receipt(self, tx_hash, timeout):
        nonce = self._by_hash[bytes(tx_hash)]
        if nonce in self.no_receipt:
            raise TimeExhausted("not in a block")
        return {"blockNumber": 100, "gasUsed": 77_000, "effectiveGasPrice": 6, "status": 1}


def _decode_nonce(raw) -> int:
    import rlp  # eth-account dependency

    raw = bytes(raw)
    assert raw[0] == 2  # EIP-1559
    fields = rlp.decode(raw[1:])
    return int.from_bytes(fields[1], "big")


class FakeW3:
    def __init__(self, eth: FakeEth) -> None:
        self.eth = eth


def _relayer(*, pipeline=True, eth: FakeEth | None = None) -> Relayer:
    cfg = RelayerConfig(
        rpc_url="http://127.0.0.1:1",
        chain_id=CHAIN_ID,
        payment_channel=PAYMENT_CHANNEL,
        gateway_private_key=GATEWAY.key.hex(),
        device_address=DEVICE.address,
        merchant_address=MERCHANT.address,
        submit=True,
        pipeline=pipeline,
    )
    r = Relayer(config=cfg)
    r.w3 = FakeW3(eth or FakeEth())
    return r


def _voucher(nonce: int = 1) -> Voucher:
    return Voucher(
        channel=CHANNEL,
        nonce=nonce,
        cumulativeAmount=1_000 * nonce,
        serviceRef=keccak(text=f"s{nonce}"),
        deadline=int(time.time()) + 300,
    )


def _sig(account, v: Voucher) -> HexBytes:
    msg = encode_typed_data(
        full_message=v.to_typed_data(chain_id=CHAIN_ID, verifying_contract=PAYMENT_CHANNEL)
    )
    return HexBytes(Account.sign_message(msg, account.key).signature)


def _relay(r: Relayer, n: int = 1):
    v = _voucher(n)
    return r.relay(v, _sig(DEVICE, v), _sig(MERCHANT, v))


# --- config: off by default --------------------------------------------------


def test_pipeline_is_off_by_default():
    assert RelayerConfig().pipeline is False
    assert RelayerConfig.from_env({}).pipeline is False
    assert RelayerConfig.from_env({"GATEWAY_PIPELINE": "1"}).pipeline is True


def test_default_path_returns_a_plain_relay_result():
    eth = FakeEth()
    r = _relayer(pipeline=False, eth=eth)
    res = _relay(r)
    assert type(res) is RelayResult
    assert r._nonces is None  # the nonce manager is never created
    assert eth.count_calls == ["latest"]  # the old fresh-per-voucher read


# --- nonce manager -----------------------------------------------------------


def test_allocations_are_unique_across_threads():
    m = NonceManager(lambda: 10)
    got: list[int] = []
    lock = threading.Lock()

    def worker():
        for _ in range(50):
            n, _ = m.allocate()
            with lock:
                got.append(n)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == list(range(10, 410))


def test_failure_resyncs_fills_the_gap_and_skips_held_nonces():
    pending = {"n": 10}
    m = NonceManager(lambda: pending["n"])
    a, _ = m.allocate()  # 10 - will fail
    b, _ = m.allocate()  # 11 - in flight
    c, _ = m.allocate()  # 12 - in flight
    m.fail(a, R.FAIL_SEND)  # node's pending nonce stops at the gap: 10
    assert m.resyncs == 1 and m.epoch == 1
    assert m.allocate()[0] == 10  # fills the gap
    assert m.allocate()[0] == 13  # skips 11 and 12, still held
    assert m.release(b) == 10 and m.release(c) == 10  # both sat behind the gap
    assert m.release(a) is None
    ev = m.snapshot()["events"][0]
    assert ev["nonce"] == 10 and ev["inFlightBehind"] == [11, 12]


def test_occupied_nonce_failure_resyncs_without_marking_a_gap():
    pending = {"n": 10}
    m = NonceManager(lambda: pending["n"])
    a, _ = m.allocate()
    b, _ = m.allocate()
    pending["n"] = 12  # someone else used 10 and 11 is ours in flight
    m.fail(a, R.FAIL_NONCE_TOO_LOW)
    assert m.release(b) is None
    assert m.allocate()[0] == 12


# --- error classification -----------------------------------------------------


@pytest.mark.parametrize(
    "msg,status",
    [
        ("nonce too low: next nonce 7, tx nonce 5", R.FAIL_NONCE_TOO_LOW),
        ("replacement transaction underpriced", R.FAIL_REPLACEMENT_UNDERPRICED),
        ("already known", R.FAIL_ALREADY_KNOWN),
        ("nonce too high", R.FAIL_NONCE_TOO_HIGH),
        ("insufficient funds for gas * price + value", R.FAIL_INSUFFICIENT_FUNDS),
        ("max fee per gas less than block base fee", R.FAIL_UNDERPRICED),
        ("HTTPConnectionPool: Read timed out.", R.FAIL_SEND_TIMEOUT),
        ("something else entirely", R.FAIL_SEND),
    ],
)
def test_send_errors_map_to_distinct_statuses(msg, status):
    assert classify_send_error(RpcError(msg)) == status


# --- relayer, pipelined --------------------------------------------------------


def test_pipelined_relay_allocates_locally_and_records_fields():
    eth = FakeEth(pending=5)
    r = _relayer(eth=eth)
    res1, res2 = _relay(r, 1), _relay(r, 2)
    assert isinstance(res1, PipelinedResult)
    assert (res1.txNonce, res2.txNonce) == (5, 6)
    assert eth.sent == [5, 6]
    assert eth.count_calls == ["pending"]  # read once, then local
    assert eth.estimate_nonces == [None, None]  # no nonce is held while estimating
    assert res1.failure is None and res1.submitted and res1.t4 is not None
    assert res1.inflight == 1 and res1.nonceResyncs == 0 and res1.gapBehind is None


def test_concurrent_relays_overlap_and_never_share_a_nonce():
    eth = FakeEth(pending=0)
    eth.send_delay = 0.2
    r = _relayer(eth=eth)
    out = []
    threads = [threading.Thread(target=lambda i=i: out.append(_relay(r, i + 1))) for i in range(4)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert time.monotonic() - t0 < 0.6  # 4 x 0.2 s would be 0.8 s if serialised
    assert sorted(x.txNonce for x in out) == [0, 1, 2, 3]


def test_broadcast_failure_is_a_status_not_a_retry():
    eth = FakeEth(pending=5)
    eth.fail_send[5] = RpcError("nonce too low")
    r = _relayer(eth=eth)
    res = _relay(r)
    assert res.failure == R.FAIL_NONCE_TOO_LOW
    assert res.submitted is False and res.txHash is None
    assert res.nonceResyncs == 1
    assert eth.sent == []  # not retried


def test_not_mined_is_recorded_and_resyncs():
    eth = FakeEth(pending=5)
    eth.no_receipt.add(5)
    r = _relayer(eth=eth)
    res = _relay(r)
    assert res.failure == R.FAIL_NOT_MINED
    assert res.submitted is True and res.t4 is None
    assert res.nonceResyncs == 1


# --- API ---------------------------------------------------------------------


def _payload(v: Voucher) -> dict:
    return {
        "voucher": {
            "channel": v.channel,
            "nonce": v.nonce,
            "cumulativeAmount": str(v.cumulativeAmount),
            "serviceRef": "0x" + v.serviceRef.hex(),
            "deadline": v.deadline,
        },
        "sigDevice": _sig(DEVICE, v).to_0x_hex(),
        "sigMerchant": _sig(MERCHANT, v).to_0x_hex(),
        "t0": 1.0,
        "t1": 2.0,
    }


def test_api_pipelined_route_returns_the_extra_fields():
    client = TestClient(create_app(_relayer()))
    body = client.post("/voucher", json=_payload(_voucher())).json()
    assert body["txNonce"] == 5 and body["failure"] is None and body["t6"] is not None


def test_api_pipelined_broadcast_failure_is_502_with_status():
    eth = FakeEth(pending=5)
    eth.fail_send[5] = RpcError("replacement transaction underpriced")
    client = TestClient(create_app(_relayer(eth=eth)))
    resp = client.post("/voucher", json=_payload(_voucher()))
    assert resp.status_code == 502
    assert resp.json()["failure"] == R.FAIL_REPLACEMENT_UNDERPRICED


def test_api_default_route_shape_is_unchanged():
    client = TestClient(create_app(_relayer(pipeline=False)))
    body = client.post("/voucher", json=_payload(_voucher())).json()
    assert "failure" not in body and "txNonce" not in body
    assert set(body) == set(RelayResult.model_fields)


def test_pipelined_failure_detail_never_carries_the_rpc_key():
    """A keyed RPC URL (exp/rpc) quoted in an exception must not reach the row."""
    secret_url = "https://base-sepolia.example.com/v2/SECRETKEY123"
    cfg = RelayerConfig(rpc_url=secret_url, chain_id=CHAIN_ID, payment_channel=PAYMENT_CHANNEL,
                        gateway_private_key=GATEWAY.key.hex(), submit=True, pipeline=True)
    r = Relayer(config=cfg)
    res = r._fail(PipelinedResult.model_construct(channel=CHANNEL, nonce=1),
                  R.FAIL_RPC, f"estimate_gas: HTTPError for url: {secret_url}")
    assert "SECRETKEY123" not in res.reason
    assert "base-sepolia.example.com" in res.reason


def test_receipt_lookup_error_keeps_the_nonce_consumed():
    """A broadcast tx whose receipt poll fails (e.g. RPC 429) has used its nonce.

    Regression for v2-3: the gateway resynced after a receipt-lookup error, the
    resync's own RPC call failed too, the counter fell back to the broadcast
    nonce, and the next voucher reused it (nonce_too_low) although the first
    tx had settled.
    """
    eth = FakeEth(pending=5)

    def flaky_receipt(tx_hash, timeout):
        raise RuntimeError("429 Client Error: Too Many Requests")

    eth.wait_for_transaction_receipt = flaky_receipt
    r = _relayer(eth=eth)
    first = _relay(r, 1)
    assert first.failure == R.FAIL_RECEIPT
    assert first.txNonce == 5
    assert first.nonceResyncs == 0

    eth.wait_for_transaction_receipt = FakeEth.wait_for_transaction_receipt.__get__(eth)
    eth.pending = 5  # a lagging node would still report 5
    second = _relay(r, 2)
    assert second.txNonce == 6, "nonce 5 was handed out twice"
