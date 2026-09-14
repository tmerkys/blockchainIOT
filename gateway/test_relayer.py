import sys
import time
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak
from hexbytes import HexBytes

# Same bootstrap as gateway/api.py: pytest puts gateway/ on sys.path, not the
# repo root, and relayer.py imports shared.voucher. Importing under the
# `gateway.` package also keeps one module identity shared with test_api.py,
# so `VoucherRejected` is the same class in both files.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gateway.relayer import (  # noqa: E402
    SETTLE_ABI,
    Relayer,
    RelayerConfig,
    RelayerNotReady,
    VoucherRejected,
)
from shared.voucher import Voucher  # noqa: E402

CHAIN_ID = 84532  # Base Sepolia
PAYMENT_CHANNEL = "0x00000000000000000000000000000000000000cD"
CHANNEL = "0x00000000000000000000000000000000000000AB"

# keccak("settle(...)")[:4], spelled out from the ABI in CLAUDE.md rather than
# read back off the contract object - otherwise the test only proves that
# encode_abi agrees with itself.
SETTLE_SIGNATURE = "settle((address,uint256,uint256,bytes32,uint64),bytes,bytes,bytes)"
SETTLE_SELECTOR = keccak(text=SETTLE_SIGNATURE)[:4]

DEVICE = Account.create()
MERCHANT = Account.create()
GATEWAY = Account.create()
STRANGER = Account.create()


def _config(**overrides) -> RelayerConfig:
    kwargs = {
        "rpc_url": "http://127.0.0.1:8545",
        "chain_id": CHAIN_ID,
        "payment_channel": PAYMENT_CHANNEL,
        "gateway_private_key": GATEWAY.key.hex(),
        "device_address": DEVICE.address,
        "merchant_address": MERCHANT.address,
    }
    kwargs.update(overrides)
    return RelayerConfig(**kwargs)


def _relayer(**overrides) -> Relayer:
    # No RPC call happens at construction, so these tests need no anvil.
    return Relayer(config=_config(**overrides))


def _voucher(**overrides) -> Voucher:
    kwargs = {
        "channel": CHANNEL,
        "nonce": 1,
        "cumulativeAmount": 1_000,
        "serviceRef": keccak(text="session-1"),
        "deadline": 1_893_456_000,  # 2030-01-01
    }
    kwargs.update(overrides)
    return Voucher(**kwargs)


def _sign(account, voucher: Voucher, *, chain_id: int = CHAIN_ID) -> HexBytes:
    """Sign `voucher` the way the device does, independently of Relayer.sign()."""
    signable = encode_typed_data(
        full_message=voucher.to_typed_data(
            chain_id=chain_id, verifying_contract=PAYMENT_CHANNEL
        )
    )
    return HexBytes(Account.sign_message(signable, private_key=account.key).signature)


# --- verify_device --------------------------------------------------------


def test_verify_device_recovers_the_configured_device():
    relayer = _relayer()
    voucher = _voucher()

    assert relayer.verify_device(voucher, _sign(DEVICE, voucher)) == DEVICE.address


def test_verify_device_rejects_a_signature_from_another_key():
    # H2: the gateway must not be able to substitute its own signature for the
    # device's, and neither may any third party that can reach the API.
    relayer = _relayer()
    voucher = _voucher()

    for impostor in (GATEWAY, MERCHANT, STRANGER):
        with pytest.raises(VoucherRejected, match="bad device sig"):
            relayer.verify_device(voucher, _sign(impostor, voucher))


def test_verify_device_rejects_a_signature_over_a_different_voucher():
    relayer = _relayer()
    signed_voucher = _voucher()
    tampered = signed_voucher.model_copy(update={"cumulativeAmount": 999_999})

    with pytest.raises(VoucherRejected, match="bad device sig"):
        relayer.verify_device(tampered, _sign(DEVICE, signed_voucher))


def test_verify_device_rejects_a_malformed_signature():
    # Anything at all can POST here; a short signature is a rejection, not a 500.
    relayer = _relayer()

    with pytest.raises(VoucherRejected, match="65 bytes"):
        relayer.verify_device(_voucher(), b"\x01" * 64)


