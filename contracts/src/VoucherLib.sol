// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";

struct Voucher {
    address channel; // which channel this spends from
    uint256 nonce; // strictly increasing, replay protection
    uint256 cumulativeAmount; // monotonic total, channel-style
    bytes32 serviceRef; // keccak256(sessionId ‖ kWh ‖ timestamp) — the service fact
    uint64 deadline; // voucher expiry
}

/// @notice EIP-712 domain separator + struct hashing for the Voucher type.
/// PaymentChannel inherits this and combines `_hashVoucher` with the
/// EIP712-provided `_hashTypedDataV4` to recover signer addresses.
abstract contract VoucherLib is EIP712 {
    bytes32 private constant VOUCHER_TYPEHASH = keccak256(
        "Voucher(address channel,uint256 nonce,uint256 cumulativeAmount,bytes32 serviceRef,uint64 deadline)"
    );

    constructor(string memory name, string memory version) EIP712(name, version) {}

    function _hashVoucher(Voucher calldata v) internal pure returns (bytes32) {
        return keccak256(
            abi.encode(VOUCHER_TYPEHASH, v.channel, v.nonce, v.cumulativeAmount, v.serviceRef, v.deadline)
        );
    }
}
