"""M2 setup, laptop side: deploy the contract and open the Pi's channel.

    python scripts/m2_setup.py --device-address 0xTHE_PI_ADDRESS

M2 splits the device leg onto the Raspberry Pi. The Pi generates its own key
(`device/keygen.py`) and hands back nothing but an address; this script takes
that address, deploys a fresh `PaymentChannel`, opens a channel naming the Pi
as the `device` signer, and writes the result into the gateway's `.env`.

Who pays for what, and why it is not an accident:

  * The Pi signs. It holds a key, no ETH, and no chain state, and it never
    sends a transaction — not for `open()` and not for `settle()`. That is the
    invariant H1's cost argument and H2's security argument both stand on, and
    the easiest way to break it by mistake is to let a convenience script have
    the Pi fund its own channel.
  * `FUNDER_PRIVATE_KEY` escrows the deposit. Read it as the vehicle owner's
    wallet: the party who owns the money, distinct from the onboard unit that
    signs for it. It defaults to `DEVICE_PRIVATE_KEY` so M1's `.env` keeps
    working, but it is a different role and gets a different name.
  * The gateway pays gas for `settle()`, as before.

Deployment goes through web3 rather than `forge create` so the whole setup is
one command; the bytecode still comes from the Foundry artifact, so `forge
build` is a prerequisite and the deployed code is whatever `forge test` tested.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eth_account import Account  # noqa: E402
from hexbytes import HexBytes  # noqa: E402
from web3 import Web3  # noqa: E402
from web3.exceptions import Web3RPCError  # noqa: E402
from web3.logs import DISCARD  # noqa: E402

from scripts.e2e_m1 import ARTIFACT, ENV_FILE, load_env, require  # noqa: E402
from shared.rpc import rpc_host  # noqa: E402

DEPLOY_TIMEOUT_S = 120
NONCE_RETRIES = 3
NONCE_BACKOFF_S = 3.0


def load_artifact() -> tuple[list[dict[str, Any]], str]:
    """ABI and creation bytecode from `forge build` output.

    No fallback here, unlike `e2e_m1.py`: that script only needed an ABI to
    decode with, and a hand-written one is good enough for that. Deploying
    needs the actual compiled bytecode, and there is no honest substitute.
    """
    if not ARTIFACT.exists():
        raise SystemExit(
            f"no {ARTIFACT.relative_to(REPO_ROOT)}\n"
            "Run `forge build` in contracts/ first — the deployed bytecode has to be "
            "the bytecode `forge test` tested."
        )
    artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    return artifact["abi"], artifact["bytecode"]["object"]


def send(w3: Web3, account: Any, call: Any, what: str, value: int = 0) -> dict[str, Any]:
    """Build, sign, broadcast and await one provisioning transaction.

    `from` goes in before `build_transaction`, not after: web3 estimates gas
    while building, and an estimate with no sender runs as the zero address,
    which holds no ETH and fails any call carrying `value`.

    The nonce is read from the *pending* block and the send is retried on
    "nonce too low". A public endpoint like Base Sepolia is a load-balanced
    pool, and the node answering `eth_getTransactionCount` is often not the
    one that just accepted the previous transaction — so holding a receipt is
    no guarantee the next nonce lookup has caught up. Against a local anvil
    this never happens, which is exactly why it was not already handled.
    """
    last: Exception | None = None
    for attempt in range(NONCE_RETRIES):
        tx = call.build_transaction(
            {
                "from": account.address,
                "value": value,
                "nonce": w3.eth.get_transaction_count(account.address, "pending"),
                "chainId": w3.eth.chain_id,
            }
        )
        signed = account.sign_transaction(tx)
        try:
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            break
        except Web3RPCError as exc:
            if "nonce" not in str(exc).lower() or attempt == NONCE_RETRIES - 1:
                raise
            last = exc
            print(f"  {what}: {exc}; refetching nonce (attempt {attempt + 2})")
            time.sleep(NONCE_BACKOFF_S)
    else:  # pragma: no cover - the loop always breaks or raises
        raise SystemExit(f"{what}: could not settle on a nonce: {last}")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=DEPLOY_TIMEOUT_S)
    if receipt["status"] != 1:
        raise SystemExit(f"{what} reverted: {HexBytes(tx_hash).to_0x_hex()}")
    print(f"  {what}: tx {HexBytes(tx_hash).to_0x_hex()}  gasUsed {receipt['gasUsed']:,}")
    return receipt


def deploy(w3: Web3, funder: Any, abi: list[dict[str, Any]], bytecode: str) -> Any:
    """Deploy a fresh `PaymentChannel` and return the bound contract."""
    factory = w3.eth.contract(abi=abi, bytecode=bytecode)
    receipt = send(w3, funder, factory.constructor(), "deploy")
    address = receipt["contractAddress"]
    print(f"  PaymentChannel at {address}")
    return w3.eth.contract(address=address, abi=abi)


def open_channel(
    w3: Web3,
    contract: Any,
    funder: Any,
    device: str,
    gateway: str,
    merchant: str,
    deposit: int,
) -> str:
    """Escrow the deposit for the Pi and return the channel id.

    `open()` is `payable` and takes the device as an argument rather than
    reading `msg.sender`, which is exactly what lets the funder open a channel
    on behalf of a device that will never hold ETH.
    """
    receipt = send(
        w3,
        funder,
        contract.functions.open(device, gateway, merchant),
        "open()",
        value=deposit,
    )
    events = contract.events.ChannelOpened().process_receipt(receipt, errors=DISCARD)
    if not events:
        raise SystemExit("open() mined but emitted no ChannelOpened — wrong ABI?")
    args = events[0]["args"]
    print(f"  channel {args['channel']}  deposit {w3.from_wei(args['deposit'], 'ether')} ETH")
    return args["channel"]


def update_env(path: Path, updates: dict[str, str]) -> None:
    """Rewrite `KEY=value` lines in place, preserving comments and order.

    In place rather than regenerated: `.env` is gitignored, so it is the one
    file in this repo with no history to recover from if a script flattens it.
    Keys that are not already present are appended under a heading.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)

    for i, line in enumerate(lines):
        match = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=)", line)
        if match and match.group(2) in remaining:
            key = match.group(2)
            lines[i] = f"{match.group(1)}{key}{match.group(3)}{remaining.pop(key)}"

    if remaining:
        lines += ["", "# --- added by scripts/m2_setup.py ---"]
        lines += [f"{k}={v}" for k, v in remaining.items()]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for key, value in updates.items():
        print(f"  {path.name}: {key}={value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy and open the Pi's channel (M2).")
    parser.add_argument(
        "--device-address",
        required=True,
        help="the Pi's address, from `python device/keygen.py` run on the Pi",
    )
    parser.add_argument("--deposit-eth", type=float, default=1.0, help="channel deposit")
    parser.add_argument(
        "--contract",
        default=None,
        help="reuse an already-deployed PaymentChannel instead of deploying a new one",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ENV_FILE,
        help="gateway env file to read and update; e.g. .env.sepolia for the L1 baseline",
    )
    parser.add_argument(
        "--gateway-host",
        default="0.0.0.0",
        help="interface uvicorn binds to; 0.0.0.0 is what lets the Pi reach it",
    )
    args = parser.parse_args(argv)

    env = load_env(args.env_file)
    (rpc_url,) = require(env, "RPC_URL")
    chain_id = int(env.get("CHAIN_ID", "31337"))
    gateway_key, merchant_key = require(env, "GATEWAY_PRIVATE_KEY", "MERCHANT_PRIVATE_KEY")
    # The device's own key is not a funder; see the module docstring.
    funder_key = env.get("FUNDER_PRIVATE_KEY") or require(env, "DEVICE_PRIVATE_KEY")[0]

    funder = Account.from_key(funder_key)
    gateway = Account.from_key(gateway_key)
    merchant = Account.from_key(merchant_key)

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
    if not w3.is_connected():
        raise SystemExit(f"no RPC at {rpc_host(rpc_url)} — is anvil running?")
    observed = w3.eth.chain_id
    if observed != chain_id:
        raise SystemExit(f"chain id mismatch: .env says {chain_id}, the node says {observed}")

    device = w3.to_checksum_address(args.device_address)
    if device in {funder.address, gateway.address, merchant.address}:
        # Not a contract rule — `settle()` would happily verify three signatures
        # from one key. It is the experiment's rule: a channel whose signers are
        # not three distinct parties cannot demonstrate H2 either way.
        raise SystemExit(f"{device} is already one of the other signers; use the Pi's own key")
    if w3.eth.get_balance(device) != 0:
        print(f"  note: {device} holds ETH. Harmless, but the Pi never needs any.")

    print("=== 1. preflight =================================================")
    print(f"  RPC {rpc_host(rpc_url)}  chainId {observed}  block {w3.eth.block_number}")
    print(f"  device (Pi)  {device}   <- signs, never pays")
    print(f"  funder       {funder.address}   <- escrows the deposit")
    print(f"  gateway      {gateway.address}   <- pays gas")
    print(f"  merchant     {merchant.address}   <- receives funds")

    abi, bytecode = load_artifact()

    print("\n=== 2. PaymentChannel ============================================")
    if args.contract:
        # Reuse rather than redeploy. On a public testnet the faucet budget is
        # finite and a deploy is by far the most expensive transaction here, so
        # a failure *after* the deploy must not force paying for it twice.
        address = w3.to_checksum_address(args.contract)
        if not w3.eth.get_code(address):
            raise SystemExit(f"no contract code at {address} on chain {observed}")
        contract = w3.eth.contract(address=address, abi=abi)
        print(f"  reusing deployment at {address}")
    else:
        contract = deploy(w3, funder, abi, bytecode)

    print("\n=== 3. open the Pi's channel =====================================")
    channel = open_channel(
        w3,
        contract,
        funder,
        device,
        gateway.address,
        merchant.address,
        w3.to_wei(args.deposit_eth, "ether"),
    )

    print("\n=== 4. update the gateway's .env =================================")
    update_env(
        args.env_file,
        {
            "PAYMENT_CHANNEL_ADDRESS": contract.address,
            "DEVICE_ADDRESS": device,
            "CHANNEL_ADDRESS": channel,
            "GATEWAY_HOST": args.gateway_host,
        },
    )

    port = env.get("GATEWAY_PORT", "8000")
    print("\n=== 5. next ======================================================")
    print("  On the laptop, start the gateway on all interfaces:")
    print(f"    uvicorn gateway.api:app --host {args.gateway_host} --port {port}")
    print("\n  On the Pi, put these in ~/magis/.env:")
    print(f"    CHANNEL_ADDRESS={channel}")
    print(f"    VERIFYING_CONTRACT={contract.address}")
    print(f"    CHAIN_ID={chain_id}")
    print("    DEVICE_KEYSTORE=/home/merkys/magis/device/keystore/device.json")
    print("    DEVICE_KEYSTORE_PASSWORD=<the passphrase you chose on the Pi>")
    print(f"    GATEWAY_URL=http://<laptop-ip>:{port}")
    # Deliberately not echoed. This was harmless while every key in .env was an
    # anvil dev key, and stopped being harmless the moment the same line ran
    # against a public testnet: printed secrets end up in scrollback, CI logs
    # and pasted transcripts, and a "testnet-only" key is still a key.
    print("    MERCHANT_PRIVATE_KEY=<copy from the gateway's .env; not printed here>")
    print("\n  Then, on the Pi:  ./venv/bin/python device/run_tick.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
