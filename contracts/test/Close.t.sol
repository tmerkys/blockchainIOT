// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {PaymentChannel} from "../src/PaymentChannel.sol";
import {Voucher} from "../src/VoucherLib.sol";

/// @notice M4: close() settles the final voucher and refunds the unspent deposit.
///         The happy path is what the amortisation claim rests on — 200 ticks, one
///         on-chain transaction — and the abuse cases below are the reason closing
///         does not weaken H2: it carries the same 3-of-3 requirement as settle(),
///         and the refund can only ever reach the funder.
contract CloseTest is Test {
    bytes32 private constant DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 private constant VOUCHER_TYPEHASH = keccak256(
        "Voucher(address channel,uint256 nonce,uint256 cumulativeAmount,bytes32 serviceRef,uint64 deadline)"
    );

    uint256 private constant DEPOSIT = 10 ether;

    PaymentChannel internal pc;
    address internal channelId;

    address internal device;
    uint256 internal devicePk;
    address internal gateway;
    uint256 internal gatewayPk;
    address internal merchant;
    uint256 internal merchantPk;
    address internal funder;
    address internal stranger;
    uint256 internal strangerPk;

    event ChannelClosed(address indexed channel, uint256 paidTotal, uint256 refunded);

    function setUp() public {
        (device, devicePk) = makeAddrAndKey("device");
        (gateway, gatewayPk) = makeAddrAndKey("gateway");
        (merchant, merchantPk) = makeAddrAndKey("merchant");
        (stranger, strangerPk) = makeAddrAndKey("stranger");

        // A funder distinct from every signer, mirroring M3/M4 where the deposit
        // comes from a wallet and the device holds no ETH at all.
        funder = makeAddr("funder");
        vm.deal(funder, 100 ether);

        pc = new PaymentChannel();
        vm.prank(funder);
        channelId = pc.open{value: DEPOSIT}(device, gateway, merchant);
    }

    // ---------------------------------------------------------------- helpers

    function _voucher(uint256 nonce, uint256 cumulativeAmount) internal view returns (Voucher memory) {
        return Voucher({
            channel: channelId,
            nonce: nonce,
            cumulativeAmount: cumulativeAmount,
            serviceRef: keccak256(abi.encodePacked("session-m4", nonce)),
            deadline: uint64(block.timestamp + 1 hours)
        });
    }

    function _digest(Voucher memory v) internal view returns (bytes32) {
        (, string memory name, string memory version, uint256 chainId, address verifyingContract,,) = pc.eip712Domain();
        bytes32 domainSeparator = keccak256(
            abi.encode(DOMAIN_TYPEHASH, keccak256(bytes(name)), keccak256(bytes(version)), chainId, verifyingContract)
        );
        bytes32 structHash =
            keccak256(abi.encode(VOUCHER_TYPEHASH, v.channel, v.nonce, v.cumulativeAmount, v.serviceRef, v.deadline));
        return MessageHashUtils.toTypedDataHash(domainSeparator, structHash);
    }

    function _sign(uint256 pk, Voucher memory v) internal view returns (bytes memory) {
        (uint8 vv, bytes32 r, bytes32 s) = vm.sign(pk, _digest(v));
        return abi.encodePacked(r, s, vv);
    }

    function _close(Voucher memory v) internal {
        vm.prank(gateway);
        pc.close(v, _sign(devicePk, v), _sign(gatewayPk, v), _sign(merchantPk, v));
    }

    function _state() internal view returns (uint256 paid, uint256 nonce, bool closed) {
        (,,,,, paid, nonce, closed) = pc.channels(channelId);
    }

    // ------------------------------------------------------------ happy path

    function test_closePaysMerchantAndRefundsFunder() public {
        uint256 total = 3 ether;
        uint256 funderBefore = funder.balance;

        _close(_voucher(1, total));

        assertEq(merchant.balance, total, "merchant receives the cumulative total");
        assertEq(funder.balance, funderBefore + (DEPOSIT - total), "funder gets the remainder");
        assertEq(address(pc).balance, 0, "channel is emptied");

        (uint256 paid, uint256 nonce, bool closed) = _state();
        assertEq(paid, total);
        assertEq(nonce, 1);
        assertTrue(closed);
    }

    function test_closeAfterPartialSettlementsPaysOnlyTheDelta() public {
        // The M4 shape in miniature: settle once mid-session, then close on a
        // later voucher. The merchant must end up with the cumulative total,
        // not the sum of the two cumulative figures.
        Voucher memory mid = _voucher(5, 1 ether);
        vm.prank(gateway);
        pc.settle(mid, _sign(devicePk, mid), _sign(gatewayPk, mid), _sign(merchantPk, mid));
        assertEq(merchant.balance, 1 ether);

        uint256 funderBefore = funder.balance;
        _close(_voucher(9, 4 ether));

        assertEq(merchant.balance, 4 ether, "cumulative, not additive");
        assertEq(funder.balance, funderBefore + (DEPOSIT - 4 ether));
        assertEq(address(pc).balance, 0);
    }

    function test_closeEmitsChannelClosed() public {
        vm.expectEmit(true, false, false, true, address(pc));
        emit ChannelClosed(channelId, 3 ether, DEPOSIT - 3 ether);
        _close(_voucher(1, 3 ether));
    }

    function test_closeWithFullDepositRefundsNothing() public {
        uint256 funderBefore = funder.balance;
        _close(_voucher(1, DEPOSIT));

        assertEq(merchant.balance, DEPOSIT);
        assertEq(funder.balance, funderBefore, "no refund when the deposit is fully spent");
        assertEq(address(pc).balance, 0);
    }

    // ------------------------------------------------------- H2 / abuse cases

    function test_closeRefundsFunderNotTheSubmitter() public {
        // The gateway submits and pays the gas. It must not thereby receive the
        // remainder — that would be exactly the unilateral claim on funds H2
        // says the design prevents.
        uint256 gatewayBefore = gateway.balance;
        uint256 funderBefore = funder.balance;

        _close(_voucher(1, 1 ether));

        assertEq(gateway.balance, gatewayBefore, "submitter gains nothing");
        assertEq(funder.balance, funderBefore + (DEPOSIT - 1 ether));
    }

    function test_closeRevertsWithoutDeviceSignature() public {
        Voucher memory v = _voucher(1, 1 ether);
        // Signed up front: _digest() calls pc.eip712Domain(), and an
        // expectRevert armed before that would match the view call instead.
        // Gateway forges the device slot with its own key.
        bytes memory forged = _sign(gatewayPk, v);
        bytes memory sigG = _sign(gatewayPk, v);
        bytes memory sigM = _sign(merchantPk, v);
        vm.prank(gateway);
        vm.expectRevert("bad device sig");
        pc.close(v, forged, sigG, sigM);
    }

    function test_closeRevertsWithoutMerchantSignature() public {
        Voucher memory v = _voucher(1, 1 ether);
        bytes memory sigD = _sign(devicePk, v);
        bytes memory sigG = _sign(gatewayPk, v);
        bytes memory wrong = _sign(strangerPk, v);
        vm.prank(gateway);
        vm.expectRevert("bad merchant sig");
        pc.close(v, sigD, sigG, wrong);
    }

    function test_cannotCloseTwice() public {
        _close(_voucher(1, 1 ether));
        Voucher memory again = _voucher(2, 2 ether);
        bytes memory sigD = _sign(devicePk, again);
        bytes memory sigG = _sign(gatewayPk, again);
        bytes memory sigM = _sign(merchantPk, again);
        vm.prank(gateway);
        vm.expectRevert("channel closed");
        pc.close(again, sigD, sigG, sigM);
    }

    function test_cannotSettleAfterClose() public {
        // The drained-channel case: without the `closed` guard a later voucher
        // would try to pay out of a balance that belongs to other channels.
        _close(_voucher(1, 1 ether));
        Voucher memory later = _voucher(2, 2 ether);
        bytes memory sigD = _sign(devicePk, later);
        bytes memory sigG = _sign(gatewayPk, later);
        bytes memory sigM = _sign(merchantPk, later);
        vm.prank(gateway);
        vm.expectRevert("channel closed");
        pc.settle(later, sigD, sigG, sigM);
    }

    function test_closeRevertsOnStaleNonce() public {
        Voucher memory first = _voucher(5, 1 ether);
        vm.prank(gateway);
        pc.settle(first, _sign(devicePk, first), _sign(gatewayPk, first), _sign(merchantPk, first));

        Voucher memory stale = _voucher(4, 2 ether);
        bytes memory sigD = _sign(devicePk, stale);
        bytes memory sigG = _sign(gatewayPk, stale);
        bytes memory sigM = _sign(merchantPk, stale);
        vm.prank(gateway);
        vm.expectRevert("stale nonce");
        pc.close(stale, sigD, sigG, sigM);
    }

    function test_closeRevertsWhenExpired() public {
        Voucher memory v = _voucher(1, 1 ether);
        bytes memory sigD = _sign(devicePk, v);
        bytes memory sigG = _sign(gatewayPk, v);
        bytes memory sigM = _sign(merchantPk, v);
        vm.warp(v.deadline + 1);
        vm.prank(gateway);
        vm.expectRevert("expired");
        pc.close(v, sigD, sigG, sigM);
    }

    function test_closeRevertsAboveDeposit() public {
        Voucher memory v = _voucher(1, DEPOSIT + 1);
        bytes memory sigD = _sign(devicePk, v);
        bytes memory sigG = _sign(gatewayPk, v);
        bytes memory sigM = _sign(merchantPk, v);
        vm.prank(gateway);
        vm.expectRevert("exceeds deposit");
        pc.close(v, sigD, sigG, sigM);
    }

    /// @dev Closing one channel must not reach another's escrow. Two channels
    ///      share the contract's balance, and only per-channel accounting keeps
    ///      them apart.
    function test_closingOneChannelLeavesAnotherIntact() public {
        vm.prank(funder);
        address other = pc.open{value: 5 ether}(device, gateway, merchant);

        _close(_voucher(1, 2 ether));

        assertEq(address(pc).balance, 5 ether, "the other channel's deposit is untouched");
        (,,,, uint256 otherDeposit, uint256 otherPaid,, bool otherClosed) = pc.channels(other);
        assertEq(otherDeposit, 5 ether);
        assertEq(otherPaid, 0);
        assertFalse(otherClosed);
    }
}
