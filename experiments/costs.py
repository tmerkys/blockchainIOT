"""H1 cost table: measured gas units x mainnet prices on one stated date.

    python experiments/costs.py --date 2026-10-14

Why this exists. Testnet gas prices are not prices: Ethereum Sepolia ran at
~0.001 gwei during M5, Base Sepolia at ~0.006 gwei, so on testnets L1 looks
*cheaper* than L2. The L1/L2 cost gap H1 is about only exists at mainnet
prices. So the cost table is

    cost = gas units (per chain) x gas price (mainnet, one date) x ETH/EUR (same date)

with gas units primary (reproducible) and EUR secondary (CLAUDE.md).

Everything mainnet here is **read-only** — `eth_simulateV1`, `eth_feeHistory`,
`eth_call`. No key holds mainnet funds, nothing is broadcast. (CLAUDE.md's
"no mainnet, ever" is about transactions and real funds; reading public price
data was approved explicitly on 2026-10-14.)

**1. Gas units, by simulation.** Ethereum Sepolia has repriced state creation
and mainnet has not (fresh SSTORE 126k vs 43k, code deposit ~7x — see
docs/measurements.md), so Sepolia's measured 88k settle would overstate
mainnet L1. Instead the full lifecycle — deploy, open, settle (first), settle
x2 (subsequent), close — is run through `eth_simulateV1` on each chain, with
throwaway keys signing EIP-712 vouchers for *that* chain's id. The two
testnets are controls: the simulation must reproduce the gas measured there
on real transactions, or the mainnet figures are not trustworthy.

**2. Gas price.** `eth_feeHistory` over the 24 h before the run: per block,
base fee + median priority fee. Reported as median with p10–p90.

**3. Base's L1 data fee.** OP-stack chains charge L2 execution *plus* a fee for
posting the transaction to Ethereum, computed by the `GasPriceOracle`
predeploy from the signed transaction's bytes. We hand it a realistic signed
`settle()` (same calldata size as the real ones) and read `getL1Fee`, sampled
over the same 24 h where the node still has the state.

**4. ETH/EUR.** CoinGecko's daily price for the date.
"""

from __future__ import annotations

import argparse
import json
import secrets
import statistics
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eth_abi import encode as abi_encode  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402
from web3 import Web3  # noqa: E402

from shared.voucher import Voucher  # noqa: E402

ARTIFACT = REPO_ROOT / "contracts" / "out" / "PaymentChannel.sol" / "PaymentChannel.json"
OUT_DIR = REPO_ROOT / "experiments" / "analysis"

CHAINS = {
    # name: (rpc, chainId, mainnet?)
    "eth-mainnet": ("https://ethereum-rpc.publicnode.com", 1, True),
    "base-mainnet": ("https://mainnet.base.org", 8453, True),
    "eth-sepolia": ("https://ethereum-sepolia-rpc.publicnode.com", 11155111, False),
    "base-sepolia": ("https://base-sepolia-rpc.publicnode.com", 84532, False),
}
BLOCK_TIME_S = {"eth-mainnet": 12, "base-mainnet": 2}

# Gas measured on real testnet transactions (docs/measurements.md, M4/M5).
# The simulation must land on these for the method to count as validated.
#
# Each control is matched like for like, because the scenario moves gas a lot:
#   open_first    first channel on a fresh contract (_channelIndex 0 -> 1): M4 open
#   open          a later channel (index already non-zero): the M5 open on Base
#   close_only    close() as the channel's only settlement: M4's close
#   settle_first_newmerchant  merchant account empty, so the payment creates it:
#                 the M5 L1 first settle (the Sepolia merchant had never been paid)
MEASURED = {
    "base-sepolia": {"deploy": 2_037_870, "open_first": 167_067, "open": 149_967,
                     "settle_first": 111_477, "settle": 77_290, "close_only": 143_590},
    "eth-sepolia": {"deploy": 14_375_662, "open_first": 694_887,
                    "settle_first_newmerchant": 467_793, "settle": 88_365},
}

GAS_PRICE_ORACLE = "0x420000000000000000000000000000000000000F"
TICKS_PER_CHANNEL = 200  # M4
DEPOSIT = 10**13


def rpc(url: str, method: str, params: list) -> Any:
    r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=60)
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{method} on {url}: {body['error']}")
    return body["result"]


# --- 1. gas units --------------------------------------------------------------


