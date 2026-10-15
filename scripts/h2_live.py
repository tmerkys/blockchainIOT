"""M6 live H2 test: attack the running gateway and the deployed contract.

    python scripts/h2_live.py

CLAUDE.md's M6 asks for the Foundry abuse suite "plus a live attempt from the
running gateway with the device signature stripped". This is that attempt, and
it is deliberately two attempts, because the two answer different questions.

**Attack through the API** asks what an honest gateway does when handed a
voucher it should not relay. The answer is a 422 and no transaction. That is
worth showing, and it is worth almost nothing as security: the gateway is the
adversary in the H2 threat model, and an adversary deletes its own checks. The
relayer's docstring says as much — rejecting off-chain "is an optimisation,
never the security boundary".

**Attack as the gateway** asks the real question. Here the script *is* the
malicious gateway: it holds the gateway key, it builds the calldata with the
gateway's own production code path (`Relayer.build_settle_tx`), it fills the
device slot with the only key it has, and then it bypasses every off-chain
check — including `estimate_gas`, which would otherwise catch the revert and
politely decline to spend gas — and puts the transaction on chain anyway.

The H2 claim is that this fails on the chain, not in the gateway's manners.
A mined transaction with `status = 0` and an escrow that has not moved is what
proves it, and nothing less does.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402
from hexbytes import HexBytes  # noqa: E402
from web3 import Web3  # noqa: E402
from web3.exceptions import ContractLogicError  # noqa: E402
from shared.rpc import rpc_host  # noqa: E402
from web3.logs import DISCARD  # noqa: E402

from gateway.relayer import Relayer, RelayerConfig  # noqa: E402
from scripts.e2e_m1 import load_env, require  # noqa: E402
from scripts.m2_setup import load_artifact, send  # noqa: E402
from shared.voucher import Voucher  # noqa: E402

FORCED_GAS = 300_000
"""Gas limit for the forced attack transaction. Hard-coded precisely because
`estimate_gas` refuses to produce one for a call that reverts — declining to
estimate is how the honest path avoids wasting gas, and stepping over it is
what makes this a real attack rather than a simulation of one."""

results: list[tuple[str, bool, str]] = []


def record(case: str, blocked: bool, detail: str) -> None:
    results.append((case, blocked, detail))
    print(f"  [{'BLOCKED' if blocked else 'NOT BLOCKED':^11}] {case}")
    print(f"                {detail}")


def voucher_for(channel: str, nonce: int, amount: int, deadline: int) -> Voucher:
    from eth_utils import keccak

    return Voucher(
        channel=channel,
        nonce=nonce,
        cumulativeAmount=amount,
        serviceRef=keccak(text=f"h2-live-{nonce}-{amount}"),
        deadline=deadline,
    )


def sign(voucher: Voucher, key: str, chain_id: int, contract: str) -> HexBytes:
    signable = encode_typed_data(
        full_message=voucher.to_typed_data(chain_id=chain_id, verifying_contract=contract)
    )
    return HexBytes(Account.sign_message(signable, key).signature)


def api_attack(url: str, body: dict[str, Any], case: str) -> None:
    """POST a malformed voucher at the live gateway and report what it does."""
    try:
        r = requests.post(f"{url}/voucher", json=body, timeout=60)
    except requests.RequestException as exc:
        record(case, False, f"gateway unreachable: {exc}")
        return

    try:
        payload = r.json()
    except ValueError:
        payload = {"raw": r.text[:200]}

    if r.status_code == 200 and payload.get("submitted"):
        record(case, False, f"gateway SUBMITTED it: tx {payload.get('txHash')}")
        return
    reason = payload.get("reason") or payload.get("detail") or r.text[:160]
    if isinstance(reason, list):  # pydantic validation errors
        reason = reason[0].get("msg", str(reason))
    record(case, True, f"HTTP {r.status_code}, no transaction — {reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M6: live H2 attack on the gateway + contract.")
    parser.add_argument("--deposit-eth", type=float, default=0.0000001, help="channel deposit")
    parser.add_argument("--channel", default=None, help="reuse an open channel instead of opening one")
    parser.add_argument("--skip-control", action="store_true", help="skip the honest control settlement")
    args = parser.parse_args(argv)

    env = load_env()
    rpc_url, contract_addr = require(env, "RPC_URL", "PAYMENT_CHANNEL_ADDRESS")
    chain_id = int(env["CHAIN_ID"])
    gateway_key, device_key_unused = env["GATEWAY_PRIVATE_KEY"], None
    merchant_key = env["MERCHANT_PRIVATE_KEY"]
    gateway_url = f"http://127.0.0.1:{env.get('GATEWAY_PORT', '8000')}"

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    contract_addr = w3.to_checksum_address(contract_addr)
    abi, _ = load_artifact()
    contract = w3.eth.contract(address=contract_addr, abi=abi)
    gw = Account.from_key(gateway_key)

    print("=== setup =========================================================")
    print(f"  chain            {chain_id} via {rpc_host(rpc_url)}")
    print(f"  contract         {contract_addr}")
    print(f"  gateway (attacker) {gw.address}")

    # The device key lives on the Pi and is not here. That is the point: this
    # script can hold every key the gateway holds and no more.
    device_addr = w3.to_checksum_address(env["DEVICE_ADDRESS"])
    print(f"  device           {device_addr}  (key NOT held by this script)")

    if args.channel:
        channel = w3.to_checksum_address(args.channel)
    else:
        deposit = w3.to_wei(args.deposit_eth, "ether")
        receipt = send(
            w3,
            gw,
            contract.functions.open(device_addr, gw.address, Account.from_key(merchant_key).address),
            "open()",
            value=deposit,
        )
        ev = contract.events.ChannelOpened().process_receipt(receipt, errors=DISCARD)
        channel = ev[0]["args"]["channel"]
    ch_before = contract.functions.channels(channel).call()
    print(f"  channel          {channel}")
    print(f"  deposit {ch_before[4]:,} wei, paid {ch_before[5]:,}, nonce {ch_before[6]}")

    deadline = w3.eth.get_block("latest")["timestamp"] + 3600
    v = voucher_for(channel, 1, ch_before[4] // 2, deadline)

    sig_gateway = sign(v, gateway_key, chain_id, contract_addr)
    sig_merchant = sign(v, merchant_key, chain_id, contract_addr)
    wire = {
        "voucher": {
            "channel": v.channel,
            "nonce": v.nonce,
            "cumulativeAmount": str(v.cumulativeAmount),
            "serviceRef": "0x" + v.serviceRef.hex(),
            "deadline": v.deadline,
        },
        "sigMerchant": sig_merchant.to_0x_hex(),
    }

    print("\n=== A. attacks through the live gateway API =======================")
    api_attack(
        gateway_url,
        {**wire, "sigDevice": "0x"},
        "device signature stripped entirely (empty)",
    )
    api_attack(
        gateway_url,
        {**wire, "sigDevice": sig_gateway.to_0x_hex()},
        "device slot filled with the gateway's own signature",
    )
    api_attack(
        gateway_url,
        {**wire, "sigDevice": ("0x" + "11" * 65)},
        "device signature replaced with 65 junk bytes",
    )

    print("\n=== B. the gateway itself attacks the contract ====================")
    print("  Using the gateway's production code path, its own key in the device")
    print("  slot, and no off-chain checks at all — estimate_gas included.\n")

    relayer = Relayer(
        RelayerConfig(
            rpc_url=rpc_url,
            chain_id=chain_id,
            payment_channel=contract_addr,
            gateway_private_key=gateway_key,
            submit=False,
        ),
        w3=w3,
    )
    # Real gateway calldata, with the device slot forged by the gateway.
    tx = relayer.build_settle_tx(v, sig_gateway, sig_gateway, sig_merchant)

    # Prove the honest path *would* have refused, then step over it.
    try:
        w3.eth.estimate_gas({**tx, "from": gw.address})
        print("  estimate_gas SUCCEEDED — the contract would accept this. H2 BROKEN.")
        estimate_blocked = False
    except ContractLogicError as exc:
        print(f"  estimate_gas refused: {exc}")
        print("  -> overriding the gas limit and submitting anyway.\n")
        estimate_blocked = True

    merchant_addr = Account.from_key(merchant_key).address
    merchant_before = w3.eth.get_balance(merchant_addr)
    contract_before = w3.eth.get_balance(contract_addr)

    tx = {
        **tx,
        "from": gw.address,
        "nonce": w3.eth.get_transaction_count(gw.address, "pending"),
        "gas": FORCED_GAS,
        **relayer._fee_fields(),
    }
    signed = gw.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  forced tx        {HexBytes(tx_hash).to_0x_hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)

    reason = "reverted"
    try:
        w3.eth.call({k: v2 for k, v2 in tx.items() if k in ("to", "from", "data", "value")},
                    block_identifier=receipt["blockNumber"])
    except ContractLogicError as exc:
        reason = str(exc)

    merchant_after = w3.eth.get_balance(merchant_addr)
    contract_after = w3.eth.get_balance(contract_addr)
    ch_after = contract.functions.channels(channel).call()

    print(f"  mined in block   {receipt['blockNumber']}, status {receipt['status']}, "
          f"gasUsed {receipt['gasUsed']:,}")
    record(
        "gateway forges the device signature and forces the tx on-chain",
        receipt["status"] == 0
        and merchant_after == merchant_before
        and ch_after[5] == ch_before[5],
        f"status=0 (reverted) — {reason}; merchant balance unchanged, channel paid still "
        f"{ch_after[5]:,} wei",
    )
    print(f"    escrow before/after   {contract_before:,} / {contract_after:,} wei")
    print(f"    merchant before/after {merchant_before:,} / {merchant_after:,} wei")
    print(f"    attack cost the gateway {receipt['gasUsed'] * receipt.get('effectiveGasPrice', 0):,} "
          "wei in burnt gas, and bought nothing")

    print("\n=== summary =======================================================")
    blocked = sum(1 for _, ok, _ in results if ok)
    for case, ok, _ in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {case}")
    print(f"\n  {blocked}/{len(results)} attacks blocked")
    print(f"  estimate_gas pre-check also refused: {estimate_blocked}")
    return 0 if blocked == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
