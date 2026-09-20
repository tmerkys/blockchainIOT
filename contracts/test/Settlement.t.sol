// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {PaymentChannel} from "../src/PaymentChannel.sol";
import {Voucher} from "../src/VoucherLib.sol";

/// @notice M1 happy path: a fully signed voucher settles, pays the merchant the
///         delta, and advances channel state. Abuse cases live in GatewayAbuse.t.sol.
contract SettlementTest is Test {
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

    event Settled(
        address indexed channel, bytes32 indexed serviceRef, uint256 nonce, uint256 cumulativeAmount, uint256 delta
    );

    function setUp() public {
        (device, devicePk) = makeAddrAndKey("device");
        (gateway, gatewayPk) = makeAddrAndKey("gateway");
        (merchant, merchantPk) = makeAddrAndKey("merchant");

        pc = new PaymentChannel();
        channelId = pc.open{value: DEPOSIT}(device, gateway, merchant);
    }

    // ---------------------------------------------------------------- helpers

    function _voucher(uint256 nonce, uint256 cumulativeAmount) internal view returns (Voucher memory) {
        return Voucher({
            channel: channelId,
            nonce: nonce,
            cumulativeAmount: cumulativeAmount,
            serviceRef: keccak256(abi.encodePacked("session-1", nonce)),
            deadline: uint64(block.timestamp + 1 hours)
        });
    }

    /// @dev Rebuilds the digest from the contract's own ERC-5267 domain, so the
    ///      test signs exactly what the contract will verify.
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

    /// @dev Submits as the gateway, mirroring the real relayer.
    function _settle(Voucher memory v) internal {
        vm.prank(gateway);
        pc.settle(v, _sign(devicePk, v), _sign(gatewayPk, v), _sign(merchantPk, v));
    }

    function _paid() internal view returns (uint256 paid) {
        (,,,,, paid,,) = pc.channels(channelId);
    }

    function _nonce() internal view returns (uint256 nonce) {
        (,,,,,, nonce,) = pc.channels(channelId);
    }

    // ------------------------------------------------------------------ tests

    function test_openRegistersSignersAndDeposit() public view {
        (address d, address g, address m, address f, uint256 deposit, uint256 paid, uint256 nonce, bool closed) =
            pc.channels(channelId);

        assertEq(d, device);
        assertEq(g, gateway);
        assertEq(m, merchant);
        assertEq(deposit, DEPOSIT);
        assertEq(paid, 0);
        assertEq(nonce, 0);
        // open() records its caller as the refund destination; this test contract
        // paid the deposit, so it is the funder.
        assertEq(f, address(this));
        assertFalse(closed);
        assertEq(address(pc).balance, DEPOSIT);
    }

    function test_domainBindsChainIdAndVerifyingContract() public view {
        (, string memory name, string memory version, uint256 chainId, address verifyingContract,,) = pc.eip712Domain();

        assertEq(name, "MagisVoucher");
        assertEq(version, "1");
        assertEq(chainId, block.chainid);
        assertEq(verifyingContract, address(pc));
    }

    function test_settlePaysMerchant() public {
        uint256 before = merchant.balance;

        _settle(_voucher(1, 1 ether));

        assertEq(merchant.balance - before, 1 ether);
        assertEq(address(pc).balance, DEPOSIT - 1 ether);
    }

    function test_settleAdvancesChannelState() public {
        _settle(_voucher(3, 2 ether));

        assertEq(_paid(), 2 ether);
        assertEq(_nonce(), 3);
    }

    function test_settleEmitsSettled() public {
        Voucher memory v = _voucher(1, 1.5 ether);

        vm.expectEmit(true, true, true, true, address(pc));
        emit Settled(channelId, v.serviceRef, v.nonce, v.cumulativeAmount, 1.5 ether);

        _settle(v);
    }

    function test_successiveVouchersPayOnlyTheDelta() public {
        uint256 before = merchant.balance;

        _settle(_voucher(1, 1 ether));
        _settle(_voucher(2, 2.5 ether));

        assertEq(merchant.balance - before, 2.5 ether);
        assertEq(_paid(), 2.5 ether);
        assertEq(_nonce(), 2);
    }

    /// @dev The channel trick from CLAUDE.md: if voucher #7 is lost in transit,
    ///      #8 supersedes it and the merchant is still made whole in one tx.
    function test_lostVoucherIsSupersededByTheNextOne() public {
        uint256 before = merchant.balance;

        _settle(_voucher(7, 0.7 ether)); // arrives
        // voucher #8 (0.8 ether) never reaches the gateway
        _settle(_voucher(9, 0.9 ether));

        assertEq(merchant.balance - before, 0.9 ether);
        assertEq(_paid(), 0.9 ether);
        assertEq(_nonce(), 9);
    }

    /// @dev M4 in miniature: many off-chain ticks, one on-chain settlement.
    function test_manyTicksSettleAsOneTransaction() public {
        uint256 before = merchant.balance;
        uint256 tickValue = 0.001 ether;
        uint256 ticks = 200;

        _settle(_voucher(ticks, ticks * tickValue));

        assertEq(merchant.balance - before, ticks * tickValue);
        assertEq(_paid(), ticks * tickValue);
    }

    function test_settleAcceptsVoucherAtExactDeadline() public {
        Voucher memory v = _voucher(1, 1 ether);
        vm.warp(v.deadline);

        _settle(v);

        assertEq(_paid(), 1 ether);
    }

    function test_settleDrainsFullDeposit() public {
        uint256 before = merchant.balance;

        _settle(_voucher(1, DEPOSIT));

        assertEq(merchant.balance - before, DEPOSIT);
        assertEq(address(pc).balance, 0);
    }

    /// @dev Anyone may submit a fully signed voucher — the signatures, not the
    ///      sender, are the authority. The gateway is just who pays the gas.
    function test_anySubmitterMayRelayAFullySignedVoucher() public {
        Voucher memory v = _voucher(1, 1 ether);
        uint256 before = merchant.balance;

        vm.prank(makeAddr("stranger"));
        pc.settle(v, _sign(devicePk, v), _sign(gatewayPk, v), _sign(merchantPk, v));

        assertEq(merchant.balance - before, 1 ether);
    }
}