def load_contract() -> tuple[list, str]:
    art = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    return art["abi"], art["bytecode"]["object"]


def create_address(sender: str, nonce: int) -> str:
    # RLP([sender, nonce]) for small nonces
    sender_b = bytes.fromhex(sender[2:])
    nonce_b = b"" if nonce == 0 else bytes([nonce]) if nonce < 0x80 else None
    assert nonce_b is not None
    item = (bytes([0x80]) if nonce == 0 else nonce_b)
    payload = bytes([0x80 + 20]) + sender_b + item
    return to_checksum_address(keccak(bytes([0xC0 + len(payload)]) + payload)[12:])


def sign(account, voucher: Voucher, chain_id: int, contract: str) -> bytes:
    msg = encode_typed_data(full_message=voucher.to_typed_data(chain_id=chain_id, verifying_contract=contract))
    return bytes(Account.sign_message(msg, account.key).signature)


def lifecycle_calls(chain_id: int) -> tuple[list[dict], dict[str, str], dict]:
    """Every scenario the cost table or a control needs, as eth_simulateV1 calls.

    One contract, three channels, one simulated block, state carried across:
      A: open_first, settle_first, settle x2, close (after settles)
      B: open, close_only                       (M4: 200 ticks off-chain, one close)
      C: open, settle_first_newmerchant         (merchant account never paid)
    A and B pay a merchant that already holds a balance, as on the real
    channels; C's merchant is empty, which is what the L1 control needs.
    """
    abi, bytecode = load_contract()
    w3 = Web3()
    gateway = Account.from_key(secrets.token_bytes(32))
    contract_addr = create_address(gateway.address, 0)
    c = w3.eth.contract(address=contract_addr, abi=abi)
    calls: list[dict] = [{"from": gateway.address, "data": bytecode, "label": "deploy"}]
    overrides = {gateway.address: {"balance": hex(10**20)}}

    def channel(index: int, open_label: str, steps: list[str], merchant_funded: bool) -> None:
        device, merchant = (Account.from_key(secrets.token_bytes(32)) for _ in range(2))
        if merchant_funded:
            overrides[merchant.address] = {"balance": hex(10**18)}
        ch = to_checksum_address(keccak(abi_encode(
            ["address"] * 3 + ["uint256"], [device.address, gateway.address, merchant.address, index]))[12:])
        calls.append({"from": gateway.address, "to": contract_addr, "value": hex(DEPOSIT), "label": open_label,
                      "data": c.encode_abi("open", args=[device.address, gateway.address, merchant.address])})
        for n, label in enumerate(steps, 1):
            v = Voucher(channel=ch, nonce=n, cumulativeAmount=n * 39_000_000_000,
                        serviceRef=secrets.token_bytes(32), deadline=2**40)
            sigs = [sign(a, v, chain_id, contract_addr) for a in (device, gateway, merchant)]
            args = [(v.channel, v.nonce, v.cumulativeAmount, v.serviceRef, v.deadline), *sigs]
            fn = "close" if label.startswith("close") else "settle"
            calls.append({"from": gateway.address, "to": contract_addr, "label": label,
                          "data": c.encode_abi(fn, args=args)})

    channel(0, "open_first", ["settle_first", "settle", "settle", "close_after_settles"], merchant_funded=True)
    channel(1, "open", ["close_only"], merchant_funded=True)
    channel(2, "open", ["settle_first_newmerchant"], merchant_funded=False)
    return calls, overrides, {"gateway": gateway}


def simulate_gas(name: str) -> dict[str, int]:
    url, chain_id, _ = CHAINS[name]
    calls, overrides, _ = lifecycle_calls(chain_id)
    payload = [{"blockStateCalls": [{"stateOverrides": overrides,
                                     "calls": [{k: v for k, v in c.items() if k != "label"} for c in calls]}],
                "validation": False}, "latest"]
    result = rpc(url, "eth_simulateV1", payload)[0]["calls"]
    gas: dict[str, list[int]] = {}
    for c, r in zip(calls, result):
        if int(r["status"], 16) != 1:
            raise RuntimeError(f"{name}: {c['label']} reverted in simulation: {r.get('error') or r.get('returnData')}")
        gas.setdefault(c["label"], []).append(int(r["gasUsed"], 16))
    # repeated labels (two subsequent settles, two later opens) agree to within
    # calldata-byte noise; report their mean
    return {k: round(statistics.mean(v)) for k, v in gas.items()}


