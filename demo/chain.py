"""Chain-side helpers for the demo: channels, balances, and the H2 attack.

Reuses the project's own tooling: `scripts/m2_setup.py` for deploy/open/send,
`scripts/h2_live.py` for the attack voucher and signatures, and the gateway's
production `Relayer.build_settle_tx` for the forged calldata. Nothing here is
new security logic; it is the M6 live attack, emitted step by step.
"""

from __future__ import annotations

from typing import Any, Callable

import requests
from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import ContractLogicError

from gateway.relayer import Relayer, RelayerConfig
from scripts.h2_live import FORCED_GAS, sign, voucher_for
from scripts.m2_setup import load_artifact, open_channel, send  # noqa: F401

Emit = Callable[[dict[str, Any]], None]

CHANNEL_FIELDS = ("device", "gateway", "merchant", "funder", "deposit", "paid", "nonce", "closed")


def revert_reason(exc: ContractLogicError) -> str:
    """'execution reverted: bad device sig', without web3's raw ABI-encoded data."""
    return getattr(exc, "message", None) or str(exc)


class Chain:
    def __init__(self, rpc_url: str, chain_id: int, contract: str | None = None) -> None:
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        self.abi, self.bytecode = load_artifact()
        self.contract = None
        if contract:
            self.bind(contract)

    def bind(self, address: str) -> None:
        self.contract = self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=self.abi)

    @property
    def address(self) -> str:
        assert self.contract is not None
        return self.contract.address

    def channel(self, channel: str) -> dict[str, Any]:
        assert self.contract is not None
        values = self.contract.functions.channels(Web3.to_checksum_address(channel)).call()
        return dict(zip(CHANNEL_FIELDS, values))

    def balance(self, address: str) -> int:
        return self.w3.eth.get_balance(Web3.to_checksum_address(address))

    def open(self, funder: Any, device: str, gateway: str, merchant: str, deposit: int) -> dict[str, Any]:
        """`open()` from the funder's wallet; returns the channel and its receipt facts."""
        assert self.contract is not None
        receipt = send(
            self.w3,
            funder,
            self.contract.functions.open(device, gateway, merchant),
            "open()",
            value=deposit,
        )
        from web3.logs import DISCARD

        events = self.contract.events.ChannelOpened().process_receipt(receipt, errors=DISCARD)
        if not events:
            raise RuntimeError("open() mined but emitted no ChannelOpened")
        return {
            "channel": events[0]["args"]["channel"],
            "deposit": deposit,
            "txHash": HexBytes(receipt["transactionHash"]).to_0x_hex(),
            "gasUsed": receipt["gasUsed"],
            "blockNumber": receipt["blockNumber"],
        }

    def attack(
        self,
        *,
        channel: str,
        gateway_key: str,
        merchant_key: str,
        gateway_url: str,
        emit: Emit,
    ) -> None:
        """The gateway tries to pay out half the remaining escrow with no device signature.

        A. Through the running gateway API, with the gateway's own signature in
           the device slot: an honest gateway refuses (HTTP 422).
        B. As the gateway itself: production calldata, device slot forged,
           estimate_gas stepped over, transaction forced on-chain. The contract
           reverts it; balances and the channel do not move.
        """
        assert self.contract is not None
        w3 = self.w3
        gw = Account.from_key(gateway_key)
        merchant_addr = Account.from_key(merchant_key).address

        before = self.channel(channel)
        escrow = before["deposit"] - before["paid"]
        amount = before["paid"] + escrow // 2
        deadline = w3.eth.get_block("latest")["timestamp"] + 3600
        v = voucher_for(Web3.to_checksum_address(channel), before["nonce"] + 1, amount, deadline)
        sig_gateway = sign(v, gateway_key, self.chain_id, self.address)
        sig_merchant = sign(v, merchant_key, self.chain_id, self.address)

        emit(
            {
                "type": "atk.start",
                "channel": channel,
                "gateway": gw.address,
                "device": before["device"],
                "escrow": str(escrow),
                "paid": str(before["paid"]),
                "target": str(amount - before["paid"]),
            }
        )

        # --- A: the live gateway API -----------------------------------------
        body = {
            "voucher": {
                "channel": v.channel,
                "nonce": v.nonce,
                "cumulativeAmount": str(v.cumulativeAmount),
                "serviceRef": "0x" + v.serviceRef.hex(),
                "deadline": v.deadline,
            },
            "sigDevice": sig_gateway.to_0x_hex(),
            "sigMerchant": sig_merchant.to_0x_hex(),
        }
        try:
            r = requests.post(f"{gateway_url}/voucher", json=body, timeout=60)
            try:
                payload = r.json()
            except ValueError:
                payload = {"raw": r.text[:200]}
            reason = payload.get("reason") or payload.get("detail") or r.text[:200]
            if isinstance(reason, list):
                reason = reason[0].get("msg", str(reason))
            emit(
                {
                    "type": "atk.api",
                    "status": r.status_code,
                    "submitted": bool(payload.get("submitted")),
                    "reason": str(reason),
                }
            )
        except requests.RequestException as exc:
            emit({"type": "atk.api", "status": None, "submitted": False, "reason": f"unreachable: {exc}"})

        # --- B: the gateway attacks the contract directly ---------------------
        relayer = Relayer(
            RelayerConfig(
                rpc_url=self.rpc_url,
                chain_id=self.chain_id,
                payment_channel=self.address,
                gateway_private_key=gateway_key,
                submit=False,
            ),
            w3=w3,
        )
        tx = relayer.build_settle_tx(v, sig_gateway, sig_gateway, sig_merchant)
        try:
            w3.eth.estimate_gas({**tx, "from": gw.address})
            emit({"type": "atk.estimate", "refused": False, "reason": "estimate_gas accepted it"})
        except ContractLogicError as exc:
            emit({"type": "atk.estimate", "refused": True, "reason": revert_reason(exc)})

        contract_before = self.balance(self.address)
        merchant_before = self.balance(merchant_addr)
        tx = {
            **tx,
            "from": gw.address,
            "nonce": w3.eth.get_transaction_count(gw.address, "pending"),
            "gas": FORCED_GAS,
            **relayer._fee_fields(),
        }
        tx_hash = w3.eth.send_raw_transaction(gw.sign_transaction(tx).raw_transaction)
        emit({"type": "atk.forced", "txHash": HexBytes(tx_hash).to_0x_hex()})
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)

        reason = "reverted"
        try:
            w3.eth.call(
                {k: tx[k] for k in ("to", "from", "data", "value") if k in tx},
                block_identifier=receipt["blockNumber"],
            )
        except ContractLogicError as exc:
            reason = revert_reason(exc)

        after = self.channel(channel)
        emit(
            {
                "type": "atk.mined",
                "txHash": HexBytes(tx_hash).to_0x_hex(),
                "blockNumber": receipt["blockNumber"],
                "status": receipt["status"],
                "gasUsed": receipt["gasUsed"],
                "burnt_wei": str(receipt["gasUsed"] * receipt.get("effectiveGasPrice", 0)),
                "reason": reason,
                "escrow_before": str(contract_before),
                "escrow_after": str(self.balance(self.address)),
                "merchant_before": str(merchant_before),
                "merchant_after": str(self.balance(merchant_addr)),
                "paid_before": str(before["paid"]),
                "paid_after": str(after["paid"]),
                "blocked": receipt["status"] == 0 and after["paid"] == before["paid"],
            }
        )
