"""M1 end-to-end: sensor event on this machine -> `Settled` on the chain.

    python scripts/e2e_m1.py

CLAUDE.md's M1 is "done when one command produces a `Settled` event and prints
t0-t4". This is that command. Everything runs on one box against anvil, all
three signers are local keys, and the transport is a real HTTP POST over
loopback - M2 is what moves the device leg onto the Pi, at which point only the
gateway URL the agent points at has to change.

What it does, in order:

1.  Opens a channel on-chain: `PaymentChannel.open(device, gateway, merchant)`
    with a deposit. The channel id comes back in the `ChannelOpened` event, not
    from the return value - a transaction receipt carries logs, not returns.
2.  Starts `uvicorn gateway.api:app` in a subprocess, with that channel's
    parameters in its environment, and waits for `GET /health` to go green.
3.  Runs one device tick: the simulated meter fires (t0), the agent signs the
    voucher (t1), and `HttpTransport` POSTs it to the gateway.
4.  The gateway verifies the device signature, co-signs, and submits the real
    `settle()` transaction with its own key (t2, t3, t4).
5.  Decodes `Settled` out of the receipt and prints the marks, the latency
    decomposition, and the gas numbers.

The one mark that is not yet what it will be: t0-t4 all come from this
machine's clock, so the clock-skew warning in CLAUDE.md does not bite here. It
bites from M2 onward, and the round-trip this script prints is the sanity check
that catches it.

Note on who pays for `open()`: the deposit comes from the device account,
matching PaymentChannel.sol's own framing ("the device deposits"). That is a
one-off provisioning transaction outside the measured t0-t4 path - the Pi still
never pays gas per settlement, which is the invariant H1 and H2 rest on.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
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
from web3.logs import DISCARD  # noqa: E402

from device.agent import AgentConfig, DeviceAgent  # noqa: E402
from device.sensor import KwhMeter  # noqa: E402
from device.transport.http_client import GatewayError, HttpTransport  # noqa: E402
from shared.voucher import Voucher  # noqa: E402

ENV_FILE = REPO_ROOT / ".env"
ARTIFACT = REPO_ROOT / "contracts" / "out" / "PaymentChannel.sol" / "PaymentChannel.json"

HEALTH_TIMEOUT_S = 30.0
HEALTH_POLL_S = 0.25

# Used only if `forge build` has not run - contracts/out/ is gitignored, so a
# fresh clone has no artifact. Covers just what this script calls and decodes;
# the relayer carries its own `settle` fragment.
FALLBACK_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "open",
        "stateMutability": "payable",
        "inputs": [
            {"name": "device", "type": "address"},
            {"name": "gateway", "type": "address"},
            {"name": "merchant", "type": "address"},
        ],
        "outputs": [{"name": "channel", "type": "address"}],
    },
    {
        "type": "function",
        "name": "channels",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [
            {"name": "device", "type": "address"},
            {"name": "gateway", "type": "address"},
            {"name": "merchant", "type": "address"},
            {"name": "deposit", "type": "uint256"},
            {"name": "paid", "type": "uint256"},
            {"name": "nonce", "type": "uint256"},
        ],
    },
    {
        "type": "event",
        "name": "ChannelOpened",
        "anonymous": False,
        "inputs": [
            {"name": "channel", "type": "address", "indexed": True},
            {"name": "device", "type": "address", "indexed": True},
            {"name": "gateway", "type": "address", "indexed": False},
            {"name": "merchant", "type": "address", "indexed": False},
            {"name": "deposit", "type": "uint256", "indexed": False},
        ],
    },
    {
        "type": "event",
        "name": "Settled",
        "anonymous": False,
        "inputs": [
            {"name": "channel", "type": "address", "indexed": True},
            {"name": "serviceRef", "type": "bytes32", "indexed": True},
            {"name": "nonce", "type": "uint256", "indexed": False},
            {"name": "cumulativeAmount", "type": "uint256", "indexed": False},
            {"name": "delta", "type": "uint256", "indexed": False},
        ],
    },
]


# --- plumbing --------------------------------------------------------------


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    """Read `.env` into a dict and into `os.environ`.

    Hand-rolled rather than pulling in python-dotenv: this is the only place
    the project reads a dotenv file, and the format it needs is `KEY=value`.
    Real environment variables win, so a run can be overridden without editing
    the file.
    """
    if not path.exists():
        raise SystemExit(f"no {path} - copy .env.example and fill it in")

    values: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"{path}:{lineno}: expected KEY=value, got {raw!r}")
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        values[key] = value
        os.environ.setdefault(key, value)
    return values


def require(env: dict[str, str], *names: str) -> list[str]:
    missing = [n for n in names if not env.get(n)]
    if missing:
        raise SystemExit(f"{ENV_FILE} is missing: {', '.join(missing)}")
    return [env[n] for n in names]


def load_abi() -> list[dict[str, Any]]:
    if ARTIFACT.exists():
        return json.loads(ARTIFACT.read_text(encoding="utf-8"))["abi"]
    print(f"  note: no {ARTIFACT.relative_to(REPO_ROOT)} (run `forge build`); using fallback ABI")
    return FALLBACK_ABI


def fmt_ts(t: float | None) -> str:
    if t is None:
        return "     --     "
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def fmt_ms(a: float | None, b: float | None) -> str:
    if a is None or b is None:
        return "      -- "
    return f"{(a - b) * 1000:8.1f} ms"


# --- steps -----------------------------------------------------------------


def preflight(w3: Web3, chain_id: int, payment_channel: str) -> None:
    """Fail before spending anything if the chain is not what `.env` claims.

    A chainId mismatch is the failure mode worth catching loudest: it sits
    inside the EIP-712 domain, so every signature would verify locally and be
    worthless on-chain.
    """
    if not w3.is_connected():
        raise SystemExit(f"no RPC at {w3.provider.endpoint_uri} - is anvil running?")

    observed = w3.eth.chain_id
    if observed != chain_id:
        raise SystemExit(f"chain id mismatch: .env says {chain_id}, the node says {observed}")

    if not w3.eth.get_code(payment_channel):
        raise SystemExit(f"no contract code at {payment_channel} - deploy PaymentChannel first")

    print(f"  RPC {w3.provider.endpoint_uri}  chainId {observed}  block {w3.eth.block_number}")
    print(f"  PaymentChannel at {payment_channel}")


def open_channel(
    w3: Web3,
    contract: Any,
    device: Any,
    gateway: str,
    merchant: str,
    deposit_wei: int,
) -> str:
    """Escrow the deposit and return the new channel id."""
    tx = contract.functions.open(device.address, gateway, merchant).build_transaction(
        {
            "from": device.address,
            "value": deposit_wei,
            "nonce": w3.eth.get_transaction_count(device.address),
            "chainId": w3.eth.chain_id,
        }
    )
    signed = device.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt["status"] != 1:
        raise SystemExit(f"open() reverted: {HexBytes(tx_hash).to_0x_hex()}")

    # The channel id is a return value, and receipts do not carry return values.
    # The event is the only way to read it back after the fact.
    events = contract.events.ChannelOpened().process_receipt(receipt, errors=DISCARD)
    if not events:
        raise SystemExit("open() mined but emitted no ChannelOpened - wrong ABI?")
    args = events[0]["args"]

    print(f"  tx {HexBytes(tx_hash).to_0x_hex()}  gasUsed {receipt['gasUsed']:,}")
    print(f"  channel {args['channel']}  deposit {w3.from_wei(args['deposit'], 'ether')} ETH")
    return args["channel"]


def start_gateway(channel_env: dict[str, str], host: str, port: int) -> subprocess.Popen:
    """Launch `uvicorn gateway.api:app` with this run's configuration."""
    child_env = os.environ.copy()
    child_env.update(channel_env)
    child_env["PYTHONUNBUFFERED"] = "1"

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "gateway.api:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "info",
    ]
    print(f"  $ {' '.join(cmd)}")
    return subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=child_env)