# --- 2. gas price --------------------------------------------------------------


def fee_history(name: str, hours: float = 24) -> dict[str, float]:
    """Per-block (baseFee + median tip) over the last `hours`, in wei."""
    url = CHAINS[name][0]
    head = int(rpc(url, "eth_blockNumber", []), 16)
    span = int(hours * 3600 / BLOCK_TIME_S[name])
    window = 1024
    # Ethereum: cover every block. Base (43k blocks/day): 24 evenly spaced windows.
    starts = range(head - span, head, window) if span <= 12 * window else [
        head - span + i * (span // 24) for i in range(24)
    ]
    prices: list[int] = []
    for start in starts:
        newest = min(start + window - 1, head)
        h = rpc(url, "eth_feeHistory", [hex(newest - start + 1), hex(newest), [50]])
        base = [int(x, 16) for x in h["baseFeePerGas"][:-1]]
        tips = [int(r[0], 16) for r in h["reward"]]
        prices += [b + t for b, t in zip(base, tips)]
    prices.sort()
    q = lambda p: prices[min(len(prices) - 1, int(p * len(prices)))]  # noqa: E731
    return {"blocks": len(prices), "median": statistics.median(prices), "p10": q(0.10), "p90": q(0.90),
            "head": head}


# --- 3. Base L1 data fee -------------------------------------------------------


def base_l1_fees(hours: float = 24) -> dict[str, dict[str, float]]:
    """`GasPriceOracle.getL1Fee(signedTx)` on Base mainnet, per operation, sampled over the day.

    Every Base transaction pays it, sized by its own bytes — the 10 kB deploy
    far more than a ~600 B settle — so each operation gets its own figure.
    """
    url, chain_id, _ = CHAINS["base-mainnet"]
    calls, _, keys = lifecycle_calls(chain_id)
    selector = keccak(text="getL1Fee(bytes)")[:4]
    head = int(rpc(url, "eth_blockNumber", []), 16)
    step = int(hours * 3600 / BLOCK_TIME_S["base-mainnet"]) // 24
    out: dict[str, dict[str, float]] = {}
    for call in calls:
        op = call["label"]
        if op in out:
            continue
        tx = {
            "data": call["data"], "value": int(call.get("value", "0x0"), 16), "nonce": 1234,
            "gas": 3_000_000, "maxFeePerGas": 10**8, "maxPriorityFeePerGas": 10**6,
            "chainId": chain_id, "type": 2,
        }
        if "to" in call:
            tx["to"] = call["to"]
        raw = bytes(Account.sign_transaction(tx, keys["gateway"].key).raw_transaction)
        data = "0x" + (selector + abi_encode(["bytes"], [raw])).hex()
        fees = []
        for i in range(24, -1, -1):
            try:
                fees.append(int(rpc(url, "eth_call", [{"to": GAS_PRICE_ORACLE, "data": data}, hex(head - i * step)]), 16))
            except RuntimeError:
                continue  # pruned state; keep what the node still serves
        if not fees:
            raise RuntimeError(f"GasPriceOracle unreachable at every sampled block ({op})")
        fees.sort()
        out[op] = {"samples": len(fees), "median": statistics.median(fees), "p10": fees[int(0.1 * len(fees))],
                   "p90": fees[min(len(fees) - 1, int(0.9 * len(fees)))], "tx_bytes": len(raw)}
    return out


# --- 4. ETH/EUR ------------------------------------------------------------------


def eth_eur(day: date) -> float:
    url = f"https://api.coingecko.com/api/v3/coins/ethereum/history?date={day:%d-%m-%Y}&localization=false"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "magis-thesis"}), timeout=30) as r:
        return float(json.load(r)["market_data"]["current_price"]["eur"])


