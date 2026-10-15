// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {VoucherLib, Voucher} from "../../src/VoucherLib.sol";

/// @title SingleSigChannel — measurement baseline, NOT a deployable design
/// @notice `PaymentChannel` with the 3-of-3 removed: `settle()` checks the
///         device's signature only, as in a classic unidirectional payment
///         channel. Everything else — storage layout, voucher, EIP-712 domain,
///         checks, event, transfer — is kept identical, so the gas difference
///         between the two is the cost of the two extra signatures and nothing
///         else. Used only by `experiments/throughput.py` (H2: "without
///         destroying throughput"), in simulation. It lives under test/ so it
///         can never be mistaken for, or deployed as, the contract under study.
///
/// @dev    In this contract whoever relays a device-signed voucher can settle
///         it — the property H2 says the 3-of-3 removes. That is the point of
///         the baseline, not an oversight.
contract SingleSigChannel is VoucherLib {
    using ECDSA for bytes32;

    struct Channel {
        address device;
        address gateway;
        address merchant;
        address funder;
        uint256 deposit;
        uint256 paid;
        uint256 nonce;
        bool closed;
    }

    mapping(address => Channel) public channels;
    uint256 private _channelIndex;

    event ChannelOpened(
        address indexed channel, address indexed device, address gateway, address merchant, uint256 deposit
    );

    event Settled(
        address indexed channel, bytes32 indexed serviceRef, uint256 nonce, uint256 cumulativeAmount, uint256 delta
    );

    constructor() VoucherLib("MagisVoucher", "1") {}

    function open(address device, address gateway, address merchant) external payable returns (address channel) {
        require(device != address(0) && gateway != address(0) && merchant != address(0), "zero signer");
        require(msg.value > 0, "empty deposit");

        channel = address(uint160(uint256(keccak256(abi.encode(device, gateway, merchant, _channelIndex++)))));

        channels[channel] = Channel({
            device: device,
            gateway: gateway,
            merchant: merchant,
            funder: msg.sender,
            deposit: msg.value,
            paid: 0,
            nonce: 0,
            closed: false
        });

        emit ChannelOpened(channel, device, gateway, merchant, msg.value);
    }

    function settle(Voucher calldata v, bytes calldata sigDevice) external {
        Channel storage ch = channels[v.channel];
        require(ch.device != address(0), "unknown channel");
        require(!ch.closed, "channel closed");
        require(block.timestamp <= v.deadline, "expired");

        bytes32 digest = _hashTypedDataV4(_hashVoucher(v));
        require(digest.recoverCalldata(sigDevice) == ch.device, "bad device sig");

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
