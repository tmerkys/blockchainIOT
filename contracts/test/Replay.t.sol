// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ChannelTestBase} from "./ChannelTestBase.sol";
import {PaymentChannel} from "../src/PaymentChannel.sol";
import {Voucher} from "../src/VoucherLib.sol";

/// @notice M6: a voucher is spendable exactly once, in exactly one place.
///
///         Every signature the device produces is public the moment it reaches the
///         gateway, so the interesting question is never "can it be read" but "what
///         can it be re-used for". The answers here are the four axes a signature
///         could be moved along: time (submit it twice), sequence (submit an older
///         one after a newer one), space (aim it at another channel), and
///         deployment (aim it at another chain or another contract).
///
///         The last two are what the EIP-712 domain is for. CLAUDE.md requires the
///         domain separator to include `chainId` and `verifyingContract` precisely
///         so a voucher signed for Base Sepolia cannot be replayed on Sepolia L1
///         or against a different deployment — and an untested requirement is a
///         hope, so both are exercised against a real second deployment below.
contract ReplayTest is ChannelTestBase {
    // -------------------------------------------------------------------- time

    /// @dev CLAUDE.md M6, case 3: "replayed voucher".
    ///      The same fully-signed voucher, submitted twice.
    function test_replayingTheSameVoucherReverts() public {
        Voucher memory v = _voucher(1, 2 ether);
        (bytes memory d, bytes memory g, bytes memory m) = _sigs(v);

        vm.prank(gateway);
        pc.settle(v, d, g, m);
        assertEq(merchant.balance, 2 ether);

        vm.prank(gateway);
        vm.expectRevert("not monotonic");
        pc.settle(v, d, g, m);

        assertEq(merchant.balance, 2 ether, "merchant was not paid twice");
        assertEq(_paid(), 2 ether);
    }

    /// @dev Replay is not made possible by waiting, nor by a different submitter.
    function test_replayByAnotherSubmitterAlsoReverts() public {
        Voucher memory v = _voucher(1, 2 ether);
        (bytes memory d, bytes memory g, bytes memory m) = _sigs(v);

        vm.prank(gateway);
        pc.settle(v, d, g, m);

        vm.warp(block.timestamp + 10 minutes);
        vm.prank(attacker);
        vm.expectRevert("not monotonic");
        pc.settle(v, d, g, m);
    }

    // ---------------------------------------------------------------- sequence

    /// @dev The channel's whole point is that voucher #8 supersedes #7. The
    ///      superseded one must not be re-playable afterwards, or the gateway
    ///      could pick whichever historical voucher suits it.
    function test_supersededVoucherCannotBeSettledAfterwards() public {
        Voucher memory earlier = _voucher(7, 3 ether);
        (bytes memory d7, bytes memory g7, bytes memory m7) = _sigs(earlier);

        _settle(_voucher(8, 5 ether));
        assertEq(_paid(), 5 ether);

        vm.prank(gateway);
        vm.expectRevert("not monotonic");
        pc.settle(earlier, d7, g7, m7);
    }

    /// @dev A higher amount under an already-used nonce is caught by the nonce
    ///      check, not just the amount check — both guards are live.
    function test_reusedNonceWithHigherAmountReverts() public {
        _settle(_voucher(5, 3 ether));

        Voucher memory sameNonce = _voucher(5, 6 ether);
        (bytes memory d, bytes memory g, bytes memory m) = _sigs(sameNonce);

        vm.prank(gateway);
        vm.expectRevert("stale nonce");
        pc.settle(sameNonce, d, g, m);
    }

    // ------------------------------------------------------------------- space

    /// @dev CLAUDE.md M6, case 6: "voucher from another channel".
    ///      Two channels, identical participants, same contract. A voucher signed
    ///      for one names that one in its signed body, so pointing it at the other
    ///      breaks the signatures.
    function test_voucherFromAnotherChannelReverts() public {
        vm.prank(funder);
        address other = pc.open{value: DEPOSIT}(device, gateway, merchant);

        // Honestly signed — for `other`, not for `channelId`.
        Voucher memory forOther = _voucherFor(other, 1, 4 ether);
        bytes memory d = _sign(devicePk, forOther);
        bytes memory g = _sign(gatewayPk, forOther);
        bytes memory m = _sign(merchantPk, forOther);

        // Re-aim it at this channel. `channel` is inside the struct hash, so
        // editing it invalidates every signature over it.
        Voucher memory reaimed = forOther;
        reaimed.channel = channelId;

        vm.prank(gateway);
        vm.expectRevert("bad device sig");
        pc.settle(reaimed, d, g, m);

        assertEq(_paid(), 0, "this channel paid nothing");
    }

    /// @dev Submitting it unmodified drains only the channel it was signed for,
    ///      which is the control showing the signatures themselves were valid.
    function test_theSameVoucherIsValidForItsOwnChannel() public {
        vm.prank(funder);
        address other = pc.open{value: DEPOSIT}(device, gateway, merchant);

        Voucher memory forOther = _voucherFor(other, 1, 4 ether);
        bytes memory d = _sign(devicePk, forOther);
        bytes memory g = _sign(gatewayPk, forOther);
        bytes memory m = _sign(merchantPk, forOther);

        vm.prank(gateway);
        pc.settle(forOther, d, g, m);

        assertEq(merchant.balance, 4 ether);
        assertEq(_paid(), 0, "the other channel is untouched");
    }

    // -------------------------------------------------------------- deployment

    /// @dev CLAUDE.md M6, case 7: "voucher from another chainId".
    ///      The device's signature is harvested from a run on a different chain —
    ///      the exact scenario CLAUDE.md names: a Base Sepolia voucher replayed on
    ///      Sepolia L1. Only the domain's chainId differs; the struct is identical.
    function test_voucherSignedForAnotherChainIdReverts() public {
        Voucher memory v = _voucher(1, 1 ether);

        (, string memory name, string memory version,, address verifyingContract,,) = pc.eip712Domain();
        // Ethereum Sepolia, against this same deployment address.
        bytes32 foreign = _digestIn(v, name, version, 11155111, verifyingContract);

        bytes memory d = _signDigest(devicePk, foreign);
        bytes memory g = _signDigest(gatewayPk, foreign);
        bytes memory m = _signDigest(merchantPk, foreign);

        vm.prank(gateway);
        vm.expectRevert("bad device sig");
        pc.settle(v, d, g, m);

        assertEq(_paid(), 0);
    }

    /// @dev And the control: the identical struct, signed under *this* chainId,
    ///      settles. Without this the test above would also pass if the contract
    ///      rejected everything.
    function test_theSameVoucherUnderThisChainIdSettles() public {
        Voucher memory v = _voucher(1, 1 ether);
        (bytes memory d, bytes memory g, bytes memory m) = _sigs(v);

        vm.prank(gateway);
        pc.settle(v, d, g, m);

        assertEq(merchant.balance, 1 ether);
    }

    /// @dev The other half of the domain: a second deployment on the same chain.
    ///      Same signers, same struct, different `verifyingContract` — so a
    ///      voucher meant for one deployment cannot drain another.
    function test_voucherSignedForAnotherDeploymentReverts() public {
        PaymentChannel second = new PaymentChannel();
        vm.prank(funder);
        address otherChannel = second.open{value: DEPOSIT}(device, gateway, merchant);

        Voucher memory v = _voucherFor(otherChannel, 1, 1 ether);
        (, string memory name, string memory version, uint256 chainId,,,) = pc.eip712Domain();

        // Signed against the *second* deployment, as its own device legitimately would.
        bytes32 forSecond = _digestIn(v, name, version, chainId, address(second));
        bytes memory d = _signDigest(devicePk, forSecond);
        bytes memory g = _signDigest(gatewayPk, forSecond);
        bytes memory m = _signDigest(merchantPk, forSecond);

        // It works there.
        vm.prank(gateway);
        second.settle(v, d, g, m);
        assertEq(merchant.balance, 1 ether);

        // Re-aimed at this deployment, with the channel edited to one it knows,
        // it is worthless — the signature covers the other contract's domain.
        Voucher memory reaimed = _voucher(1, 1 ether);
        vm.prank(gateway);
        vm.expectRevert("bad device sig");
        pc.settle(reaimed, d, g, m);

        assertEq(_paid(), 0, "the first deployment paid nothing");
    }

    /// @dev A voucher from a foreign domain is no more acceptable to close() than
    ///      to settle(): the exit path must bind the domain just as tightly.
    function test_foreignChainIdCannotCloseEither() public {
        Voucher memory v = _voucher(1, 1 ether);
        (, string memory name, string memory version,, address verifyingContract,,) = pc.eip712Domain();
        bytes32 foreign = _digestIn(v, name, version, 11155111, verifyingContract);

        bytes memory d = _signDigest(devicePk, foreign);
        bytes memory g = _signDigest(gatewayPk, foreign);
        bytes memory m = _signDigest(merchantPk, foreign);

        vm.prank(gateway);
        vm.expectRevert("bad device sig");
        pc.close(v, d, g, m);

        assertEq(address(pc).balance, DEPOSIT, "escrow intact");
    }
}
