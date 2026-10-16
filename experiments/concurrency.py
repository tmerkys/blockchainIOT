"""Concurrency experiment: K devices against a pipelining gateway (H2, §5.2).

The H2 throughput run (`tp_`, 2026-10-15) found the prototype settles exactly
one payment per Base block, because the gateway is sequential. This measures
what a pipelining gateway does with K concurrent devices, K = 1, 2, 4, 8:
settled payments/s, payments per L2 block, per-payment latency (to
pre-confirmation `t4` and to the sealed block `t5`), and failures by status.

Topology (live session): K device processes **on the Pi** (each its own key,
its own channel, signing on the real device hardware; they share the Pi's
4 cores, which is part of what is measured), one gateway on the laptop in
pipelining mode with a **dedicated load-test key** whose nonces never touch
the M5 gateway's. The K channels each name that load-test key as `gateway`
and the existing merchant as `merchant`, so every settlement is still 3-of-3.
Opening the channels is setup, not measurement.

Files: one CSV per level per session, `experiments/results/cc_<chain>_k<K>_
<session>.csv` — prefix `cc_`, so `analyze.py` (m5_) and `throughput.py` (tp_)
never pool it. The analysis here writes `experiments/analysis/concurrency.csv`.

Subcommands (all take `--env-file`, default `.env.load`, gitignored):

  gateway-key  generate the load-test gateway key into the env file
               (LOAD_GATEWAY_PRIVATE_KEY / LOAD_GATEWAY_ADDRESS; address printed)
  estimate     cost of a plan, before anything is sent (read-only RPC)
  serve        run the gateway with the load key, GATEWAY_PIPELINE=1
               (--no-pipeline: the old sequential gateway, as the control)
  setup        open K channels for K device addresses (from the Pi, or
               --local DIR for a smoke test), write per-device overlays
  run          start K device processes at once, stream rows into the CSV
  analyze      per-level statistics over cc_*.csv only

`.env.load` holds (KEY=value, like `.env`):
  RPC_URL, CHAIN_ID, PAYMENT_CHANNEL_ADDRESS   the deployed PaymentChannel
  MERCHANT_ADDRESS                             the existing merchant (payee)
  LOAD_GATEWAY_PRIVATE_KEY, LOAD_GATEWAY_ADDRESS   from `gateway-key`
  PRICE_WEI_PER_KWH                            1400000000000 (M4/M5 scaled price)
  CONFIRMATIONS=1, GATEWAY_RECEIPT_TIMEOUT_S=120 (optional)
  LOAD_DEVICE_<i>, LOAD_CHANNEL_<i>            written by `setup`
It must NOT contain GATEWAY_PRIVATE_KEY: `serve` maps the LOAD_ key onto it,
and refuses to start if the load key equals the M5 gateway's.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

RESULTS_DIR = REPO_ROOT / "experiments" / "results"
OUT_DIR = REPO_ROOT / "experiments" / "analysis"
DEFAULT_ENV = REPO_ROOT / ".env.load"
PI_REPO = "~/magis"

LEVELS = (1, 2, 4, 8)
SCALED_PRICE_WEI_PER_KWH = 1_400_000_000_000
"""M4/M5's scaled price (docs/measurements.md, M4). Gas does not depend on it."""
MAX_TICK_MWH = 50 * 2 / 3600 * 1.05 * 1_000_000
"""Upper bound of one tick: 50 kW x 2 s, +5% meter jitter = 29.17 Wh = 29,167 mWh
(device/sensor.py defaults; mWh, as `agent.price()` takes them)."""

# Gas units measured on Base Sepolia (docs/measurements.md: M3, M4, M5).
GAS_OPEN = 167_067  # M4 (M5's open was 149,967; the larger is used for budgeting)
GAS_FIRST_SETTLE = 111_477  # first settle on a channel (fresh storage)
GAS_SETTLE = 77_290  # later settles (M5 median)
GAS_CLOSE = 143_590  # M4
FEE_PER_SETTLE_WEI = 463_738_800_640  # summary.csv fee_wei, L2 eth controlled (incl. l1Fee)

