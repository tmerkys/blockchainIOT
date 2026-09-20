// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ChannelTestBase} from "./ChannelTestBase.sol";
import {Voucher} from "../src/VoucherLib.sol";

/// @notice M6 / H2: the gateway cannot move funds on its own.
///
///         This is the suite the H2 claim rests on. The threat model is a gateway
///         that is fully malicious but otherwise normal: it holds its own key, it
///         pays the gas, it chooses what to submit and when, and it sees every
///         voucher that passes through it. What it does not hold is the device key
///         or the merchant key, and every test here asserts that this is the only
///         thing standing between it and the escrow — and that it is enough.
///
///         The gateway's own off-chain checks are deliberately given no weight: an
///         adversary simply removes them. Every assertion below is about what the
///         *contract* does when a malformed settlement actually reaches the chain.
contract GatewayAbuseTest is ChannelTestBase {
    // ------------------------------------------------- the central H2 assertion

    /// @dev CLAUDE.md M6, case 1: "gateway-only signature".
    ///      The gateway signs all three slots with the only key it has.
    function test_gatewayOnlySignatureReverts() public {
        Voucher memory v = _voucher(1, 1 ether);
        bytes memory own = _sign(gatewayPk, v);

        vm.prank(gateway);
        vm.expectRevert("bad device sig");
        pc.settle(v, own, own, own);

        assertEq(_paid(), 0, "nothing moved");
        assertEq(merchant.balance, 0);
        assertEq(address(pc).balance, DEPOSIT, "escrow intact");
    }

    /// @dev The same attack aimed at the exit instead of the spend path. A close()
    ///      that needed fewer signatures than settle() would be a way out that
    ///      auditing settle() alone would never reveal.
    function test_gatewayOnlySignatureCannotCloseEither() public {
        Voucher memory v = _voucher(1, 1 ether);
        bytes memory own = _sign(gatewayPk, v);

        vm.prank(gateway);
        vm.expectRevert("bad device sig");
        pc.close(v, own, own, own);

        assertEq(address(pc).balance, DEPOSIT, "escrow intact");
    }

    /// @dev CLAUDE.md M6, case 2: "forged device signature".
    ///      Genuine gateway and merchant signatures, device slot forged.
    function test_forgedDeviceSignatureReverts() public {
        Voucher memory v = _voucher(1, 1 ether);
        bytes memory forged = _sign(attackerPk, v);
        bytes memory g = _sign(gatewayPk, v);
        bytes memory m = _sign(merchantPk, v);

        vm.prank(gateway);
        vm.expectRevert("bad device sig");
        pc.settle(v, forged, g, m);

        assertEq(_paid(), 0, "nothing moved");
    }

    /// @dev Collusion case: gateway and merchant together, without the device.
    ///      The device is the party attesting that the service happened, so this
    ///      is the pair that could otherwise invent service and bill for it.
    function test_gatewayAndMerchantTogetherCannotSpend() public {
        Voucher memory v = _voucher(1, 1 ether);
        bytes memory fakeDevice = _sign(gatewayPk, v);
        bytes memory g = _sign(gatewayPk, v);
        bytes memory m = _sign(merchantPk, v);

        vm.prank(gateway);
        vm.expectRevert("bad device sig");
        pc.settle(v, fakeDevice, g, m);
    }

    /// @dev The mirror: a genuine device voucher the merchant never countersigned.
    function test_missingMerchantSignatureReverts() public {
        Voucher memory v = _voucher(1, 1 ether);
        bytes memory d = _sign(devicePk, v);
        bytes memory g = _sign(gatewayPk, v);
        bytes memory forgedMerchant = _sign(gatewayPk, v);

        vm.prank(gateway);
        vm.expectRevert("bad merchant sig");
        pc.settle(v, d, g, forgedMerchant);
    }

    /// @dev A gateway that is not this channel's gateway cannot substitute itself,
    ///      even holding otherwise valid device and merchant signatures.
    function test_strangerCannotSubstituteForTheGateway() public {
        Voucher memory v = _voucher(1, 1 ether);
        bytes memory d = _sign(devicePk, v);
        bytes memory wrongGateway = _sign(attackerPk, v);
        bytes memory m = _sign(merchantPk, v);

        vm.prank(attacker);
        vm.expectRevert("bad gateway sig");
        pc.settle(v, d, wrongGateway, m);
    }

    // ------------------------------------------------------ amount manipulation

    /// @dev The gateway holds a valid low-value voucher and wants a high-value
    ///      settlement. Editing the amount invalidates all three signatures,
    ///      because the amount is inside the signed struct hash.
    function test_gatewayCannotInflateTheAmount() public {
        Voucher memory honest = _voucher(1, 1 ether);
        (bytes memory d, bytes memory g, bytes memory m) = _sigs(honest);

        Voucher memory inflated = honest;
        inflated.cumulativeAmount = 9 ether;

        vm.prank(gateway);
        vm.expectRevert("bad device sig");
        pc.settle(inflated, d, g, m);
    }

    /// @dev Nor can it redirect payment by editing the service reference, which is
    ///      the field binding a voucher to the metered fact it was issued for.
    function test_gatewayCannotAlterTheServiceRef() public {
        Voucher memory honest = _voucher(1, 1 ether);
        (bytes memory d, bytes memory g, bytes memory m) = _sigs(honest);

        Voucher memory tampered = honest;
        tampered.serviceRef = keccak256("a different service fact");

        vm.prank(gateway);
        vm.expectRevert("bad device sig");
        pc.settle(tampered, d, g, m);
    }

    /// @dev CLAUDE.md M6, case 4: "non-monotonic amount".
    ///      A fully and honestly signed voucher worth less than what is already
    ///      paid. Every signature is real; the contract rejects it on accounting.
    function test_nonMonotonicAmountReverts() public {
        _settle(_voucher(1, 5 ether));
        assertEq(_paid(), 5 ether);

        Voucher memory lower = _voucher(2, 3 ether);
        (bytes memory d, bytes memory g, bytes memory m) = _sigs(lower);

        vm.prank(gateway);
        vm.expectRevert("not monotonic");
        pc.settle(lower, d, g, m);

        assertEq(_paid(), 5 ether, "paid total did not go backwards");
    }

    /// @dev Equal is not greater: re-settling the same amount under a fresh nonce
    ///      would pay a zero delta and burn a nonce for nothing.
    function test_equalAmountIsNotMonotonic() public {
        _settle(_voucher(1, 5 ether));

        Voucher memory same = _voucher(2, 5 ether);
        (bytes memory d, bytes memory g, bytes memory m) = _sigs(same);

        vm.prank(gateway);
        vm.expectRevert("not monotonic");
        pc.settle(same, d, g, m);
    }

    /// @dev The escrow is a hard ceiling: the channel cannot pay out more than was
    ///      deposited, even with three genuine signatures over a larger amount.
    function test_cannotSpendBeyondTheDeposit() public {
        Voucher memory tooBig = _voucher(1, DEPOSIT + 1 wei);
        (bytes memory d, bytes memory g, bytes memory m) = _sigs(tooBig);

        vm.prank(gateway);
        vm.expectRevert("exceeds deposit");
        pc.settle(tooBig, d, g, m);
    }

    // --------------------------------------------------------------- deadlines

    /// @dev CLAUDE.md M6, case 5: "expired deadline".
    ///      Withholding is the gateway's cheapest attack — it controls submission
    ///      timing entirely — and the deadline is what bounds it.
    function test_expiredDeadlineReverts() public {
        Voucher memory v = _voucher(1, 1 ether);
        (bytes memory d, bytes memory g, bytes memory m) = _sigs(v);

        vm.warp(v.deadline + 1);

        vm.prank(gateway);
        vm.expectRevert("expired");
        pc.settle(v, d, g, m);
    }

    /// @dev A stale voucher stays dead however long the gateway sits on it.
    function test_expiredVoucherStaysDeadMuchLater() public {
        Voucher memory v = _voucher(1, 1 ether);
        (bytes memory d, bytes memory g, bytes memory m) = _sigs(v);

        vm.warp(v.deadline + 365 days);

        vm.prank(gateway);
        vm.expectRevert("expired");
        pc.settle(v, d, g, m);
    }

    // ------------------------------------------------------------ malformed input

    /// @dev Garbage in the signature slots must revert, not be treated as absent.
    function test_emptySignaturesRevert() public {
        Voucher memory v = _voucher(1, 1 ether);

        vm.prank(gateway);
        vm.expectRevert();
        pc.settle(v, "", "", "");
    }

    /// @dev A channel id nobody opened has no signers, so nothing can authorise it.
    function test_unknownChannelReverts() public {
        Voucher memory v = _voucherFor(address(0xDEAD), 1, 1 ether);
        bytes memory d = _sign(devicePk, v);
        bytes memory g = _sign(gatewayPk, v);
        bytes memory m = _sign(merchantPk, v);

        vm.prank(gateway);
        vm.expectRevert("unknown channel");
        pc.settle(v, d, g, m);
    }
}