# --- main ------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="H1 cost table at mainnet prices.")
    parser.add_argument("--date", default=str(date.today()), help="reference date (prices are taken now)")
    args = parser.parse_args(argv)
    ref = date.fromisoformat(args.date)
    taken = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print("== 1. gas units (eth_simulateV1, full lifecycle)")
    gas = {}
    for name in CHAINS:
        gas[name] = simulate_gas(name)
        print(f"  {name:13} " + "  ".join(f"{k} {v:,}" for k, v in gas[name].items()))
    print("  controls (simulated vs measured on real txs):")
    ok = True
    for name, meas in MEASURED.items():
        for k, v in meas.items():
            d = gas[name][k] - v
            flag = "ok" if abs(d) <= max(150, 0.002 * v) else "MISMATCH"
            ok &= flag == "ok"
            print(f"    {name:13} {k:13} sim {gas[name][k]:>11,}  real {v:>11,}  diff {d:+7,}  {flag}")
    if not ok:
        print("  WARNING: simulation does not reproduce measured gas; mainnet figures are suspect")

    print("\n== 2. mainnet gas price, last 24 h (base fee + median tip)")
    price = {n: fee_history(n) for n in ("eth-mainnet", "base-mainnet")}
    for n, p in price.items():
        print(f"  {n:13} median {p['median']/1e9:.4f} gwei  p10 {p['p10']/1e9:.4f}  p90 {p['p90']/1e9:.4f}"
              f"  ({p['blocks']:,} blocks to #{p['head']})")

    print("\n== 3. Base L1 data fee per operation (GasPriceOracle, signed tx bytes)")
    l1 = base_l1_fees()
    for op, f in l1.items():
        print(f"  {op:13} median {f['median']:>16,.0f} wei  p10 {f['p10']:>16,.0f}  p90 {f['p90']:>16,.0f}"
              f"  ({f['samples']} samples, {f['tx_bytes']} B)")

    print("\n== 4. ETH/EUR")
    eur = eth_eur(ref)
    print(f"  CoinGecko {ref}: {eur:,.2f} EUR/ETH")

    # Operation -> (gas units, L1-fee share) as sums over lifecycle ops, so the
    # M4 amortisation rows reuse the same inputs: a per-tick figure is just a
    # weighted sum of real transactions divided by the ticks it paid for.
    ops = {
        "deploy": {"deploy": 1},
        "open (later channel)": {"open": 1},
        "settle (first on channel)": {"settle_first": 1},
        "settle": {"settle": 1},
        "close (only settlement)": {"close_only": 1},
        # H1/M4: what one micropayment costs if every tick settles on chain...
        "per tick: settle every tick": {"settle": 1},
        # ...vs inside a channel (M4): 200 ticks off-chain, then one close().
        # Marginal share of the close, and the full open+close lifecycle.
        f"per tick: channel, close/{TICKS_PER_CHANNEL}": {"close_only": 1 / TICKS_PER_CHANNEL},
        f"per tick: channel, (open+close)/{TICKS_PER_CHANNEL}": {"open": 1 / TICKS_PER_CHANNEL,
                                                                 "close_only": 1 / TICKS_PER_CHANNEL},
    }
    rows = []
    for chain in ("eth-mainnet", "base-mainnet"):
        for op, weights in ops.items():
            gas_units = sum(gas[chain][k] * w for k, w in weights.items())
            for stat in ("median", "p10", "p90"):
                l1f = sum(l1[k][stat] * w for k, w in weights.items()) if chain == "base-mainnet" else 0.0
                wei = gas_units * price[chain][stat] + l1f
                rows.append({"chain": chain, "operation": op, "gas": round(gas_units, 1), "price_stat": stat,
                             "gas_price_wei": price[chain][stat], "l1_fee_wei": l1f, "cost_wei": wei,
                             "cost_eur": wei / 1e18 * eur})

    print(f"\n== cost per operation, mainnet prices, {ref} (median [p10–p90])")
    import pandas as pd  # local: only needed for the table

    df = pd.DataFrame(rows)
    for (chain, op), g in df.groupby(["chain", "operation"], sort=False):
        m, lo, hi = (g[g.price_stat == s].iloc[0] for s in ("median", "p10", "p90"))
        print(f"  {chain:13} {op:40} {m.gas:>13,.1f} gas   {m.cost_eur:>12.6f} EUR"
              f"   [{lo.cost_eur:.6f} – {hi.cost_eur:.6f}]")

    meta = {"reference_date": str(ref), "prices_taken_utc": taken, "eth_eur": eur,
            "gas_price": price, "base_l1_fee": l1, "gas_units": gas,
            "rpc": {k: v[0] for k, v in CHAINS.items()}, "simulation_controls_ok": ok}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_DIR / f"costs_{ref}.csv", index=False)
    (OUT_DIR / f"costs_{ref}.json").write_text(json.dumps(meta, indent=2, default=float), encoding="utf-8")
    print(f"\nwrote {OUT_DIR / f'costs_{ref}.csv'} and .json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