RECORD_COLUMNS = [
    "run", "status", "error", "iface",
    "t0_wall", "t1_wall", "signing_ms", "rtt_ms", "latency_ms", "network_ms", "clock_offset_ms",
    "t2", "t3", "t4", "t5", "t6",
    "nonce", "cumulativeAmount", "serviceRef", "txHash", "blockNumber",
    "gasUsed", "effectiveGasPrice", "l1Fee", "l1GasUsed", "l1GasPrice", "gatewayReason",
    "cpu_sign_ms", "cpu_countersign_ms", "cpu_device_ms",
    # exp/rpc additions to run_batch.build_record (runner.RPC_RECORD_COLUMNS)
    "rpcHost", "blockTimestamp", "headAtReceipt",
    # device/run_load.py additions
    "device", "deviceAddress", "channel", "closed",
    "failure", "txNonce", "nonceEpoch", "inflight", "gapBehind", "nonceResyncs",
]
SESSION_COLUMNS = [
    "session", "experiment", "level", "nPerDevice", "transport", "deviceHost",
    "chain", "chainId", "contract", "gateway", "pipeline",
    "commit", "piCommit", "seed", "startedUtc", "wifiPowersave", "cpuGovernor",
]


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --- env file ----------------------------------------------------------------


def read_env(path: Path) -> dict[str, str]:
    """`KEY=value` lines into a dict. Unlike `load_env`, leaves os.environ alone."""
    if not path.exists():
        raise SystemExit(f"no {path}")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def update_env(path: Path, updates: dict[str, str]) -> None:
    from scripts.m2_setup import update_env as _update

    if not path.exists():
        path.write_text("# concurrency experiment (experiments/concurrency.py); gitignored\n",
                        encoding="utf-8")
    _update(path, {k: v for k, v in updates.items()})


def chain_name(chain_id: int) -> str:
    return {31337: "anvil", 84532: "base-sepolia", 11155111: "sepolia"}.get(chain_id, str(chain_id))


# --- gateway-key ---------------------------------------------------------------


def cmd_gateway_key(args: argparse.Namespace) -> int:
    from eth_account import Account

    env = read_env(args.env_file) if args.env_file.exists() else {}
    if env.get("LOAD_GATEWAY_PRIVATE_KEY") and not args.force:
        raise SystemExit(f"{args.env_file} already has a load gateway key (--force to replace)")
    acct = Account.create(extra_entropy=os.urandom(32).hex())
    # Not echoed through update_env's printout: write directly.
    if not args.env_file.exists():
        args.env_file.write_text("# concurrency experiment; gitignored\n", encoding="utf-8")
    from scripts.m2_setup import update_env as _update
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):  # m2_setup echoes values; keep the key out
        _update(args.env_file, {"LOAD_GATEWAY_PRIVATE_KEY": acct.key.to_0x_hex(),
                                "LOAD_GATEWAY_ADDRESS": acct.address})
    print(f"load gateway address {acct.address}  (key written to {args.env_file}, not printed)")
    return 0


# --- estimate ------------------------------------------------------------------


def plan_n(k: int, n: int | None) -> int:
    """Runs per device at level k: given, or enough for >=30 settles, min 10."""
    return n if n is not None else max(10, math.ceil(30 / k))


def plan_per_channel(levels: list[int], n: int | None) -> dict[int, int]:
    """Settles each channel carries over the whole plan (channel i is used at every K >= i)."""
    per_channel = {i: 0 for i in range(1, max(levels) + 1)}
    for k in levels:
        for i in range(1, k + 1):
            per_channel[i] += plan_n(k, n)
    return per_channel


def tick_wei(price_wei_per_kwh: int) -> int:
    return math.ceil(MAX_TICK_MWH * price_wei_per_kwh / 1_000_000)


