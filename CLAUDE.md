# IoT L2 Micropayments — Prototype Spec

Thesis artifact for *"Blokų grandinės pagrindu veikiantys finansiniai atsiskaitymai tarp IoT
įrenginių"*. This file is the working spec; keep it at repo root so Claude Code has context in
every session.

## What this thing is

A Raspberry Pi 4 detects a service event (simulated EV charging session), signs a payment
voucher with its own private key, and hands it to a gateway node. The gateway pays the gas and
submits the settlement to an Ethereum L2. The Pi never holds chain state and never pays gas.
The contract will not release funds on the gateway's signature alone.

Four layers, matching §3.2 of the thesis:

| Layer | Component | Runs on |
|---|---|---|
| L1 | Ethereum Sepolia — final settlement, cost baseline | public testnet |
| L2 | Base Sepolia — the rollup where the contract lives | public testnet |
| Gateway | relayer + channel manager, pays gas | laptop / VM |
| Device | payment agent, signs vouchers | Raspberry Pi 4 |

## Hypotheses this code has to be able to answer

- **H1** — mean end-to-end latency is in the low-seconds range and per-tx cost is far below L1.
  → needs the timestamp instrumentation and the experiment runner.
- **H2** — the 3-of-3 channel stops the gateway from moving funds unilaterally, without
  destroying throughput. → needs the negative Foundry tests *and* the throughput run.
- **H3** — regulatory (MiCA/PSD2). Analytical, no code. Ignore here.

If a feature doesn't serve H1 or H2, it's out of scope. No web UI, no dashboard, no token
economics. Resist scope creep — the deliverable is measurements, not a product.

## Stack

- **Contracts**: Solidity 0.8.24+, Foundry (`forge`, `anvil`, `cast`). Use `forge snapshot` for
  gas — those numbers go straight into the thesis cost table.
- **Off-chain**: Python 3.11, `web3.py`, `eth-account`, `fastapi` + `uvicorn` for the gateway,
  `bleak` / BlueZ for the Bluetooth transport, `pydantic` for the voucher schema.
- **Analysis**: `pandas`, `scipy` (for confidence intervals), `matplotlib`.
- **Local chain**: `anvil` with `--block-time 2` to roughly imitate L2 block cadence.

One language off-chain (Python) on purpose — you're a solo author and the Pi side needs Python
anyway.

## Repo layout

```
iot-l2-micropayments/
├── CLAUDE.md
├── .env.example              # RPC URLs, keys — NEVER commit .env
├── contracts/                # foundry project
│   ├── src/
│   │   ├── PaymentChannel.sol
│   │   └── VoucherLib.sol    # EIP-712 hashing + recovery
│   ├── test/
│   │   ├── Settlement.t.sol      # happy path
│   │   ├── GatewayAbuse.t.sol    # H2 — the important one
│   │   └── Replay.t.sol
│   └── script/Deploy.s.sol
├── shared/
│   ├── voucher.py            # canonical EIP-712 struct, shared by device+gateway+merchant
│   └── config.py
├── device/                   # deployed to the Pi
│   ├── agent.py              # sensor event → sign → transmit
│   ├── sensor.py             # simulated kWh meter
│   ├── transport/
│   │   ├── http_client.py    # WiFi / Ethernet
│   │   └── ble_client.py     # Bluetooth
│   └── keystore/             # encrypted key, gitignored
├── gateway/
│   ├── api.py                # POST /voucher, GET /health
│   ├── relayer.py            # verify sig → build tx → submit → await receipt
│   ├── channel.py            # open / track / close channel state
│   └── ble_server.py
├── merchant/
│   └── signer.py             # third signer (charging station operator)
├── experiments/
│   ├── runner.py             # N>=30 runs per condition, writes CSV
│   ├── results/              # raw CSVs, committed — this is your data
│   └── analyze.py            # mean, stdev, 95% CI, plots for the thesis
└── docs/
    └── measurements.md       # log every run: date, chain, commit hash, conditions
```

## The voucher

EIP-712 typed data. Domain separator must include `chainId` and `verifyingContract` so a voucher
signed for Base Sepolia can't be replayed on Sepolia L1 or against a different channel.

```
Voucher {
  address channel;            // which channel this spends from
  uint256 nonce;              // strictly increasing, replay protection
  uint256 cumulativeAmount;   // monotonic total, channel-style — see below
  bytes32 serviceRef;         // keccak256(sessionId ‖ kWh ‖ timestamp) — the service fact
  uint64  deadline;           // voucher expiry
}
```

`cumulativeAmount` is the total owed so far, not the delta. This is the standard unidirectional
payment channel trick: if voucher #7 is lost in transit, voucher #8 supersedes it, and only the
highest one ever needs to hit the chain. It's also what makes "micropayments" honest — 200
charging ticks settle as one transaction, not 200.

## Contract, core shape

