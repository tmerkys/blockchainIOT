// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {VoucherLib, Voucher} from "../src/VoucherLib.sol";

/// @notice Minimal concrete contract to exercise the internal VoucherLib hashing.
contract VoucherLibHarness is VoucherLib {
    constructor() VoucherLib("MagisVoucher", "1") {}

    function hashVoucher(Voucher calldata v) external pure returns (bytes32) {
        return _hashVoucher(v);
    }

    function domainSeparator() external view returns (bytes32) {
        return _domainSeparatorV4();
    }

    function digest(Voucher calldata v) external view returns (bytes32) {
        return _hashTypedDataV4(_hashVoucher(v));
    }
}

contract VoucherLibTest is Test {
    VoucherLibHarness harness;

    function setUp() public {
        harness = new VoucherLibHarness();
    }

    function _sampleVoucher() internal pure returns (Voucher memory) {
        return Voucher({
            channel: address(0xBEEF),
            nonce: 1,
            cumulativeAmount: 1_000,
            serviceRef: keccak256("session-1"),
            deadline: 1_893_456_000
        });
    }

    function test_domainSeparatorIsNonZero() public view {
        assertTrue(harness.domainSeparator() != bytes32(0));
    }

    function test_domainSeparatorIsDeterministic() public view {
        assertEq(harness.domainSeparator(), harness.domainSeparator());
    }

    function test_structHashIsNonZero() public view {
        assertTrue(harness.hashVoucher(_sampleVoucher()) != bytes32(0));
    }

    function test_structHashIsDeterministic() public view {
        Voucher memory v = _sampleVoucher();
        assertEq(harness.hashVoucher(v), harness.hashVoucher(v));
    }

    function test_structHashDiffersOnFieldChange() public view {
        Voucher memory v = _sampleVoucher();
        bytes32 base = harness.hashVoucher(v);

        v.nonce += 1;
        assertTrue(harness.hashVoucher(v) != base);
    }

    function test_digestIsNonZeroAndDeterministic() public view {
        Voucher memory v = _sampleVoucher();
        bytes32 d1 = harness.digest(v);
        bytes32 d2 = harness.digest(v);

        assertTrue(d1 != bytes32(0));
        assertEq(d1, d2);
        assertTrue(d1 != harness.hashVoucher(v));
    }
}