def cmd_estimate(args: argparse.Namespace) -> int:
    levels = [int(x) for x in args.levels.split(",")]
    kmax = max(levels)
    tick = tick_wei(args.price_wei_per_kwh)
    per_channel = plan_per_channel(levels, args.n)
    settles = sum(per_channel.values())

    gas_price = args.gas_price_wei
    source = "--gas-price-wei"
    if gas_price is None and args.rpc:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 10}))
        gas_price = int(w3.eth.gas_price)
        base = w3.eth.get_block("latest").get("baseFeePerGas")
        source = f"eth_gasPrice at {args.rpc} (base fee {base} wei), {datetime.now(timezone.utc):%Y-%m-%d %H:%MZ}"
    measured = FEE_PER_SETTLE_WEI / GAS_SETTLE
    print(f"plan: levels {levels}, runs/device " + ", ".join(f"K={k}: {plan_n(k, args.n)}" for k in levels))
    print(f"  settles per level: " + ", ".join(f"K={k}: {k * plan_n(k, args.n)}" for k in levels)
          + f"  -> total {settles}")
    print(f"  channels: {kmax} (opened once, reused across levels); settles per channel {per_channel}")
    print(f"  max tick value: {MAX_TICK_MWH:.0f} mWh x {args.price_wei_per_kwh} wei/kWh = {tick} wei")
    print(f"\nprice per gas: measured all-in {measured:,.0f} wei/gas (fee_wei {FEE_PER_SETTLE_WEI} / {GAS_SETTLE} gas)")
    if gas_price is not None:
        print(f"               current {gas_price:,} wei/gas ({source}); estimate uses the larger")
    p = max(measured, gas_price or 0)

    rows = []
    gas_open = kmax * GAS_OPEN
    gas_first = kmax * GAS_FIRST_SETTLE
    gas_later = (settles - kmax) * GAS_SETTLE
    gas_close = kmax * GAS_CLOSE
    for label, gas in (("open x%d" % kmax, gas_open), ("first settle x%d" % kmax, gas_first),
                       ("later settles x%d" % (settles - kmax), gas_later),
                       ("close x%d (optional)" % kmax, gas_close)):
        rows.append((label, gas, math.ceil(gas * p)))
    margin = args.margin
    deposits = {i: math.ceil(c * margin) * tick for i, c in per_channel.items()}
    print("\n  item                         gas units           wei")
    for label, gas, wei in rows:
        print(f"  {label:<26} {gas:>12,} {wei:>16,}")
    gas_wei = sum(w for _, _, w in rows)
    dep = sum(deposits.values())
    paid = settles * tick
    print(f"  {'gas total':<26} {sum(g for _, g, _ in rows):>12,} {gas_wei:>16,}")
    print(f"\n  deposits (x{margin} margin): {deposits}")
    print(f"  deposits total {dep:,} wei (locked; at most {paid:,} paid to the merchant, "
          "rest refundable only via close())")
    print(f"\n  TO FUND the load gateway key: gas {gas_wei:,} + deposits {dep:,} = {gas_wei + dep:,} wei")
    print(f"  with 30% headroom on gas: {math.ceil(gas_wei * 1.3) + dep:,} wei")
    return 0


# --- serve -------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    from eth_account import Account

    env = read_env(args.env_file)
    if "GATEWAY_PRIVATE_KEY" in env:
        raise SystemExit(f"{args.env_file} contains GATEWAY_PRIVATE_KEY; use LOAD_GATEWAY_PRIVATE_KEY only")
    key = env.get("LOAD_GATEWAY_PRIVATE_KEY")
    if not key:
        raise SystemExit("no LOAD_GATEWAY_PRIVATE_KEY; run `gateway-key` first")
    load_addr = Account.from_key(key).address
    m5 = args.m5_env
    if m5 and m5.exists():
        m5env = read_env(m5)
        m5_addrs = {m5env.get("GATEWAY_ADDRESS", "").lower()}
        if m5env.get("GATEWAY_PRIVATE_KEY"):
            m5_addrs.add(Account.from_key(m5env["GATEWAY_PRIVATE_KEY"]).address.lower())
        if load_addr.lower() in m5_addrs:
            raise SystemExit("the load gateway key is the M5 gateway key: nonces would collide")
    missing = [k for k in ("RPC_URL", "CHAIN_ID", "PAYMENT_CHANNEL_ADDRESS", "MERCHANT_ADDRESS") if not env.get(k)]
    if missing:
        raise SystemExit(f"{args.env_file} is missing {missing}")

    # Only what the relayer reads, nothing inherited that could retarget it.
    for k in ("GATEWAY_PRIVATE_KEY", "DEVICE_ADDRESS", "GATEWAY_PIPELINE", "GATEWAY_SUBMIT"):
        os.environ.pop(k, None)
    os.environ.update({
        "RPC_URL": env["RPC_URL"],
        "CHAIN_ID": env["CHAIN_ID"],
        "PAYMENT_CHANNEL_ADDRESS": env["PAYMENT_CHANNEL_ADDRESS"],
        "MERCHANT_ADDRESS": env["MERCHANT_ADDRESS"],
        "GATEWAY_PRIVATE_KEY": key,
        "GATEWAY_SUBMIT": "1",
        "GATEWAY_PIPELINE": "0" if args.no_pipeline else "1",
        "CONFIRMATIONS": env.get("CONFIRMATIONS", "1"),
    })
    for opt in ("GATEWAY_RECEIPT_TIMEOUT_S", "RPC_TIMEOUT_S"):
        if env.get(opt):
            os.environ[opt] = env[opt]
    # DEVICE_ADDRESS stays unset: in pipelining mode the relayer checks each
    # signer against its channel's on-chain device. With --no-pipeline (the
    # control) the default path accepts any recovered signer and logs a
    # warning; the contract still enforces the device signature.
    print(f"  load gateway {load_addr}  pipeline={'off' if args.no_pipeline else 'on'}")
    print(f"  chain {env['RPC_URL']} chainId {env['CHAIN_ID']}  contract {env['PAYMENT_CHANNEL_ADDRESS']}")
    print(f"  listening on {args.host}:{args.port}")
    import uvicorn

    uvicorn.run("gateway.api:app", host=args.host, port=args.port, log_level="warning")
    return 0


