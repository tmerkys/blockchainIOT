"""Canonical EIP-712 Voucher struct, shared by device, gateway, and merchant.

Mirrors `contracts/src/VoucherLib.sol` field-for-field, including field order —
the order matters because it feeds the EIP-712 typed-data `types["Voucher"]`
list, which must match the Solidity typehash string exactly:

    Voucher(address channel,uint256 nonce,uint256 cumulativeAmount,bytes32 serviceRef,uint64 deadline)

The domain name/version below must match the `EIP712(name, version)` args
passed into VoucherLib's constructor by the concrete settlement contract
(currently "MagisVoucher" / "1", per contracts/test/VoucherLib.t.sol).
"""

from __future__ import annotations

from typing import Any

from eth_utils import is_address, to_checksum_address
from hexbytes import HexBytes
from pydantic import BaseModel, ConfigDict, field_validator

DOMAIN_NAME = "MagisVoucher"
DOMAIN_VERSION = "1"

UINT64_MAX = 2**64 - 1
UINT256_MAX = 2**256 - 1

VOUCHER_TYPES: dict[str, list[dict[str, str]]] = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Voucher": [
        {"name": "channel", "type": "address"},
        {"name": "nonce", "type": "uint256"},
        {"name": "cumulativeAmount", "type": "uint256"},
        {"name": "serviceRef", "type": "bytes32"},
        {"name": "deadline", "type": "uint64"},
    ],
}


class Voucher(BaseModel):
    """Mirrors the Solidity `Voucher` struct in VoucherLib.sol."""

    model_config = ConfigDict(frozen=True)

    channel: str
    nonce: int
    cumulativeAmount: int
    serviceRef: bytes
    deadline: int

    @field_validator("channel", mode="before")
    @classmethod
    def _checksum_channel(cls, v: Any) -> str:
        if not isinstance(v, str) or not is_address(v):
            raise ValueError(f"channel is not a valid address: {v!r}")
        return to_checksum_address(v)

    @field_validator("nonce", "cumulativeAmount")
    @classmethod
    def _validate_uint256(cls, v: int) -> int:
        if not (0 <= v <= UINT256_MAX):
            raise ValueError(f"value {v} out of uint256 range")
        return v

    @field_validator("deadline")
    @classmethod
    def _validate_uint64(cls, v: int) -> int:
        if not (0 <= v <= UINT64_MAX):
            raise ValueError(f"deadline {v} out of uint64 range")
        return v

    @field_validator("serviceRef", mode="before")
    @classmethod
    def _coerce_service_ref(cls, v: Any) -> bytes:
        if isinstance(v, str):
            v = HexBytes(v)
        if not isinstance(v, (bytes, bytearray)):
            raise TypeError(f"serviceRef must be bytes or a hex string, got {type(v)!r}")
        v = bytes(v)
        if len(v) != 32:
            raise ValueError(f"serviceRef must be exactly 32 bytes, got {len(v)}")
        return v

    def to_typed_data(self, *, chain_id: int, verifying_contract: str) -> dict[str, Any]:
        """Build the EIP-712 typed-data dict (domain + types + message).

        Passable directly as `full_message` to `Account.sign_typed_data` /
        `eth_account.messages.encode_typed_data`. `verifying_contract` is the
        settlement contract address (the one that calls `_hashTypedDataV4`),
        which is not necessarily the same address as `channel`.
        """
        if not is_address(verifying_contract):
            raise ValueError(f"verifying_contract is not a valid address: {verifying_contract!r}")

        return {
            "types": VOUCHER_TYPES,
            "primaryType": "Voucher",
            "domain": {
                "name": DOMAIN_NAME,
                "version": DOMAIN_VERSION,
                "chainId": chain_id,
                "verifyingContract": to_checksum_address(verifying_contract),
            },
            "message": {
                "channel": self.channel,
                "nonce": self.nonce,
                "cumulativeAmount": self.cumulativeAmount,
                "serviceRef": self.serviceRef,
                "deadline": self.deadline,
            },
        }
