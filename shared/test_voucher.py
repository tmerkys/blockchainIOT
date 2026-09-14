import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from voucher import Voucher

CHAIN_ID = 84532  # Base Sepolia
VERIFYING_CONTRACT = "0x00000000000000000000000000000000000000cD"


def _sample_voucher() -> Voucher:
    return Voucher(
        channel="0x00000000000000000000000000000000000000AB",
        nonce=1,
        cumulativeAmount=1_000,
        serviceRef=keccak(text="session-1"),
        deadline=1_893_456_000,
    )


def test_recovered_signer_matches_signer():
    account = Account.create()
    voucher = _sample_voucher()

    typed_data = voucher.to_typed_data(chain_id=CHAIN_ID, verifying_contract=VERIFYING_CONTRACT)
    signable = encode_typed_data(full_message=typed_data)

    signed = Account.sign_message(signable, private_key=account.key)
    recovered = Account.recover_message(signable, signature=signed.signature)

    assert recovered == account.address


def test_different_nonce_changes_digest_and_signature():
    account = Account.create()
    voucher = _sample_voucher()
    other = voucher.model_copy(update={"nonce": voucher.nonce + 1})

    typed_data_a = voucher.to_typed_data(chain_id=CHAIN_ID, verifying_contract=VERIFYING_CONTRACT)
    typed_data_b = other.to_typed_data(chain_id=CHAIN_ID, verifying_contract=VERIFYING_CONTRACT)

    signable_a = encode_typed_data(full_message=typed_data_a)
    signable_b = encode_typed_data(full_message=typed_data_b)

    assert signable_a.body != signable_b.body

    signed_a = Account.sign_message(signable_a, private_key=account.key)
    signed_b = Account.sign_message(signable_b, private_key=account.key)

    assert signed_a.signature != signed_b.signature
    # A signature for voucher A must not verify against voucher B's digest.
    assert Account.recover_message(signable_b, signature=signed_a.signature) != account.address


def test_service_ref_rejects_wrong_length():
    with pytest.raises(ValueError):
        Voucher(
            channel="0x0000000000000000000000000000000000beef",
            nonce=1,
            cumulativeAmount=1,
            serviceRef=b"\x01" * 31,
            deadline=1_893_456_000,
        )


if __name__ == "__main__":
    test_recovered_signer_matches_signer()
    test_different_nonce_changes_digest_and_signature()
    test_service_ref_rejects_wrong_length()
    print("all tests passed")
