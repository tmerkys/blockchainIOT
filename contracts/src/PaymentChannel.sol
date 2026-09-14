// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {VoucherLib, Voucher} from "./VoucherLib.sol";

/// @title PaymentChannel
/// @notice Unidirectional 3-of-3 payment channel for IoT micropayments.
///         The device deposits, signs cumulative vouchers off-chain, and the
///         gateway submits the highest one on-chain. Settlement requires all
///         three signatures (device, gateway, merchant), so the gateway — which
///         pays the gas and therefore controls submission — can never move funds
///         on its own. This is the H2 property.
contract PaymentChannel is VoucherLib {
    using ECDSA for bytes32;

    struct Channel {
        address device; // signs the service fact, funds the channel
        address gateway; // relays and pays gas, never a unilateral spender
        address merchant; // charging station operator, receives the funds
        uint256 deposit; // total escrowed at open()
        uint256 paid; // cumulative amount already released to the merchant
        uint256 nonce; // highest voucher nonce settled so far
    }

    mapping(address => Channel) public channels;

    /// @dev Counter feeding the deterministic channel id, so two identical
    ///      participant triples still get distinct channels.
    uint256 private _channelIndex;

    event ChannelOpened(
        address indexed channel, address indexed device, address gateway, address merchant, uint256 deposit
    );

    event Settled(
        address indexed channel, bytes32 indexed serviceRef, uint256 nonce, uint256 cumulativeAmount, uint256 delta
    );

    constructor() VoucherLib("MagisVoucher", "1") {}

    /// @notice Escrow `msg.value` and register the three signers for a new channel.
    /// @return channel The channel id, also emitted in `ChannelOpened`.
    function open(address device, address gateway, address merchant) external payable returns (address channel) {
        require(device != address(0) && gateway != address(0) && merchant != address(0), "zero signer");
        require(msg.value > 0, "empty deposit");

        channel = address(uint160(uint256(keccak256(abi.encode(device, gateway, merchant, _channelIndex++)))));

        channels[channel] =
            Channel({device: device, gateway: gateway, merchant: merchant, deposit: msg.value, paid: 0, nonce: 0});

        emit ChannelOpened(channel, device, gateway, merchant, msg.value);
    }

    /// @notice Release the delta between the voucher's cumulative total and what
    ///         has already been paid. Requires all three signatures over the same
    ///         EIP-712 digest.
    function settle(
        Voucher calldata v,
        bytes calldata sigDevice,
        bytes calldata sigGateway,
        bytes calldata sigMerchant
    ) external {
        Channel storage ch = channels[v.channel];
        require(ch.device != address(0), "unknown channel");
        require(block.timestamp <= v.deadline, "expired");

        bytes32 digest = _hashTypedDataV4(_hashVoucher(v));
        require(digest.recoverCalldata(sigDevice) == ch.device, "bad device sig");
        require(digest.recoverCalldata(sigGateway) == ch.gateway, "bad gateway sig");
        require(digest.recoverCalldata(sigMerchant) == ch.merchant, "bad merchant sig");

        require(v.cumulativeAmount > ch.paid, "not monotonic");
        require(v.nonce > ch.nonce, "stale nonce");
        require(v.cumulativeAmount <= ch.deposit, "exceeds deposit");

        uint256 delta = v.cumulativeAmount - ch.paid;
        ch.paid = v.cumulativeAmount;
        ch.nonce = v.nonce;

        emit Settled(v.channel, v.serviceRef, v.nonce, v.cumulativeAmount, delta);

        (bool ok,) = ch.merchant.call{value: delta}("");
        require(ok, "merchant transfer failed");
    }
}