def await_health(url: str, proc: subprocess.Popen) -> dict[str, Any]:
    """Poll `/health` until the gateway reports ready, or give up.

    Checking the reported facts rather than just a 200: a gateway that answers
    while pointed at the wrong chain would produce a full run of invalid rows.
    """
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    last: Any = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"gateway exited during startup (code {proc.returncode})")
        try:
            response = requests.get(f"{url}/health", timeout=2.0)
            last = response.json()
            if last.get("status") == "ok":
                return last
        except requests.RequestException as exc:
            last = str(exc)
        time.sleep(HEALTH_POLL_S)
    raise SystemExit(f"gateway never became ready within {HEALTH_TIMEOUT_S}s; last health: {last}")


def merchant_countersigner(merchant: Any, chain_id: int, verifying_contract: str):
    """The charging station operator's third signature.

    Inlined here for M1. The device and the merchant are physically co-located
    - the car is plugged into the station - so the countersignature is collected
    device-side and rides along with the voucher. `merchant/signer.py` is where
    this becomes a separate process.
    """

    def countersign(voucher: Voucher) -> str:
        signable = encode_typed_data(
            full_message=voucher.to_typed_data(
                chain_id=chain_id, verifying_contract=verifying_contract
            )
        )
        return HexBytes(Account.sign_message(signable, merchant.key).signature).to_0x_hex()

    return countersign