def test_rejection_does_not_leak_the_recovered_address():
    relayer = _relayer()
    voucher = _voucher()

    with pytest.raises(VoucherRejected) as excinfo:
        relayer.verify_device(voucher, _sign(STRANGER, voucher))

    assert STRANGER.address.lower() not in str(excinfo.value).lower()


# --- check_deadline -------------------------------------------------------


def test_check_deadline_rejects_an_expired_voucher():
    now = time.time()
    expired = _voucher(deadline=int(now) - 30)

    with pytest.raises(VoucherRejected, match="expired"):
        Relayer.check_deadline(expired, now=now)


def test_check_deadline_accepts_a_voucher_that_is_still_live():
    now = time.time()
    Relayer.check_deadline(_voucher(deadline=int(now) + 30), now=now)


# --- build_settle_tx ------------------------------------------------------


def test_build_settle_tx_uses_the_settle_selector():
    relayer = _relayer()
    voucher = _voucher()

    tx = relayer.build_settle_tx(
        voucher,
        _sign(DEVICE, voucher),
        relayer.sign(voucher),
        _sign(MERCHANT, voucher),
    )

    assert HexBytes(tx["data"])[:4] == SETTLE_SELECTOR
    assert tx["to"] == PAYMENT_CHANNEL
    assert tx["chainId"] == CHAIN_ID
    assert tx["value"] == 0
    assert tx["from"] == GATEWAY.address


def test_build_settle_tx_encodes_the_voucher_and_all_three_signatures():
    relayer = _relayer()
    voucher = _voucher()
    sig_device = _sign(DEVICE, voucher)
    sig_gateway = relayer.sign(voucher)
    sig_merchant = _sign(MERCHANT, voucher)

    tx = relayer.build_settle_tx(voucher, sig_device, sig_gateway, sig_merchant)

    contract = Relayer(config=_config()).w3.eth.contract(
        address=PAYMENT_CHANNEL, abi=SETTLE_ABI
    )
    func, args = contract.decode_function_input(tx["data"])

    assert func.fn_name == "settle"
    # web3 decodes the struct back into a dict keyed by the ABI component names.
    assert args["v"] == {
        "channel": voucher.channel,
        "nonce": voucher.nonce,
        "cumulativeAmount": voucher.cumulativeAmount,
        "serviceRef": voucher.serviceRef,
        "deadline": voucher.deadline,
    }
    assert args["sigDevice"] == bytes(sig_device)
    assert args["sigGateway"] == bytes(sig_gateway)
    assert args["sigMerchant"] == bytes(sig_merchant)


# --- sign -----------------------------------------------------------------


def test_sign_produces_a_recoverable_gateway_co_signature():
    relayer = _relayer()
    voucher = _voucher()

    signature = relayer.sign(voucher)

    assert len(signature) == 65
    assert relayer.recover(voucher, signature) == GATEWAY.address
    assert relayer.gateway_address == GATEWAY.address


def test_gateway_signature_is_bound_to_the_domain():
    # The gateway co-signature carries the same replay protection as the
    # device's: it does not verify against another chain (L2 voucher replayed
    # onto L1) or another verifying contract.
    relayer = _relayer()
    voucher = _voucher()
    signature = relayer.sign(voucher)

    elsewhere = _relayer(chain_id=11155111)  # Ethereum Sepolia
    assert elsewhere.recover(voucher, signature) != GATEWAY.address

    other_contract = _relayer(payment_channel="0x000000000000000000000000000000000000dEaD")
    assert other_contract.recover(voucher, signature) != GATEWAY.address


def test_sign_without_a_key_is_a_gateway_problem_not_a_crash():
    relayer = _relayer(gateway_private_key=None)

    assert relayer.gateway_address is None
    with pytest.raises(RelayerNotReady, match="GATEWAY_PRIVATE_KEY"):
        relayer.sign(_voucher())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
