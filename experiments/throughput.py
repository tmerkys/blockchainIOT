"""H2 throughput: what the 3-of-3 costs, on chain and off.

    python experiments/throughput.py                 # all parts
    python experiments/throughput.py --part onchain  # or gateway / burst

H2 says the 3-of-3 channel stops the gateway moving funds on its own *without
destroying throughput*. "Destroying" needs a reference, so every figure here
compares the real contract against `contracts/test/baseline/SingleSigChannel.sol`:
the same contract with the device's signature as the only check, i.e. a
classic unidirectional channel. The difference between the two is the price
of the two extra signatures and nothing else.

**1. onchain — the price per settlement, and what fits in a block.** Both
variants run the same lifecycle (deploy, open, first settle, three later
settles) through `eth_simulateV1`, exactly as `costs.py` does: read-only, no
key holds funds, nothing is broadcast. The testnets are controls — the 3-of-3
simulation must reproduce the gas measured on real `settle()` transactions.
Then, per variant: calldata bytes, Base's L1 data fee for the signed
transaction (`GasPriceOracle.getL1Fee`, paired at the same blocks the cost
table sampled, so both variants see identical L1 prices), EUR at the cost
table's 2026-10-14 prices, and settlements per block = block gas limit / gas.
That last number is an upper bound — a whole block given to one contract —
and is reported as a ratio between variants, which is what H2 asks.

**2. gateway — the off-chain work per voucher.** The gateway's per-voucher
steps, timed in-process on the laptop with `perf_counter_ns`, N vouchers each:
verify the device signature, verify the merchant's, co-sign, encode. The
3-of-3 path is all four; the baseline path is device verification and a
one-signature encoding. Their inverse is the voucher rate one core can
process, the ceiling the gateway's cryptography puts on throughput.

**3. burst — the live pipeline, back to back.** `runner.py --experiment tp
--gap-min 0 --gap-max 0` drives the real Pi -> gateway -> Base Sepolia path
with no gap between runs. Read here: sustained settled payments per second,
and whether back-to-back load changes the per-payment latency against the
spaced M5 runs of the same condition. The prototype gateway settles one
voucher at a time (it waits for each receipt before taking the next), so this
measures the prototype, not the contract; the contract's ceiling is part 1.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import secrets
import statistics
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (REPO_ROOT, REPO_ROOT / "experiments", REPO_ROOT / "gateway"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402
from eth_abi import encode as abi_encode  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402
from scipy import stats  # noqa: E402
from web3 import Web3  # noqa: E402

import costs  # noqa: E402  (experiments/costs.py: rpc, chains, controls)
from relayer import Relayer, RelayerConfig  # noqa: E402
from shared.voucher import Voucher  # noqa: E402

ARTIFACTS = {
    "3of3": REPO_ROOT / "contracts" / "out" / "PaymentChannel.sol" / "PaymentChannel.json",
    "1of1": REPO_ROOT / "contracts" / "out" / "SingleSigChannel.sol" / "SingleSigChannel.json",
}
RESULTS_DIR = REPO_ROOT / "experiments" / "results"
OUT_DIR = REPO_ROOT / "experiments" / "analysis"
COST_REF = OUT_DIR / "costs_2026-10-14.json"
LATER_SETTLES = 3


def load(variant: str) -> tuple[list, str]:
    art = json.loads(ARTIFACTS[variant].read_text(encoding="utf-8"))
    return art["abi"], art["bytecode"]["object"]


def ci95(xs: list[float]) -> float:
    n = len(xs)
    return stats.t.ppf(0.975, n - 1) * statistics.stdev(xs) / math.sqrt(n) if n > 1 else math.nan


# --- 1. on chain -----------------------------------------------------------------


def lifecycle(variant: str, chain_id: int) -> tuple[list[dict], dict, Any]:
    """deploy, open, settle_first, settle x LATER_SETTLES — one variant, fresh keys."""
    abi, bytecode = load(variant)
    gateway, device, merchant = (Account.from_key(secrets.token_bytes(32)) for _ in range(3))
    addr = costs.create_address(gateway.address, 0)
    c = Web3().eth.contract(address=addr, abi=abi)
    # Merchant already holds a balance, as on the real channels (see costs.py).
    overrides = {gateway.address: {"balance": hex(10**20)}, merchant.address: {"balance": hex(10**18)}}
    ch = to_checksum_address(keccak(abi_encode(
        ["address"] * 3 + ["uint256"], [device.address, gateway.address, merchant.address, 0]))[12:])
    calls = [
        {"from": gateway.address, "data": bytecode, "label": "deploy"},
        {"from": gateway.address, "to": addr, "value": hex(costs.DEPOSIT), "label": "open",
         "data": c.encode_abi("open", args=[device.address, gateway.address, merchant.address])},
    ]
    for n in range(1, LATER_SETTLES + 2):
        v = Voucher(channel=ch, nonce=n, cumulativeAmount=n * 39_000_000_000,
                    serviceRef=secrets.token_bytes(32), deadline=2**40)
        signers = (device, gateway, merchant) if variant == "3of3" else (device,)
        sigs = [costs.sign(a, v, chain_id, addr) for a in signers]
        calls.append({"from": gateway.address, "to": addr, "label": "settle_first" if n == 1 else "settle",
                      "data": c.encode_abi("settle", args=[(v.channel, v.nonce, v.cumulativeAmount,
                                                            v.serviceRef, v.deadline), *sigs])})
    return calls, overrides, gateway


def simulate(name: str, variant: str) -> dict[str, Any]:
    url, chain_id, _ = costs.CHAINS[name]
    calls, overrides, gateway = lifecycle(variant, chain_id)
    payload = [{"blockStateCalls": [{"stateOverrides": overrides,
                                     "calls": [{k: v for k, v in c.items() if k != "label"} for c in calls]}],
                "validation": False}, "latest"]
    result = costs.rpc(url, "eth_simulateV1", payload)[0]["calls"]
    gas: dict[str, list[int]] = {}
    for c, r in zip(calls, result):
        if int(r["status"], 16) != 1:
            raise RuntimeError(f"{name}/{variant}: {c['label']} reverted: {r.get('error') or r.get('returnData')}")
        gas.setdefault(c["label"], []).append(int(r["gasUsed"], 16))
    settle_call = next(c for c in reversed(calls) if c["label"] == "settle")
    return {"gas": {k: round(statistics.mean(v)) for k, v in gas.items()},
            "calldata_bytes": (len(settle_call["data"]) - 2) // 2,
            "settle_call": settle_call, "gateway": gateway}


def signed_settle(sim: dict[str, Any], chain_id: int) -> bytes:
    call = sim["settle_call"]
    tx = {"to": call["to"], "data": call["data"], "value": 0, "nonce": 1234, "gas": 300_000,
          "maxFeePerGas": 10**8, "maxPriorityFeePerGas": 10**6, "chainId": chain_id, "type": 2}
    return bytes(Account.sign_transaction(tx, sim["gateway"].key).raw_transaction)


def paired_l1_fees(raws: dict[str, bytes], ref: dict) -> dict[str, Any]:
    """getL1Fee for each variant's signed settle, at the same Base mainnet blocks.

    The blocks are the cost table's own (24 h before its head, 25 samples), so
    the 3-of-3 row reproduces the cost table's settle fee; if the node has
    pruned that state, fall back to the same spacing before today's head.
    """
    url = costs.CHAINS["base-mainnet"][0]
    selector = keccak(text="getL1Fee(bytes)")[:4]
    step = int(24 * 3600 / costs.BLOCK_TIME_S["base-mainnet"]) // 24
    for head, label in ((ref["gas_price"]["base-mainnet"]["head"], "cost-table blocks (2026-10-14)"),
                        (int(costs.rpc(url, "eth_blockNumber", []), 16), "last 24 h")):
        fees: dict[str, list[int]] = {v: [] for v in raws}
        for i in range(24, -1, -1):
            block = hex(head - i * step)
            try:
                row = {v: int(costs.rpc(url, "eth_call", [{"to": costs.GAS_PRICE_ORACLE, "data": "0x" + (
                    selector + abi_encode(["bytes"], [raw])).hex()}, block]), 16) for v, raw in raws.items()}
            except RuntimeError:
                continue  # pruned; both variants skip the block together, keeping the pairing
            for v, f in row.items():
                fees[v].append(f)
        if len(fees["3of3"]) >= 10:
            ratio = [a / b for a, b in zip(fees["3of3"], fees["1of1"])]
            return {"blocks": label, "samples": len(ratio),
                    **{v: statistics.median(f) for v, f in fees.items()},
                    "ratio_median": statistics.median(ratio), "tx_bytes": {v: len(r) for v, r in raws.items()}}
    raise RuntimeError("GasPriceOracle unreachable at enough blocks")


def onchain() -> dict[str, Any]:
    ref = json.loads(COST_REF.read_text(encoding="utf-8"))
    print("== 1. on chain: 3-of-3 vs single-signature baseline (eth_simulateV1, read-only)")
    sims = {(n, v): simulate(n, v) for n in costs.CHAINS for v in ARTIFACTS}
    for n in costs.CHAINS:
        for v in ARTIFACTS:
            s = sims[(n, v)]
            print(f"  {n:13} {v}  " + "  ".join(f"{k} {g:,}" for k, g in s["gas"].items())
                  + f"   settle calldata {s['calldata_bytes']} B")

    controls_ok = True
    print("  controls (3-of-3 simulated vs measured on real txs):")
    for n, meas in (("base-sepolia", costs.MEASURED["base-sepolia"]["settle"]),
                    ("eth-sepolia", costs.MEASURED["eth-sepolia"]["settle"])):
        sim = sims[(n, "3of3")]["gas"]["settle"]
        ok = abs(sim - meas) <= max(150, 0.002 * meas)
        controls_ok &= ok
        print(f"    {n:13} settle  sim {sim:,}  real {meas:,}  diff {sim - meas:+,}  {'ok' if ok else 'MISMATCH'}")

    raws = {v: signed_settle(sims[("base-mainnet", v)], 8453) for v in ARTIFACTS}
    l1 = paired_l1_fees(raws, ref)
    print(f"  Base L1 data fee per settle ({l1['blocks']}, {l1['samples']} paired samples): "
          f"3of3 {l1['3of3']:,.0f} wei, 1of1 {l1['1of1']:,.0f} wei, ratio {l1['ratio_median']:.3f}")

    eur = ref["eth_eur"]
    limits = {}
    for n, bt in (("eth-mainnet", 12), ("base-mainnet", 2)):
        blk = costs.rpc(costs.CHAINS[n][0], "eth_getBlockByNumber", ["latest", False])
        limits[n] = {"gas_limit": int(blk["gasLimit"], 16), "block": int(blk["number"], 16), "block_time_s": bt}

    rows = []
    for n in ("eth-mainnet", "base-mainnet"):
        price = ref["gas_price"][n]["median"]
        for v in ARTIFACTS:
            g = sims[(n, v)]["gas"]["settle"]
            l1fee = l1[v] if n == "base-mainnet" else 0
            wei = g * price + l1fee
            per_block = limits[n]["gas_limit"] // g
            rows.append({"chain": n, "variant": v, "settle_gas": g,
                         "settle_first_gas": sims[(n, v)]["gas"]["settle_first"],
                         "calldata_bytes": sims[(n, v)]["calldata_bytes"],
                         "l1_fee_wei": l1fee, "gas_price_wei": price, "cost_wei": wei, "cost_eur": wei / 1e18 * eur,
                         "block_gas_limit": limits[n]["gas_limit"], "settles_per_block": per_block,
                         "settles_per_s": per_block / limits[n]["block_time_s"]})
    df = pd.DataFrame(rows)
    print("\n  per later settle, mainnet prices of 2026-10-14 (median):")
    for n, g in df.groupby("chain", sort=False):
        a, b = g[g.variant == "3of3"].iloc[0], g[g.variant == "1of1"].iloc[0]
        print(f"  {n:13} gas {b.settle_gas:,} -> {a.settle_gas:,} (+{a.settle_gas - b.settle_gas:,}, "
              f"+{100 * (a.settle_gas / b.settle_gas - 1):.1f}%)   EUR {b.cost_eur:.6f} -> {a.cost_eur:.6f} "
              f"(+{100 * (a.cost_eur / b.cost_eur - 1):.1f}%)   settles/block {b.settles_per_block:,} -> "
              f"{a.settles_per_block:,} ({100 * (a.settles_per_block / b.settles_per_block - 1):+.1f}%), "
              f"gas limit {a.block_gas_limit:,}")
    return {"rows": df, "l1": l1, "limits": limits, "controls_ok": controls_ok,
            "gas": {f"{n}/{v}": s["gas"] for (n, v), s in sims.items()}}


# --- 2. gateway --------------------------------------------------------------------


def gateway(n: int) -> pd.DataFrame:
    """Per-voucher gateway work, 3-of-3 path vs baseline path, one core."""
    print(f"\n== 2. gateway: per-voucher work on this machine, N = {n}")
    device, merchant, gw = (Account.from_key(secrets.token_bytes(32)) for _ in range(3))
    contract = "0xb746530c7e38aB163dE7238f70FE1e91c5E14bF9"  # Base Sepolia M5 contract; encoding only
    cfg = RelayerConfig(chain_id=84532, payment_channel=contract, gateway_private_key=gw.key.hex(),
                        device_address=device.address, merchant_address=merchant.address, submit=False)
    relayer = Relayer(cfg, w3=Web3())
    single = Web3().eth.contract(address=contract, abi=load("1of1")[0])
    channel = to_checksum_address("0x" + secrets.token_hex(20))

    def voucher(i: int) -> tuple[Voucher, bytes, bytes]:
        v = Voucher(channel=channel, nonce=i + 1, cumulativeAmount=(i + 1) * 39_000_000_000,
                    serviceRef=secrets.token_bytes(32), deadline=2**40)
        return v, costs.sign(device, v, 84532, contract), costs.sign(merchant, v, 84532, contract)

    work = [voucher(i) for i in range(n + 50)]  # signing is the device's and merchant's work: not timed
    rows = []
    for i, (v, sd, sm) in enumerate(work):
        t = time.perf_counter_ns
        a = t(); relayer.verify_device(v, sd)
        b = t(); relayer.verify_merchant(v, sm)
        c = t(); sg = relayer.sign(v)
        d = t(); relayer.build_settle_tx(v, sd, sg, sm)
        e = t(); single.encode_abi("settle", args=[(v.channel, v.nonce, v.cumulativeAmount, v.serviceRef,
                                                     v.deadline), sd])
        f = t()
        if i >= 50:  # warm-up excluded
            rows.append({"verify_device_ms": (b - a) / 1e6, "verify_merchant_ms": (c - b) / 1e6,
                         "cosign_ms": (d - c) / 1e6, "encode_3of3_ms": (e - d) / 1e6,
                         "encode_1of1_ms": (f - e) / 1e6})
    df = pd.DataFrame(rows)
    df["path_3of3_ms"] = df[["verify_device_ms", "verify_merchant_ms", "cosign_ms", "encode_3of3_ms"]].sum(axis=1)
    df["path_1of1_ms"] = df[["verify_device_ms", "encode_1of1_ms"]].sum(axis=1)
    for col in df:
        x = df[col].tolist()
        print(f"  {col:20} {statistics.mean(x):8.3f} ± {ci95(x):.3f} ms   median {statistics.median(x):.3f}")
    for p in ("3of3", "1of1"):
        print(f"  vouchers/s, one core, {p}: {1000 / df[f'path_{p}_ms'].mean():,.0f}")
    return df


# --- 3. burst ----------------------------------------------------------------------


def burst() -> dict[str, Any] | None:
    files = sorted(glob.glob(str(RESULTS_DIR / "tp_*.csv")))
    print("\n== 3. burst: live pipeline, back to back")
    if not files:
        print("  no tp_*.csv yet — run: python experiments/runner.py --experiment tp "
              "--transport wifi --n 30 --gap-min 0 --gap-max 0")
        return None
    out = {}
    for f in files:
        d = pd.read_csv(f)
        ok = d[d.status == "ok"].sort_values("run")
        chain, transport = ok.chain.iloc[0], ok.transport.iloc[0]
        span = (ok.t0_wall.iloc[-1] + ok.latency_ms.iloc[-1] / 1000) - ok.t0_wall.iloc[0]
        rate = len(ok) / span
        per_block = ok.blockNumber.value_counts()
        spaced = pd.concat(pd.read_csv(m) for m in glob.glob(str(RESULTS_DIR / f"m5_{chain}_{transport}_*.csv")))
        spaced = spaced[(spaced.status == "ok") & (spaced.cpuGovernor == "performance")
                        & (spaced.wifiPowersave == "disable")]
        w = stats.ttest_ind(ok.latency_ms, spaced.latency_ms, equal_var=False)
        res = {"file": Path(f).name, "ok": len(ok), "attempted": len(d), "span_s": span,
               "settled_per_s": rate, "mean_interval_s": span / len(ok),
               "latency_mean": ok.latency_ms.mean(), "latency_ci95": ci95(ok.latency_ms.tolist()),
               "spaced_latency_mean": spaced.latency_ms.mean(), "spaced_n": len(spaced), "welch_p": w.pvalue,
               "blocks": int(per_block.size), "max_settles_in_one_block": int(per_block.max()),
               "block_span": int(ok.blockNumber.max() - ok.blockNumber.min() + 1)}
        out[Path(f).name] = res
        print(f"  {res['file']}: {res['ok']}/{res['attempted']} ok in {span:.1f} s -> "
              f"{rate:.2f} settled payments/s (one every {res['mean_interval_s']:.2f} s)")
        print(f"    latency {res['latency_mean']:.1f} ± {res['latency_ci95']:.1f} ms back to back vs "
              f"{res['spaced_latency_mean']:.1f} ms spaced (n={res['spaced_n']}), Welch p = {w.pvalue:.2g}")
        print(f"    {res['ok']} settles in {res['blocks']} distinct blocks over {res['block_span']} blocks; "
              f"max {res['max_settles_in_one_block']} in one block")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="H2 throughput: 3-of-3 vs single-signature.")
    parser.add_argument("--part", choices=["all", "onchain", "gateway", "burst"], default="all")
    parser.add_argument("--n", type=int, default=1000, help="vouchers for the gateway benchmark")
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_json = OUT_DIR / "throughput.json"
    meta: dict[str, Any] = json.loads(out_json.read_text(encoding="utf-8")) if out_json.exists() else {}
    if args.part in ("all", "onchain"):
        r = onchain()
        r["rows"].to_csv(OUT_DIR / "throughput_onchain.csv", index=False)
        meta["onchain"] = {"taken_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "prices": "costs_2026-10-14.json (median gas price, ETH/EUR)",
                           "l1_fee": r["l1"], "block_limits": r["limits"], "gas": r["gas"],
                           "controls_ok": r["controls_ok"]}
    if args.part in ("all", "gateway"):
        df = gateway(args.n)
        summ = df.agg(["mean", "median", "std"]).T
        summ["ci95"] = [ci95(df[c].tolist()) for c in summ.index]
        summ["n"] = len(df)
        summ.to_csv(OUT_DIR / "throughput_gateway.csv", index_label="step", float_format="%.4f")
        meta["gateway"] = {"n": len(df), "machine": "gateway laptop (Windows), one core, perf_counter_ns",
                           "vouchers_per_s": {p: 1000 / df[f"path_{p}_ms"].mean() for p in ("3of3", "1of1")}}
    if args.part in ("all", "burst"):
        b = burst()
        if b is not None:
            meta["burst"] = b
    out_json.write_text(json.dumps(meta, indent=2, default=float), encoding="utf-8")
    print(f"\nwrote {OUT_DIR}/throughput*.csv and throughput.json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
