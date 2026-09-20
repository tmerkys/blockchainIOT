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
///
/// @dev    Scope limit, stated because it matters to anyone reading the H2 claim:
///         `close()` is *cooperative only*. It needs all three signatures, so no
///         single party — the gateway included — can close a channel or redirect a
///         refund. The cost is that it offers no protection against a counterparty
///         who simply stops signing: the funder's remaining deposit stays escrowed
///         until all three cooperate. A production channel answers that with a
///         unilateral close plus a challenge window, during which a counterparty can
///         publish a higher-nonce voucher to override a stale one. That machinery is
///         out of scope here (CLAUDE.md M4 is open / accumulate / close) and its
///         absence is a stated limitation of the prototype, not an oversight.
contract PaymentChannel is VoucherLib {
    using ECDSA for bytes32;

    struct Channel {
        address device; // signs the service fact
        address gateway; // relays and pays gas, never a unilateral spender
        address merchant; // charging station operator, receives the funds
        address funder; // escrowed the deposit; the only refund destination
        uint256 deposit; // total escrowed at open()
        uint256 paid; // cumulative amount already released to the merchant
        uint256 nonce; // highest voucher nonce settled so far
        bool closed; // once true, no further settle() or close()
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

    event ChannelClosed(address indexed channel, uint256 paidTotal, uint256 refunded);

    constructor() VoucherLib("MagisVoucher", "1") {}

    /// @notice Escrow `msg.value` and register the three signers for a new channel.
    /// @return channel The channel id, also emitted in `ChannelOpened`.
    function open(address device, address gateway, address merchant) external payable returns (address channel) {
        require(device != address(0) && gateway != address(0) && merchant != address(0), "zero signer");
        require(msg.value > 0, "empty deposit");

        channel = address(uint160(uint256(keccak256(abi.encode(device, gateway, merchant, _channelIndex++)))));

        channels[channel] = Channel({
            device: device,
            gateway: gateway,
            merchant: merchant,
            // Whoever escrowed the money is the only address a refund can ever
            // reach. Recording it here rather than assuming the device keeps
            // the refund away from a device that holds no ETH by design.
            funder: msg.sender,
            deposit: msg.value,
            paid: 0,
            nonce: 0,
            closed: false
        });

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
        uint256 delta = _applyVoucher(ch, v, sigDevice, sigGateway, sigMerchant);

        (bool ok,) = ch.merchant.call{value: delta}("");
        require(ok, "merchant transfer failed");
    }

    /// @notice Settle the final voucher and return the unspent deposit to the funder.
    /// @dev A *cooperative* close: it carries the same 3-of-3 signature requirement as
    ///      `settle()`, so the gateway can no more close a channel than it can spend from
    ///      one (H2). There is deliberately no unilateral path — see the note below.
    ///
    ///      This is the transaction the M4 amortisation claim rests on: 200 service ticks
    ///      accumulate off-chain as superseding vouchers and only the highest one is ever
    ///      submitted, here, together with the refund.
    function close(
        Voucher calldata v,
        bytes calldata sigDevice,
        bytes calldata sigGateway,
        bytes calldata sigMerchant
    ) external {
        Channel storage ch = channels[v.channel];
        uint256 delta = _applyVoucher(ch, v, sigDevice, sigGateway, sigMerchant);

        uint256 refund = ch.deposit - ch.paid;
        address merchant = ch.merchant;
        address funder = ch.funder;

        // Closed before any value moves: the two external calls below are the
        // only reentrancy surface, and both find `closed` already true.
        ch.closed = true;

        emit ChannelClosed(v.channel, ch.paid, refund);

        (bool paidOk,) = merchant.call{value: delta}("");
        require(paidOk, "merchant transfer failed");

        if (refund > 0) {
            // To the funder, never to msg.sender. The gateway submits this
            // transaction and pays its gas; letting it also receive the
            // remainder would hand it exactly the unilateral claim on funds
            // that H2 says the design prevents.
            (bool refundOk,) = funder.call{value: refund}("");
            require(refundOk, "refund failed");
        }
    }

    /// @dev Shared precondition checks and state update for `settle()` and `close()`.
    ///      Factored so the two can never drift apart: a check that guarded settlement
    ///      but not closing would be a way to move funds that the audit of `settle()`
    ///      alone would not reveal. Returns the delta owed to the merchant.
    function _applyVoucher(
        Channel storage ch,
        Voucher calldata v,
        bytes calldata sigDevice,
        bytes calldata sigGateway,
        bytes calldata sigMerchant
    ) private returns (uint256 delta) {
        require(ch.device != address(0), "unknown channel");
        require(!ch.closed, "channel closed");
        require(block.timestamp <= v.deadline, "expired");

        bytes32 digest = _hashTypedDataV4(_hashVoucher(v));
        require(digest.recoverCalldata(sigDevice) == ch.device, "bad device sig");
        require(digest.recoverCalldata(sigGateway) == ch.gateway, "bad gateway sig");
        require(digest.recoverCalldata(sigMerchant) == ch.merchant, "bad merchant sig");

        require(v.cumulativeAmount > ch.paid, "not monotonic");
        require(v.nonce > ch.nonce, "stale nonce");
        require(v.cumulativeAmount <= ch.deposit, "exceeds deposit");

        delta = v.cumulativeAmount - ch.paid;
        ch.paid = v.cumulativeAmount;
        ch.nonce = v.nonce;

        emit Settled(v.channel, v.serviceRef, v.nonce, v.cumulativeAmount, delta);
    }
}