# --- setup -------------------------------------------------------------------


def ssh(command: str, *, input_text: str | None = None, timeout: float = 60) -> str:
    from experiments.runner import ssh_argv

    proc = subprocess.run(ssh_argv(command), input=input_text, capture_output=True, text=True,
                          timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"ssh `{command}` failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def device_addresses(args: argparse.Namespace) -> dict[int, str]:
    if args.local:
        raw = (args.local / "keystore" / "addresses.json").read_text(encoding="utf-8")
    else:
        raw = ssh(f"cat {PI_REPO}/device/keystore/load/addresses.json")
    return {int(k): v for k, v in json.loads(raw).items()}


def cmd_setup(args: argparse.Namespace) -> int:
    from eth_account import Account
    from web3 import Web3

    from scripts.m2_setup import deploy, load_artifact, open_channel

    env = read_env(args.env_file)
    w3 = Web3(Web3.HTTPProvider(env["RPC_URL"], request_kwargs={"timeout": 15}))
    chain_id = int(env["CHAIN_ID"])
    if w3.eth.chain_id != chain_id:
        raise SystemExit(f"node chain id {w3.eth.chain_id} != {chain_id}")
    gateway = Account.from_key(env["LOAD_GATEWAY_PRIVATE_KEY"])
    merchant = Web3.to_checksum_address(env["MERCHANT_ADDRESS"])
    abi, bytecode = load_artifact()

    if args.deploy:
        if chain_id != 31337:
            raise SystemExit("--deploy is for a local anvil only; reuse the deployed contract")
        contract = deploy(w3, gateway, abi, bytecode)
        update_env(args.env_file, {"PAYMENT_CHANNEL_ADDRESS": contract.address})
        env["PAYMENT_CHANNEL_ADDRESS"] = contract.address
    contract = w3.eth.contract(address=Web3.to_checksum_address(env["PAYMENT_CHANNEL_ADDRESS"]), abi=abi)
    if not w3.eth.get_code(contract.address):
        raise SystemExit(f"no contract at {contract.address}")

    devices = device_addresses(args)
    price = int(env.get("PRICE_WEI_PER_KWH", SCALED_PRICE_WEI_PER_KWH))
    planned = plan_per_channel([int(x) for x in args.levels.split(",")], args.n)
    bal = w3.eth.get_balance(gateway.address)
    print(f"  load gateway {gateway.address} balance {bal:,} wei")

    for i in range(1, args.k + 1):
        device = Web3.to_checksum_address(devices[i])
        chan = env.get(f"LOAD_CHANNEL_{i}")
        if chan and env.get(f"LOAD_DEVICE_{i}", "").lower() == device.lower() and not args.reopen:
            print(f"  dev{i}: reusing channel {chan}")
        else:
            if device in (gateway.address, merchant):
                raise SystemExit(f"dev{i} {device} is one of the other signers")
            deposit = (args.deposit_wei if args.deposit_wei is not None
                       else math.ceil(planned.get(i, 0) * args.margin) * tick_wei(price))
            chan = open_channel(w3, contract, gateway, device, gateway.address, merchant, deposit)
            update_env(args.env_file, {f"LOAD_DEVICE_{i}": device, f"LOAD_CHANNEL_{i}": chan})
        overlay = {
            "CHANNEL_ADDRESS": chan,
            "VERIFYING_CONTRACT": contract.address,
            "CHAIN_ID": str(chain_id),
            "PRICE_WEI_PER_KWH": str(price),
            "GATEWAY_URL": args.device_gateway_url,
        }
        if args.local:
            # Smoke only: the local devices have no base .env to take the
            # merchant key from, so it comes from the (anvil) env file.
            overlay["MERCHANT_PRIVATE_KEY"] = env["MERCHANT_PRIVATE_KEY"]
        text = "".join(f"{k}={v}\n" for k, v in overlay.items())
        if args.local:
            (args.local / f".env.load.dev{i}.channel").write_text(text, encoding="utf-8")
        else:
            ssh(f"umask 077 && cat > {PI_REPO}/.env.load.dev{i}.channel", input_text=text)
    print(f"  spent {bal - w3.eth.get_balance(gateway.address):,} wei (gas + deposits)")
    return 0


# --- run ---------------------------------------------------------------------


def gateway_health(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{url}/health", timeout=10) as resp:
        return json.load(resp)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()


def device_command(args: argparse.Namespace, i: int, n: int, start_at: float, seed: int,
                   tag: str, iface: str | None) -> list[str]:
    common = [
        "--device-index", str(i), "--n", str(n), "--start-at", f"{start_at:.3f}",
        "--seed", str(seed + i), "--tag", tag, "--timeout", str(args.timeout),
        "--gap-min", str(args.gap_min), "--gap-max", str(args.gap_max),
    ]
    if args.local:
        return [sys.executable, str(REPO_ROOT / "device" / "run_load.py"),
                "--env-overlay", str(args.local / f".env.load.dev{i}"),
                "--env-overlay", str(args.local / f".env.load.dev{i}.channel"),
                "--base-env", "none", *common]
    from experiments.runner import ssh_argv

    remote = " ".join([
        f"cd {PI_REPO} && ./venv/bin/python device/run_load.py",
        f"--env-overlay .env.load.dev{i} --env-overlay .env.load.dev{i}.channel",
        *(shlex.quote(c) for c in common),
        *(["--expect-iface", iface] if iface else []),
    ])
    return ssh_argv(remote, host=args.pi_host)


def pump(i: int, proc: subprocess.Popen, out: queue.Queue) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("{"):
            out.put((i, json.loads(line)))
    out.put((i, None))


def run_level(args: argparse.Namespace, meta: dict[str, Any], k: int) -> Path:
    n = plan_n(k, args.n)
    started = datetime.now(timezone.utc)
    session = f"{started:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    seed = args.seed if args.seed is not None else int.from_bytes(os.urandom(4), "big")
    row0 = {
        "session": session, "experiment": "cc", "level": k, "nPerDevice": n,
        "transport": "loopback" if args.local else args.transport, "deviceHost": "local" if args.local else "pi",
        "chain": meta["chain"], "chainId": meta["chainId"], "contract": meta["contract"],
        "gateway": meta["gateway"], "pipeline": meta["pipeline"], "commit": meta["commit"],
        "piCommit": meta["piCommit"], "seed": seed, "startedUtc": started.isoformat(timespec="seconds"),
        "wifiPowersave": meta["wifiPowersave"], "cpuGovernor": meta["cpuGovernor"],
    }
    args.results_dir.mkdir(parents=True, exist_ok=True)
    path = args.results_dir / f"cc_{meta['chain']}_k{k}_{session}.csv"
    lead = args.lead + k * args.stagger
    start_at = time.time() + lead
    say(f"level K={k}: {n} runs/device, start in {lead:.0f} s -> {path.name}")

    procs: list[subprocess.Popen] = []
    q: queue.Queue = queue.Queue()
    iface = meta.get("iface")
    for i in range(1, k + 1):
        cmd = device_command(args, i, n, start_at, seed, session, iface)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=None, text=True, bufsize=1,
                                cwd=REPO_ROOT)
        procs.append(proc)
        threading.Thread(target=pump, args=(i, proc, q), daemon=True).start()
        time.sleep(args.stagger)  # keystore decryption (scrypt, 256 MB) one at a time

    done = 0
    counts: dict[str, int] = {}
    with path.open("x", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SESSION_COLUMNS + RECORD_COLUMNS)
        writer.writeheader()
        fh.flush()
        while done < k:
            i, record = q.get()
            if record is None:
                done += 1
                continue
            if set(record) != set(RECORD_COLUMNS):
                raise RuntimeError(f"record shape drifted: {sorted(set(record) ^ set(RECORD_COLUMNS))}")
            writer.writerow({**row0, **record})
            fh.flush()
            counts[record["status"]] = counts.get(record["status"], 0) + 1
    for i, proc in enumerate(procs, 1):
        code = proc.wait()
        if code:
            say(f"  dev{i} exited with code {code}")
    say(f"  wrote {path.name}: {counts}")
    return path


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    head = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    if dirty and not args.allow_dirty:
        raise SystemExit("working tree is dirty: commit first, or --allow-dirty for a smoke run")
    health = gateway_health(args.gateway)
    if health.get("status") != "ok" or not health.get("submit"):
        raise SystemExit(f"gateway not ready to settle: {health}")
    pipeline = bool(health.get("pipeline"))
    if not pipeline and not args.allow_sequential:
        raise SystemExit("gateway is not in pipelining mode (pass --allow-sequential for the control)")
    meta: dict[str, Any] = {
        "commit": head + ("-dirty" if dirty else ""),
        "chainId": int(health["chainId"]),
        "chain": chain_name(int(health["chainId"])),
        "contract": health["paymentChannel"],
        "gateway": health["gateway"],
        "pipeline": pipeline,
        "piCommit": "", "wifiPowersave": "", "cpuGovernor": "", "iface": None,
    }
    env = read_env(args.env_file)
    if env.get("LOAD_GATEWAY_ADDRESS", "").lower() != str(health["gateway"]).lower():
        raise SystemExit("the gateway at --gateway is not running the load key from the env file")
    if not args.local:
        from experiments.runner import TRANSPORTS

        pi_head = ssh(f"cd {PI_REPO} && git rev-parse HEAD").strip()
        pi_dirty = bool(ssh(f"cd {PI_REPO} && git status --porcelain --untracked-files=no").strip())
        if pi_head != head and not args.allow_dirty:
            raise SystemExit(f"Pi is on {pi_head[:7]}, laptop on {head[:7]}: sync the Pi first")
        meta["piCommit"] = pi_head + ("-dirty" if pi_dirty else "")
        meta["iface"] = TRANSPORTS[args.transport][0]
        meta["wifiPowersave"] = ssh(
            "nmcli -g 802-11-wireless.powersave connection show "
            "\"$(nmcli -g GENERAL.CONNECTION device show wlan0)\"").strip()
        meta["cpuGovernor"] = "/".join(sorted(set(
            ssh("cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor").split())))
    return meta


def check_channels(args: argparse.Namespace, k: int, n_total: dict[int, int]) -> None:
    """Every channel exists, names the load gateway, and can cover its ticks."""
    env = read_env(args.env_file)
    price = int(env.get("PRICE_WEI_PER_KWH", SCALED_PRICE_WEI_PER_KWH))
    for i in range(1, k + 1):
        chan = env.get(f"LOAD_CHANNEL_{i}")
        if not chan:
            raise SystemExit(f"no LOAD_CHANNEL_{i}: run setup --k {k}")
        with urllib.request.urlopen(f"{args.gateway}/channel/{chan}", timeout=10) as resp:
            st = json.load(resp)
        if not st["exists"] or st["closed"]:
            raise SystemExit(f"channel {i} {chan} missing or closed")
        if st["gateway"].lower() != env["LOAD_GATEWAY_ADDRESS"].lower():
            raise SystemExit(f"channel {i} names gateway {st['gateway']}, not the load key")
        need = n_total.get(i, 0) * tick_wei(price)
        if st["remaining"] < need:
            raise SystemExit(f"channel {i} has {st['remaining']} wei left, plan needs up to {need}")


def cmd_run(args: argparse.Namespace) -> int:
    levels = [int(x) for x in args.levels.split(",")]
    per_channel = plan_per_channel(levels, args.n)
    meta = preflight(args)
    check_channels(args, max(levels), per_channel)
    other = None
    if not args.local and not args.keep_links:
        from experiments.runner import TRANSPORTS, link, pi_addresses

        measured, other = TRANSPORTS[args.transport]
        args.pi_host = pi_addresses()[measured]
        say(f"  taking {other} down; control over {measured} @ {args.pi_host}")
        link("disconnect", other, host=args.pi_host)
    try:
        paths = []
        for k in levels:
            paths.append(run_level(args, meta, k))
            if k != levels[-1]:
                time.sleep(args.pause)
    finally:
        if other:
            from experiments.runner import link

            say(f"  restoring {other}")
            try:
                link("connect", other, host=args.pi_host)
            except Exception as exc:  # noqa: BLE001
                say(f"  WARNING: could not bring {other} back up: {exc}")
    import pandas as pd

    print_levels(summarise_levels(pd.concat([load_file(p) for p in paths], ignore_index=True)))
    return 0


# --- analyze -----------------------------------------------------------------


def load_file(path: Path):
    import pandas as pd

    d = pd.read_csv(path)
    d["file"] = path.name
    return d


def ci95(x) -> tuple[float, float, int]:
    from scipy import stats

    x = x.dropna().astype(float)
    n = len(x)
    if n < 2:
        return (float(x.mean()) if n else math.nan, math.nan, n)
    return float(x.mean()), float(stats.t.ppf(0.975, n - 1) * x.std(ddof=1) / math.sqrt(n)), n


def summarise_levels(df):
    """One row per (chain, transport, pipeline, level, session): throughput, blocks,
    latency, failures. Sessions are never pooled: a throughput span across two
    sessions is meaningless, and a rerun may use another RPC provider (`rpcHost`,
    taken from the rows that reached the chain; failed rows may carry none)."""
    import pandas as pd

    rows = []
    df = df.copy()
    df["sealed_ms"] = df["latency_ms"] + (df["t5"] - df["t4"]) * 1000
    keys = ["chain", "transport", "deviceHost", "pipeline", "level", "session"]
    for key, g in df.groupby(keys, sort=True):
        ok = g[g["status"] == "ok"]
        hosts = g["rpcHost"].dropna().unique() if "rpcHost" in g else []
        clean = ok[ok["gapBehind"].isna()]
        # Device clock (all K processes share one machine's clock): first
        # sensor event of the level to the last settled reply.
        span = (ok["t0_wall"] + ok["latency_ms"] / 1000).max() - g["t0_wall"].min() if len(ok) else math.nan
        per_block = ok["blockNumber"].value_counts()
        block_span = ok["blockNumber"].max() - ok["blockNumber"].min() + 1 if len(ok) else math.nan
        lat, lat_ci, lat_n = ci95(clean["latency_ms"])
        sea, sea_ci, _ = ci95(clean["sealed_ms"])
        statuses = g["status"].value_counts().to_dict()
        rows.append({
            **dict(zip(keys, key)),
            "rpcHost": ";".join(sorted(map(str, hosts))),
            "attempted": len(g), "ok": len(ok),
            "failed": len(g) - len(ok),
            "statuses": ";".join(f"{s}={c}" for s, c in sorted(statuses.items())),
            "span_s": span,
            "settled_per_s": len(ok) / span if span and span > 0 else math.nan,
            "blocks_with_settle": int(per_block.size),
            "block_span": block_span,
            "settles_per_block": len(ok) / block_span if block_span else math.nan,
            "max_settles_in_one_block": int(per_block.max()) if len(per_block) else 0,
            "mean_settles_per_nonempty_block": float(per_block.mean()) if len(per_block) else math.nan,
            "latency_ms_mean": lat, "latency_ms_ci95": lat_ci, "latency_n": lat_n,
            "sealed_ms_mean": sea, "sealed_ms_ci95": sea_ci,
            "gap_affected_ok": int(ok["gapBehind"].notna().sum()),
            "nonce_resyncs": int(g["nonceResyncs"].max()) if g["nonceResyncs"].notna().any() else 0,
            "max_inflight": int(g["inflight"].max()) if g["inflight"].notna().any() else None,
            "commits": ";".join(sorted(g["commit"].astype(str).str[:7].unique())),
        })
    return pd.DataFrame(rows)


def print_levels(s) -> None:
    for _, r in s.iterrows():
        print(f"\n== {r.chain} / {r.transport} / devices on {r.deviceHost} / pipeline={r.pipeline} / K={r.level}"
              f" / {r.rpcHost} / {r.session}"
              f"   ok {r.ok}/{r.attempted}   [{r.statuses}]")
        print(f"  settled/s {r.settled_per_s:.2f} over {r.span_s:.1f} s; {r.settles_per_block:.2f} settles/block "
              f"over {r.block_span} blocks (max {r.max_settles_in_one_block} in one)")
        print(f"  latency {r.latency_ms_mean:.1f} ± {r.latency_ms_ci95:.1f} ms (n={r.latency_n}, gap-free ok); "
              f"sealed {r.sealed_ms_mean:.1f} ± {r.sealed_ms_ci95:.1f} ms")
        print(f"  nonce resyncs {r.nonce_resyncs}, gap-affected ok rows {r.gap_affected_ok}, "
              f"max in flight {r.max_inflight}")


def cmd_analyze(args: argparse.Namespace) -> int:
    import glob

    import pandas as pd

    files = sorted(glob.glob(str(args.results / "cc_*.csv")))
    if not files:
        raise SystemExit(f"no cc_*.csv in {args.results}")
    s = summarise_levels(pd.concat([load_file(Path(f)) for f in files], ignore_index=True))
    print_levels(s)
    args.out.mkdir(parents=True, exist_ok=True)
    s.to_csv(args.out / "concurrency.csv", index=False, float_format="%.4f")
    say(f"wrote {args.out / 'concurrency.csv'}")
    return 0


# --- main --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("gateway-key")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_gateway_key)

    s = sub.add_parser("estimate")
    s.add_argument("--levels", default="1,2,4,8")
    s.add_argument("--n", type=int, default=None, help="runs per device (default: max(10, ceil(30/K)))")
    s.add_argument("--price-wei-per-kwh", type=int, default=SCALED_PRICE_WEI_PER_KWH)
    s.add_argument("--margin", type=float, default=1.5, help="deposit headroom over planned ticks")
    s.add_argument("--rpc", default=None, help="read-only: current gas price from this RPC")
    s.add_argument("--gas-price-wei", type=int, default=None)
    s.set_defaults(func=cmd_estimate)

    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8001)
    s.add_argument("--no-pipeline", action="store_true", help="control: the sequential gateway")
    s.add_argument("--m5-env", type=Path, default=REPO_ROOT / ".env",
                   help="refuse if the load key equals this file's gateway key")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("setup")
    s.add_argument("--k", type=int, default=8)
    s.add_argument("--deposit-wei", type=int, default=None)
    s.add_argument("--levels", default="1,2,4,8", help="plan the deposits are sized for")
    s.add_argument("--n", type=int, default=None, help="runs per device, as for `run`")
    s.add_argument("--margin", type=float, default=1.5, help="deposit headroom over planned ticks")
    s.add_argument("--device-gateway-url", required=True, help="the load gateway as the devices reach it")
    s.add_argument("--local", type=Path, default=None, help="smoke: device keys/overlays in this dir")
    s.add_argument("--deploy", action="store_true", help="anvil only: deploy a fresh PaymentChannel")
    s.add_argument("--reopen", action="store_true", help="open new channels even if recorded")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("run")
    s.add_argument("--levels", default="1,2,4,8")
    s.add_argument("--n", type=int, default=None, help="runs per device (default: max(10, ceil(30/K)))")
    s.add_argument("--transport", default="wifi", choices=["wifi", "eth"])
    s.add_argument("--gateway", default="http://127.0.0.1:8001", help="load gateway, laptop side")
    s.add_argument("--local", type=Path, default=None, help="smoke: run devices locally from this dir")
    s.add_argument("--pi-host", default=None)
    s.add_argument("--keep-links", action="store_true", help="do not switch the Pi's other link off")
    s.add_argument("--gap-min", type=float, default=0.0)
    s.add_argument("--gap-max", type=float, default=0.0)
    s.add_argument("--timeout", type=float, default=150.0)
    s.add_argument("--seed", type=int, default=None)
    s.add_argument("--lead", type=float, default=10.0, help="seconds before the start barrier")
    s.add_argument("--stagger", type=float, default=2.0, help="seconds between device launches")
    s.add_argument("--pause", type=float, default=10.0, help="seconds between levels")
    s.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    s.add_argument("--allow-dirty", action="store_true")
    s.add_argument("--allow-sequential", action="store_true", help="control run against --no-pipeline")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("analyze")
    s.add_argument("--results", type=Path, default=RESULTS_DIR)
    s.add_argument("--out", type=Path, default=OUT_DIR)
    s.set_defaults(func=cmd_analyze)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
