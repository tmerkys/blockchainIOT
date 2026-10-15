// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {PaymentChannel} from "../src/PaymentChannel.sol";
import {Voucher} from "../src/VoucherLib.sol";

/// @notice Shared harness for the M6 abuse suites (GatewayAbuse, Replay).
///
///         The one capability these need beyond the happy-path tests is signing
///         against a domain that is *not* this contract's — a different chainId,
///         a different verifyingContract. Those are the two fields EIP-712 binds a
///         signature to, and a test that can only produce correctly-domained
///         signatures cannot show that binding does anything.
///
/// @dev    Ordering trap, learned the hard way: build every signature BEFORE
///         `vm.expectRevert`. `_digest` calls `pc.eip712Domain()`, and an
///         expectRevert armed beforehand latches onto that view call instead of
///         the state-changing one under test — which then reports "did not revert"
///         while the contract is behaving perfectly.
abstract contract ChannelTestBase is Test {
    bytes32 internal constant DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 internal constant VOUCHER_TYPEHASH = keccak256(
        "Voucher(address channel,uint256 nonce,uint256 cumulativeAmount,bytes32 serviceRef,uint64 deadline)"
    );

    uint256 internal constant DEPOSIT = 10 ether;

    PaymentChannel internal pc;
    address internal channelId;

    address internal device;
    uint256 internal devicePk;
    address internal gateway;
    uint256 internal gatewayPk;
    address internal merchant;
    uint256 internal merchantPk;
    address internal attacker;
    uint256 internal attackerPk;
    address internal funder;

    function setUp() public virtual {
        (device, devicePk) = makeAddrAndKey("device");
        (gateway, gatewayPk) = makeAddrAndKey("gateway");
        (merchant, merchantPk) = makeAddrAndKey("merchant");
        (attacker, attackerPk) = makeAddrAndKey("attacker");

        funder = makeAddr("funder");
        vm.deal(funder, 100 ether);

        pc = new PaymentChannel();
        vm.prank(funder);
        channelId = pc.open{value: DEPOSIT}(device, gateway, merchant);
    }

    // ---------------------------------------------------------------- vouchers

    function _voucherFor(address channel, uint256 nonce, uint256 amount)
        internal
        view
        returns (Voucher memory)
    {
        return Voucher({
            channel: channel,
            nonce: nonce,
            cumulativeAmount: amount,
            serviceRef: keccak256(abi.encodePacked("session-m6", nonce, amount)),
            deadline: uint64(block.timestamp + 1 hours)
        });
    }

    function _voucher(uint256 nonce, uint256 amount) internal view returns (Voucher memory) {
        return _voucherFor(channelId, nonce, amount);
    }

    // ----------------------------------------------------------------- digests

    function _structHash(Voucher memory v) internal pure returns (bytes32) {
        return keccak256(
            abi.encode(VOUCHER_TYPEHASH, v.channel, v.nonce, v.cumulativeAmount, v.serviceRef, v.deadline)
        );
    }

    /// @dev The digest the contract itself will verify against.
    function _digest(Voucher memory v) internal view returns (bytes32) {
        (, string memory name, string memory version, uint256 chainId, address verifyingContract,,) = pc.eip712Domain();
        return _digestIn(v, name, version, chainId, verifyingContract);
    }

    /// @dev A digest under an arbitrary domain, for the cross-chain and
    ///      cross-contract replay tests. This is what an attacker who harvested a
    ///      signature from another deployment would be holding.
    function _digestIn(
        Voucher memory v,
        string memory name,
        string memory version,
        uint256 chainId,
        address verifyingContract
    ) internal pure returns (bytes32) {
        bytes32 domainSeparator = keccak256(
            abi.encode(DOMAIN_TYPEHASH, keccak256(bytes(name)), keccak256(bytes(version)), chainId, verifyingContract)
        );
        return MessageHashUtils.toTypedDataHash(domainSeparator, _structHash(v));
    }

    // --------------------------------------------------------------- signatures

    function _signDigest(uint256 pk, bytes32 digest) internal pure returns (bytes memory) {
        (uint8 vv, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        return abi.encodePacked(r, s, vv);
    }

    function _sign(uint256 pk, Voucher memory v) internal view returns (bytes memory) {
        return _signDigest(pk, _digest(v));
    }

    /// @dev All three legitimate signatures, precomputed. Call before expectRevert.
    function _sigs(Voucher memory v)
        internal
        view
        returns (bytes memory d, bytes memory g, bytes memory m)
    {
        d = _sign(devicePk, v);
        g = _sign(gatewayPk, v);
        m = _sign(merchantPk, v);
    }

    /// @dev Submits as the gateway, mirroring the real relayer.
    function _settle(Voucher memory v) internal {
        (bytes memory d, bytes memory g, bytes memory m) = _sigs(v);
        vm.prank(gateway);
        pc.settle(v, d, g, m);
    }

    // -------------------------------------------------------------------- state

    function _paid() internal view returns (uint256 paid) {
        (,,,,, paid,,) = pc.channels(channelId);
    }

    function _nonce() internal view returns (uint256 nonce) {
        (,,,,,, nonce,) = pc.channels(channelId);
    }
}
