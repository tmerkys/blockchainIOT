import sys
import time
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak
from fastapi.testclient import TestClient
from hexbytes import HexBytes

# See the note in test_relayer.py: pytest puts gateway/ on sys.path, not the
# repo root, and gateway.api imports shared.voucher.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gateway.api import create_app  # noqa: E402
from gateway.relayer import Relayer, RelayerConfig  # noqa: E402
from shared.voucher import Voucher  # noqa: E402

CHAIN_ID = 84532  # Base Sepolia
PAYMENT_CHANNEL = "0x00000000000000000000000000000000000000cD"
CHANNEL = "0x00000000000000000000000000000000000000AB"
SERVICE_REF = "0x" + keccak(text="session-1").hex()

SETTLE_SELECTOR = keccak(
    text="settle((address,uint256,uint256,bytes32,uint64),bytes,bytes,bytes)"
)[:4]

DEVICE = Account.create()
MERCHANT = Account.create()
GATEWAY = Account.create()
STRANGER = Account.create()


@pytest.fixture
def client() -> TestClient:
    """App wired to an explicit Relayer, so the test never reads the environment."""
    relayer = Relayer(
        config=RelayerConfig(
            chain_id=CHAIN_ID,
            payment_channel=PAYMENT_CHANNEL,
            gateway_private_key=GATEWAY.key.hex(),
            device_address=DEVICE.address,
            merchant_address=MERCHANT.address,
        )
    )
    with TestClient(create_app(relayer)) as test_client:
        yield test_client


def _sign(account, voucher: dict) -> str:
    signable = encode_typed_data(
        full_message=Voucher(**voucher).to_typed_data(
            chain_id=CHAIN_ID, verifying_contract=PAYMENT_CHANNEL
        )
    )
    return HexBytes(Account.sign_message(signable, private_key=account.key).signature).to_0x_hex()


def _payload(**overrides) -> dict:
    """A voucher the device would POST: valid, signed, not yet expired."""
    now = time.time()
    voucher = {
        "channel": CHANNEL,
        "nonce": 1,
        "cumulativeAmount": 1_000,
        "serviceRef": SERVICE_REF,
        "deadline": int(now) + 300,
    }
    voucher.update(overrides.pop("voucher", {}))

    payload = {
        "voucher": voucher,
        "sigDevice": _sign(DEVICE, voucher),
        "sigMerchant": _sign(MERCHANT, voucher),
        "t0": now - 0.05,
        "t1": now - 0.04,
    }
    payload.update(overrides)
    return payload


# --- POST /voucher, happy path --------------------------------------------


def test_valid_voucher_returns_a_relay_result(client):
    payload = _payload()

    response = client.post("/voucher", json=payload)

    assert response.status_code == 200
    body = response.json()

    assert body["accepted"] is True
    # Accepted but unsent: the calldata is built and PaymentChannel is not
    # deployed yet (M1), which is not the device's fault and so not a 4xx.
    assert body["submitted"] is False
    assert body["txHash"] is None
    assert "not submitted" in body["reason"]

    assert body["channel"] == payload["voucher"]["channel"]
    assert body["nonce"] == payload["voucher"]["nonce"]
    assert body["cumulativeAmount"] == payload["voucher"]["cumulativeAmount"]
    assert body["serviceRef"] == SERVICE_REF

    assert body["device"] == DEVICE.address
    assert body["gateway"] == GATEWAY.address
    assert body["merchant"] == MERCHANT.address

    assert HexBytes(body["calldata"])[:4] == SETTLE_SELECTOR


def test_response_carries_the_marks_one_csv_row_needs(client):
    # One POST has to yield one complete row for experiments/runner.py: the
    # device's t0/t1 echoed untouched, plus the gateway's own t2.
    payload = _payload()

    body = client.post("/voucher", json=payload).json()

    assert body["t0"] == payload["t0"]
    assert body["t1"] == payload["t1"]
    assert body["t2"] is not None and body["t2"] >= payload["t1"]
    # Not submitted yet, so the chain-side marks are still empty.
    assert body["t3"] is body["t4"] is body["t5"] is None


def test_a_voucher_without_a_merchant_signature_still_relays(client):
    body = client.post("/voucher", json=_payload(sigMerchant=None)).json()

    assert body["accepted"] is True
    assert body["merchant"] is None


# --- POST /voucher, rejections --------------------------------------------


# Applied after signing, because these payloads are ones no honest device
# could produce - several of them would not survive Voucher() locally either.
MALFORMED = {
    "serviceRef too short": {"voucher": {"serviceRef": "0x" + "01" * 31}},
    "serviceRef not hex": {"voucher": {"serviceRef": "session-1"}},
    "channel not an address": {"voucher": {"channel": "not-an-address"}},
    "negative deadline": {"voucher": {"deadline": -1}},
    "deadline over uint64": {"voucher": {"deadline": 2**64}},
    "nonce not an integer": {"voucher": {"nonce": "one"}},
    "signature not hex": {"sigDevice": "zz"},
    "signature wrong length": {"sigDevice": "0x" + "11" * 64},
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_malformed_voucher_is_rejected_with_422(client, name):
    payload = _payload()
    for field, value in MALFORMED[name].items():
        if field == "voucher":
            payload["voucher"].update(value)
        else:
            payload[field] = value

    response = client.post("/voucher", json=payload)

    assert response.status_code == 422, name
    # FastAPI's own validation error, raised before the relayer sees anything.
    assert "detail" in response.json()


def test_missing_voucher_field_is_rejected_with_422(client):
    payload = _payload()
    del payload["voucher"]["nonce"]

    assert client.post("/voucher", json=payload).status_code == 422


def test_voucher_signed_by_the_wrong_key_is_rejected_with_422(client):
    # Well-formed, so it reaches the relayer - and comes back through the
    # VoucherRejected handler, whose body shape differs from a 422 above.
    payload = _payload()
    payload["sigDevice"] = _sign(STRANGER, payload["voucher"])

    response = client.post("/voucher", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body["accepted"] is False
    assert body["submitted"] is False
    assert "bad device sig" in body["reason"]


def test_expired_voucher_is_rejected_with_422(client):
    payload = _payload(voucher={"deadline": int(time.time()) - 60})

    response = client.post("/voucher", json=payload)

    assert response.status_code == 422
    assert "expired" in response.json()["reason"]


# --- GET /health ----------------------------------------------------------


def test_health_reports_the_configuration_a_run_depends_on(client, monkeypatch):
    # Stand in for the RPC rather than requiring anvil: the real probe against
    # a dead port spends ~12s in web3's retry backoff, which is not something
    # to pay on every test run.
    relayer = client.app.state.relayer
    monkeypatch.setattr(type(relayer.w3.eth), "chain_id", CHAIN_ID)

    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["chainId"] == CHAIN_ID
    assert body["observedChainId"] == CHAIN_ID
    assert body["chainIdMatches"] is True
    assert body["paymentChannel"] == PAYMENT_CHANNEL
    assert body["gateway"] == GATEWAY.address
    assert body["device"] == DEVICE.address
    assert body["merchant"] == MERCHANT.address
    assert body["canSign"] is True
    # Nothing to submit to yet (M1) - health must not claim otherwise.
    assert body["contractDeployed"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