def report(
    result: dict[str, Any],
    signed: Any,
    rtt_ns: int | None,
    contract: Any,
    w3: Web3,
) -> None:
    """Print the marks, the decomposition, the gas, and the `Settled` event."""
    t0, t1 = result["t0"], result["t1"]
    t2, t3, t4, t5 = result["t2"], result["t3"], result["t4"], result["t5"]

    print("\n--- marks (CLAUDE.md, Instrumentation) ---------------------------")
    rows = [
        ("t0", "device ", "sensor event fires", t0),
        ("t1", "device ", "voucher signed", t1),
        ("t2", "gateway", "voucher received over transport", t2),
        ("t3", "gateway", "tx broadcast, hash returned", t3),
        ("t4", "gateway", "receipt received (in block)", t4),
        ("t5", "gateway", "confirmation depth reached", t5),
    ]
    for mark, where, what, value in rows:
        offset = f"+{(value - t0) * 1000:8.1f} ms" if value is not None and t0 else "         --"
        print(f"  {mark}  {where}  {fmt_ts(value)}  {offset}   {what}")

    print("\n--- decomposition ------------------------------------------------")
    print(f"  signing      t1-t0  {fmt_ms(t1, t0)}")
    print(f"  transport    t2-t1  {fmt_ms(t2, t1)}")
    print(f"  build+send   t3-t2  {fmt_ms(t3, t2)}")
    print(f"  inclusion    t4-t3  {fmt_ms(t4, t3)}")
    print(f"  confirm      t5-t4  {fmt_ms(t5, t4)}")
    print(f"  REPORTED     t4-t0  {fmt_ms(t4, t0)}   <- the H1 number")
    if rtt_ns is not None:
        # The skew sanity check. One machine here, so this is just a ceiling on
        # the whole gateway-side span; on the Pi it is what tells you t2 is sane.
        print(f"  device-side round trip  {rtt_ns / 1e6:8.1f} ms  (t_send -> t_ack)")

    print("\n--- cost ---------------------------------------------------------")
    gas_used = result.get("gasUsed")
    price = result.get("effectiveGasPrice")
    print(f"  gasUsed             {gas_used:,}" if gas_used else "  gasUsed             --")
    print(f"  effectiveGasPrice   {price:,} wei" if price else "  effectiveGasPrice   --")
    if gas_used and price:
        print(f"  execution fee       {w3.from_wei(gas_used * price, 'ether')} ETH")
    l1_fee = result.get("l1Fee")
    if l1_fee:
        print(f"  l1Fee               {l1_fee:,} wei")
    else:
        print("  l1Fee               n/a (anvil is not an OP-stack chain)")

    print("\n--- Settled event ------------------------------------------------")
    receipt = w3.eth.get_transaction_receipt(result["txHash"])
    events = contract.events.Settled().process_receipt(receipt, errors=DISCARD)
    if not events:
        print("  no Settled event in the receipt")
        return
    args = events[0]["args"]
    service_ref = HexBytes(args["serviceRef"]).to_0x_hex()
    print(f"  tx               {result['txHash']}")
    print(f"  block            {result['blockNumber']}")
    print(f"  channel          {args['channel']}")
    print(f"  serviceRef       {service_ref}")
    print(f"  nonce            {args['nonce']}")
    print(f"  cumulativeAmount {args['cumulativeAmount']:,} wei")
    print(f"  delta            {args['delta']:,} wei  (released to the merchant)")

    # The service fact the device hashed, checked against what the chain saw.
    expected = HexBytes(signed.voucher.serviceRef).to_0x_hex()
    verdict = "matches the voucher the device signed" if expected == service_ref else "MISMATCH"
    print(f"  serviceRef check {verdict}")