```solidity
function settle(
    Voucher calldata v,
    bytes calldata sigDevice,
    bytes calldata sigGateway,
    bytes calldata sigMerchant
) external {
    require(block.timestamp <= v.deadline, "expired");
    bytes32 digest = _hashTypedDataV4(_hashVoucher(v));
    require(ECDSA.recover(digest, sigDevice)   == channels[v.channel].device,   "bad device sig");
    require(ECDSA.recover(digest, sigGateway)  == channels[v.channel].gateway,  "bad gateway sig");
    require(ECDSA.recover(digest, sigMerchant) == channels[v.channel].merchant, "bad merchant sig");
    require(v.cumulativeAmount > channels[v.channel].paid, "not monotonic");
    require(v.nonce > channels[v.channel].nonce, "stale nonce");
    // ... transfer delta, update state, emit Settled
}
```

Use OpenZeppelin's `EIP712` and `ECDSA` — do not hand-roll signature recovery. Emit a `Settled`
event carrying `nonce` and `serviceRef`; the experiment runner matches receipts to runs on it.

## Instrumentation (this is what the whole thesis rests on)

Record every one of these per run, into the CSV:

| Mark | Where | What |
|---|---|---|
| `t0` | Pi | sensor event fires |
| `t1` | Pi | voucher signed |
| `t2` | gateway | voucher received over transport |
| `t3` | gateway | tx broadcast, hash returned |
| `t4` | gateway | receipt received (included in block) |
| `t5` | gateway | N-confirmation depth reached |

Reported latency = `t4 - t0`. Keep the intermediate marks so you can decompose the number in
ch. 4: signing cost vs transport cost vs chain cost. That decomposition is more interesting than
the total and reviewers will ask for it.

**Clock skew warning.** `t0`/`t1` come from the Pi's clock, the rest from the gateway's. Run
`chrony` on both against the same NTP source before every session, and *also* record a
gateway-side round-trip (`t_send → t_ack`) as a sanity check. If the two disagree by more than a
few ms, your latency numbers are invalid. Log the measured offset in `docs/measurements.md`.

Also record per run: `gasUsed`, `effectiveGasPrice`, and on OP-stack chains like Base the
`l1Fee` field in the receipt (people forget this and understate rollup cost by a lot). Report
cost in **gas units** primarily and EUR secondarily, pinned to one ETH price on one stated date —
gas units are reproducible, EUR isn't.

## Milestones

Do these in order. Don't skip ahead; each one's failure mode is much easier to find in isolation.

**M0 — scaffold.** `forge init`, Python venv, `.env.example`, anvil running locally, one trivial
contract deployed and read back with `cast`.
*Done when:* `forge test` passes on an empty test and `cast call` returns a value from anvil.

**M1 — end-to-end on one machine.** Everything on the laptop against anvil. Fake sensor →
sign → HTTP POST → gateway relays → `Settled` event observed. All three signers can be local
keys for now.
*Done when:* one command produces a `Settled` event and prints t0–t4.

**M2 — split onto the Pi.** Device agent moves to the Pi. Ethernet first (it's your most stable
link and makes a good control condition), then WiFi. Gateway stays on the laptop. Key
generated *on the Pi*, private key never leaves it.
*Done when:* same flow, sensor event physically originates on the Pi.

**M3 — real L2.** Swap the RPC to Base Sepolia, fund the gateway from a faucet, deploy, rerun.
*Done when:* you have a block explorer link to a real `Settled` tx triggered by the Pi.

**M4 — the channel.** `open()` / off-chain voucher accumulation / `close()`. 200 service ticks,
one on-chain settlement.
*Done when:* amortised on-chain cost per micropayment drops by ~2 orders of magnitude, measured.

**M5 — experiment harness.** `runner.py` does N≥30 per condition, writes CSV, handles failures
without corrupting the dataset. Conditions: {Ethernet, WiFi, Bluetooth} × {L2, L1}.
*Done when:* `analyze.py` prints mean/stdev/95% CI per condition and saves the plots.

**M6 — security (H2).** Foundry tests that must all revert: gateway-only signature; forged
device signature; replayed voucher; non-monotonic amount; expired deadline; voucher from another
channel; voucher from another chainId. Plus a live attempt from the running gateway with the
device signature stripped.
*Done when:* every abuse case reverts and you have the test output to paste into ch. 4.

**M7 — energy.** Inline USB power meter on the Pi. Measure idle baseline for 60s, then integrate
over the operation window, ≥30 repeats. If you can't get a meter, fall back to CPU-time as a
proxy and *state it explicitly as a limitation* — don't quietly substitute it.

## Comparison base

Your own measurements: Base Sepolia (L2) and Ethereum Sepolia (L1), same contract, same runner.
Lightning Network, IOTA and Microchain figures come from the literature (Kurt et al. 2021 give
2.6 s WiFi / 5.7 s Bluetooth — that's your direct latency reference point). Do not implement
those. If M0–M7 finish early, spend the time on more repetitions and tighter confidence
intervals, not on a second architecture.

## Rules

- Testnets only. No mainnet, ever, no real funds.
- `.env` and `device/keystore/` are gitignored. Commit `.env.example` only.
- Commit `experiments/results/*.csv`. Raw data is part of the thesis.
- Every measurement run gets a row in `docs/measurements.md` with the git commit hash. If you
  can't tie a number to a commit, you can't defend it.
- Tag the commit used for the final reported results.