# --- main ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M1 end-to-end run against a local chain.")
    parser.add_argument("--deposit-eth", type=float, default=1.0, help="channel deposit")
    parser.add_argument("--power-kw", type=float, default=50.0, help="nominal charger power")
    parser.add_argument("--tick-seconds", type=float, default=2.0, help="metering interval")
    parser.add_argument("--seed", type=int, default=1, help="meter seed, for a repeatable run")
    args = parser.parse_args(argv)

    env = load_env()
    rpc_url, payment_channel = require(env, "RPC_URL", "PAYMENT_CHANNEL_ADDRESS")
    chain_id = int(env.get("CHAIN_ID", "31337"))
    device_key, gateway_key, merchant_key = require(
        env, "DEVICE_PRIVATE_KEY", "GATEWAY_PRIVATE_KEY", "MERCHANT_PRIVATE_KEY"
    )
    host = env.get("GATEWAY_HOST", "127.0.0.1")
    port = int(env.get("GATEWAY_PORT", "8000"))
    gateway_url = f"http://{host}:{port}"

    device = Account.from_key(device_key)
    gateway = Account.from_key(gateway_key)
    merchant = Account.from_key(merchant_key)

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
    payment_channel = w3.to_checksum_address(payment_channel)

    print("=== 1. preflight =================================================")
    preflight(w3, chain_id, payment_channel)
    for role, account in (("device  ", device), ("gateway ", gateway), ("merchant", merchant)):
        balance = w3.from_wei(w3.eth.get_balance(account.address), "ether")
        print(f"  {role} {account.address}  {balance} ETH")

    contract = w3.eth.contract(address=payment_channel, abi=load_abi())

    print("\n=== 2. open the channel ==========================================")
    deposit_wei = w3.to_wei(args.deposit_eth, "ether")
    channel = open_channel(w3, contract, device, gateway.address, merchant.address, deposit_wei)

    print("\n=== 3. start the gateway =========================================")
    proc = start_gateway(
        {
            "RPC_URL": rpc_url,
            "CHAIN_ID": str(chain_id),
            "PAYMENT_CHANNEL_ADDRESS": payment_channel,
            "GATEWAY_PRIVATE_KEY": gateway_key,
            "DEVICE_ADDRESS": device.address,
            "MERCHANT_ADDRESS": merchant.address,
            "GATEWAY_SUBMIT": "1",
            "CONFIRMATIONS": env.get("CONFIRMATIONS", "1"),
        },
        host,
        port,
    )

    try:
        health = await_health(gateway_url, proc)
        print(
            f"  health: status={health['status']} chainIdMatches={health['chainIdMatches']} "
            f"contractDeployed={health['contractDeployed']} submit={health['submit']}"
        )

        print("\n=== 4. one device tick ===========================================")
        transport = HttpTransport(
            gateway_url,
            countersign=merchant_countersigner(merchant, chain_id, payment_channel),
        )
        agent = DeviceAgent(
            account=device,
            config=AgentConfig(
                channel=channel,
                verifying_contract=payment_channel,
                chain_id=chain_id,
            ),
            transport=transport,
        )
        meter = KwhMeter(power_kw=args.power_kw, tick_seconds=args.tick_seconds, seed=args.seed)
        session = meter.start_session()
        print(f"  session {session}  device {agent.address}")

        # handle_event + send rather than run_tick(): the same two steps, but it
        # keeps the gateway's reply, which run_tick() discards.
        event = meter.tick()
        signed = agent.handle_event(event)
        print(
            f"  tick {event.sequence}: {event.cumulative_kwh:.6f} kWh cumulative, "
            f"nonce {signed.voucher.nonce}, {signed.voucher.cumulativeAmount:,} wei owed"
        )

        try:
            result = transport.send(signed)
        except GatewayError as exc:
            print(f"\n  GATEWAY REFUSED: {exc}")
            if exc.body:
                print(f"  body: {json.dumps(exc.body, indent=2)}")
            return 1

        if not result["submitted"]:
            print(f"\n  accepted but not submitted: {result['reason']}")
            return 1

        print("\n=== 5. results ===================================================")
        report(result, signed, transport.last_rtt_ns, contract, w3)

        on_chain = contract.functions.channels(channel).call()
        merchant_balance = w3.from_wei(w3.eth.get_balance(merchant.address), "ether")
        print(
            f"\n  channel state: paid {on_chain[4]:,} wei, nonce {on_chain[5]}, "
            f"deposit {w3.from_wei(on_chain[3], 'ether')} ETH"
        )
        print(f"  merchant balance now {merchant_balance} ETH")
        return 0
    finally:
        print("\n=== shutting the gateway down ====================================")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
