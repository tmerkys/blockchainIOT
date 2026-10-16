# Thesis brief — for the writer

Handoff document for writing the thesis *"Blokų grandinės pagrindu veikiantys finansiniai
atsiskaitymai tarp IoT įrenginių"* (blockchain-based financial settlement between IoT devices).
Prepared 2026-10-14 from the prototype repository (branch `develop`, last data commit `a0ea140`,
cost table `b2bdc48`, EIP-8037 note `86d1ec1`). Updated 2026-10-15: device CPU-time session
(code `c602b61`), L2 WiFi controlled pooled to n = 60; H2 throughput (`e831b13`); Base `t4` re-interpreted
as a Flashblocks pre-confirmation, with time-to-sealed-block added (§4.5). Updated 2026-10-16
(**results-v2**): dedicated RPC endpoint vs public (§4.6), time to finality on L2 and L1 (§4.7),
concurrent load K = 1–8 devices with a pipelining gateway (§5.3). The results-v1 tables (§4.1–§4.5,
`summary.csv`, `tests.csv`) are unchanged — the v2 sessions write to separate files.

This brief is the map; the files listed in §10 are the territory. Where they disagree, **the files
win**, and the disagreement is a bug in this brief.

---

## 0. Rules for whoever writes from this

1. **Never invent or round a number into existence.** Every figure in the thesis must trace to a file
   in §10 (`summary.csv`, `tests.csv`, `costs_2026-10-14.csv`, `rpc_*.csv`, `finality_*.csv`,
   `concurrency.csv`, `measurements.md`). Report means as
   *mean ± 95% CI (n)*, e.g. *957.5 ± 54.5 ms (n = 30)* — not "about 0.95 s" in a results section.
2. **Keep the two Pi configurations apart** (*default* vs *controlled*, §4.4). They are different
   conditions; mixing them invalidates the comparison.
3. **Testnet prices are not costs.** Cost claims use the mainnet-priced table (§6.2) only. Testnet
   fees appear only as a methodological note.
4. **Gas units are the primary cost unit, EUR secondary**, pinned to 2026-10-14 at 2,228.86 EUR/ETH.
5. **Do not overclaim.** N = 30 per condition, one device, one gateway, one lab LAN, public RPC
   endpoints, testnets. Every finding below has its scope stated; keep it.
6. **Mark what is pending** (Bluetooth, energy/M7, H3) as pending — do not fill with literature values
   presented as our own.
7. Literature comparison figures (Kurt et al. 2021: 2.6 s WiFi, 5.7 s Bluetooth; Lightning, IOTA,
   Microchain) come **from the literature**, cited — none of those systems was implemented here.

---

## 1. What the system is

A Raspberry Pi 4 detects a service event (a simulated EV charging tick), signs a payment voucher
with its own private key, and sends it to a gateway. The gateway pays the gas and submits the
settlement to an Ethereum Layer-2 rollup. **The Pi never holds chain state and never pays gas**
(verified: device balance 0 wei, transaction count 0 throughout). **The contract will not release
funds on the gateway's signature alone** — every settlement needs three signatures (device, gateway,
merchant).

### 1.1 Four layers (thesis §3.2)
| Layer | Component | Runs on (as built) |
|---|---|---|
| L1 | Ethereum Sepolia — final settlement, latency/cost baseline | public testnet, chainId 11155111 |
| L2 | Base Sepolia — rollup where the contract lives | public testnet (OP-stack), chainId 84532 |
| Gateway | relayer + channel manager, pays gas (Python, FastAPI) | laptop, Windows 11, Python 3.11, wired Ethernet |
| Device | payment agent, signs vouchers (Python) | Raspberry Pi 4, Debian (kernel 6.18), Python 3.13 |

### 1.2 The voucher (EIP-712 typed data)
```
Voucher { address channel; uint256 nonce; uint256 cumulativeAmount; bytes32 serviceRef; uint64 deadline; }
```
- EIP-712 domain: name `MagisVoucher`, version `1`, **chainId and verifyingContract included** — a
  voucher signed for one chain/contract is invalid on any other (tested, §5).
- `cumulativeAmount` is the running total owed, not a delta (unidirectional payment-channel design):
  a lost voucher is superseded by the next, and only the highest ever needs to reach the chain.
- `serviceRef = keccak256(sessionId ‖ kWh ‖ timestamp)` binds the payment to the service fact.
- `nonce` strictly increasing (replay protection); `deadline` = expiry (default 300 s).

### 1.3 The contract (`PaymentChannel.sol`, Solidity 0.8.24+, OpenZeppelin EIP712 + ECDSA)
- `open(device, gateway, merchant)` payable — escrows the deposit, records the three signers and the
  funder.
- `settle(v, sigDevice, sigGateway, sigMerchant)` — checks deadline, recovers all three signatures over
  the same EIP-712 digest, requires `cumulativeAmount` > paid and `nonce` > last nonce, pays the delta
  to the merchant, emits `Settled(nonce, serviceRef, …)`.
- `close(...)` — same 3-of-3 checks (shared internal `_applyVoucher`), pays the final delta and
  refunds the remaining deposit **to the recorded funder, never to `msg.sender`** (the gateway gains
  nothing by submitting it).
- Cooperative close only — see limitations (§8).

### 1.4 Data flow and instrumentation marks
| Mark | Where | What |
|---|---|---|
| t0 | Pi | sensor event fires |
| t1 | Pi | voucher signed |
| t2 | gateway | voucher received (stamped in HTTP middleware, before body parsing) |
| t3 | gateway | transaction broadcast, hash returned |
| t4 | gateway | receipt received — on L1 the block; **on Base a Flashblocks pre-confirmation** (§4.5) |
| t5 | gateway | N-confirmation depth reached (N = 1) — on Base, the sealed block (§4.5) |
| t6 | gateway | response handed back (diagnostic only) |

**Reported latency = t4 − t0, measured on one clock.** t0 is the Pi's clock and t2–t6 the laptop's, so
subtracting across them would fold NTP offset into the result. Instead the gateway holds the HTTP
request open until the receipt, the Pi stamps the reply's arrival on its own monotonic clock
(`t4_dev`), and the reported value is `(t4_dev − t0) − (t6 − t4)` — a device-clock duration minus a
gateway-clock duration. No absolute instant crosses the machine boundary, so **there is no clock-skew
term** (tested against ±500 ms injected skew). Decomposition terms:
- signing = t1 − t0 (Pi); network = Pi round trip − (t6 − t2) (both legs, skew-free);
  gateway build + broadcast = t3 − t2; block inclusion = t4 − t3 (on Base: wait for pre-confirmation);
  to sealed block = latency + (t5 − t4); other = residual (merchant
  countersignature on the Pi + framing).
- Clock offset (gateway − device) is estimated NTP-style per run and reported **as a diagnostic only**;
  chrony on the Pi (time.cloudflare.com); laptop on Windows Time (within ~2 ms on 2026-10-14, 0.48 s
  off on 2026-10-15 — §8.10).

---

## 2. Hypotheses and status

| | Hypothesis | Status |
|---|---|---|
| H1 | Mean end-to-end latency in the low-seconds range; per-tx cost far below L1 | **Supported** (L2: ~1 s to sequencer pre-confirmation, ~0.8 s with a dedicated RPC, ~2.3–2.4 s to a sealed block; L1 ~8.2–8.8 s; L2 cost ~19× below L1 per settlement at median price; channel adds ~100×) — §4, §6. **Scope:** latency, not finality — Base finalizes ~17.5 min after pre-confirmation, *later* than L1 (~16.2 min) (§4.7) |
| H2 | 3-of-3 channel stops the gateway moving funds unilaterally, without destroying throughput | **Supported.** Security §5.1. Throughput §5.2: the 3-of-3 costs +22.7% gas / −18.5% settles per block and 2.7× gateway CPU per voucher — a constant factor, not the binding limit. Concurrent load §5.3: with a pipelining gateway, K devices settle K per 2 s block, linear to K = 8 (4.11 settled/s, 0 failures) |
| H3 | Regulatory (MiCA / PSD2) | Analytical, no code — author's chapter |

---

## 3. Experimental protocol (M5)

- Runner `experiments/runner.py` (laptop) drives `device/run_batch.py` (Pi) over SSH.
- **Conditions:** transport {Ethernet, WiFi} × chain {L2 Base Sepolia, L1 Ethereum Sepolia} × Pi config
  {default, controlled}. N = 30 per condition (L2 WiFi controlled: n = 60, two sessions on
  consecutive days, pooled — Welch p = 0.46 between them); 270 settled runs, **0 failures**.
- **Link isolation:** the Pi's other interface is taken down for each session (both links share one
  /24, Ethernet wins on route metric, so a "WiFi" run with the cable in would silently be Ethernet).
  The route is checked before every tick and recorded per row (`iface`).
- **Run spacing:** random gap, uniform 2–4 s on L2 (2 s blocks) and 12–24 s on L1 (12 s slots), ≥ one
  block period, so each run lands at an uncorrelated point in the block cycle. Seed recorded.
- **Failure handling:** every attempt is a CSV row with a `status`; only `ok` rows enter statistics.
  Mined-but-reverted is a distinct status (never counted as success).
- **Statistics:** mean, sd, 95% CI as t-interval on the mean, median, min, max; Welch t-tests
  (unequal variances) between transports within a chain/config, and between chains within a
  transport/config.
- **Reproducibility:** every row carries both machines' git commit; same commit enforced on both;
  every session logged in `docs/measurements.md`.
- **Network:** ZTE 5G router, WPA2-PSK (AES, PMF off) — changed from its WPA2/WPA3 transition default,
  which the Pi's Broadcom radio could not authenticate to. Pi WiFi pinned to 5 GHz, ch. 36 (2.4 GHz
  heavily congested). Laptop/gateway is wired, so the WiFi condition varies only the Pi's hop.

---

## 4. Results — latency (H1)

All values: mean ± 95% CI, ms; n = 30 except L2 WiFi controlled, n = 60. Source: `experiments/analysis/summary.csv`, `tests.csv`.

### 4.1 Main table
| Chain | Transport | Config | **t4 − t0** | median | to sealed block | signing | network | build+broadcast | t4 − t3 |
|---|---|---|---|---|---|---|---|---|---|
| L2 Base | Ethernet | default | 1041.9 ± 66.0 | 1005.6 | 2613.8 ± 200.9 | 34.1 | 7.4 | 735.9 | 233.9 |
| L2 Base | WiFi | default | 1173.2 ± 100.3 | 1102.8 | 2601.5 ± 247.8 | 30.8 | 145.7 | 741.3 | 225.4 |
| L2 Base | Ethernet | controlled | **957.5 ± 54.5** | 981.5 | 2292.7 ± 228.3 | 13.6 | 6.1 | 709.7 | 214.7 |
| L2 Base | WiFi | controlled | **966.5 ± 48.3** | 935.1 | 2419.4 ± 168.4 | 13.5 | 13.0 | 692.5 | 234.3 |
| L1 Ethereum | Ethernet | controlled | **8169.9 ± 1454.7** | 8400.3 | 8256.4 ± 1455.0 | 13.6 | 8.5 | 676.6 | 7458.0 |
| L1 Ethereum | WiFi | controlled | **8736.4 ± 1354.8** | 8695.5 | 8825.0 ± 1354.9 | 13.5 | 18.3 | 682.4 | 8009.0 |

On L2 Base, `t4 − t0` is time to the sequencer's **pre-confirmation**, and "to sealed block" is time
until the 2 s L2 block containing the settlement is sealed; on L1 the two coincide (§4.5).

### 4.2 Findings
1. **L2 end-to-end latency ≈ 1 s to pre-confirmation** (controlled: 957.5 Ethernet / 966.5 WiFi ms) and
   **≈ 2.3–2.4 s to a sealed block** (2292.7 / 2419.4 ms) — "low seconds" on either definition, H1
   latency supported.
2. **L1 is ~3.6× slower than an L2 sealed block** (+6.0 s Ethernet, p = 2.7e-09; +6.4 s WiFi,
   p = 1.2e-10) **and ~8.5× slower than L2 pre-confirmation** (+7.2 s Ethernet, p = 4.7e-11; +7.8 s WiFi, p = 1.5e-12). **The entire
   difference is block inclusion**; gateway build+broadcast does not differ between chains (p = 0.11 /
   0.54). Verified against block timestamps: all 60 L1 transactions were included in the *next* slot
   (broadcast→block 0.8–13.4 s, mean 6.5 s ≈ half a 12 s slot); receipt seen ~1.2 s after the block.
3. **Where the time goes on L2 (controlled):** ~70% gateway↔public-RPC round trips (build+broadcast
   ≈ 690–710 ms), ~23% waiting for the Flashblocks pre-confirmation (≈ 215–235 ms; Base streams 200 ms sub-blocks), ~2% signing,
   ~1% network. **The chain is not the L2 bottleneck; the public RPC endpoint is.** On L1 the slot
   time dominates (~90%).
4. **Transport (Ethernet vs WiFi) does not matter end-to-end** under controlled config: L2 p = 0.80,
   L1 p = 0.56. On the wire WiFi costs ~7 ms more (13.0 vs 6.1 ms, p = 3.6e-32) — real but invisible
   against block and RPC time.
5. Comparison: Kurt et al. (2021) report 2.6 s over WiFi; our L2 WiFi figure is 0.97 s **with an
   on-chain settlement per payment**. (Different systems — cite, don't equate.)

### 4.3 Device-configuration finding (default vs controlled)
- **WiFi power save** (Linux/NetworkManager default on the Pi's Broadcom chip): in 11 of 30 default-config
  WiFi runs the network term was 187–518 ms (median 17.8 ms otherwise). The clock-offset estimate in
  those runs ≈ −½ × network, i.e. all excess on the gateway→Pi leg: the AP buffering the reply for a
  dozing radio. Confirmed by direct test: 3-s-spaced pings 447–507 ms in 4/10 with power save on,
  5–7 ms in 10/10 with it off. Turning it off removed the slow cluster entirely (0/30).
- **CPU governor** `ondemand` (default) drops the Pi to 600 MHz between runs: signing ~39 ms median
  (bimodal 13 / 39 ms). With `performance`: **13.5 ± 0.1 ms**, matching back-to-back signing in M4.
- **The default-config WiFi penalty (~131 ms, p = 0.03) was almost entirely power save**, not the radio
  link. Practical point for IoT payment agents: stock power management costs latency on every reply;
  disabling it costs energy (→ M7).
- The merchant countersignature (made on the Pi in this prototype) costs the same as the device
  signature (13.4 ms controlled, ~30 ms default) — the device effectively signs twice per payment.

### 4.4 Device CPU time per payment (M7 fallback — a proxy, not energy)
Source: `summary.csv` metrics `cpu_*`; one session, L2 WiFi controlled, n = 30, 2026-10-15, commit c602b61.
Measured with the process CPU clock (`CLOCK_PROCESS_CPUTIME_ID`, user + system).

| Device-side work | CPU ms, mean ± 95% CI | median |
|---|---|---|
| Sign (tick + voucher build + EIP-712 hash + ECDSA) | **13.50 ± 0.13** | 13.46 |
| Whole device-side operation, tick → parsed reply (excl. merchant) | **16.17 ± 0.16** | 16.10 |
| Merchant countersignature (simulated on the Pi; not the device's work) | 13.18 ± 0.06 | 13.18 |

- Signing is CPU-bound (CPU time = wall time, ratio 1.003) and is ~83% of the device's CPU per payment;
  request/JSON/HTTP add ~2.7 ms. The ~1 s spent waiting for the on-chain receipt costs the device
  effectively no CPU — the chain work is on the gateway.
- With the merchant simulated on the Pi, the Pi's actual CPU per payment is ~29.3 ms.
- **Must be presented as a proxy for device energy** (no power meter). It omits kernel network work
  (WiFi driver softirq), radio power, idle draw and the one-off key decryption: a lower bound on
  device effort. Do not convert to joules with a literature wattage.

---

### 4.5 Base receipts are pre-confirmations (Flashblocks) — how to report L2 latency
- Each run's receipt time `t4` was checked against the timestamp of the block the receipt names. On
  Base Sepolia the gateway held the receipt **0.8–1.5 s before that block's timestamp** (median per
  session; 100% of runs on 2026-10-14). On Ethereum Sepolia, 0.8–1.0 s *after* it (0% before).
- Mechanism: Base **Flashblocks** — the sequencer streams 200 ms sub-blocks inside each 2 s block, and
  Base's RPC returns receipts for pre-confirmed transactions. The pre-confirmation is the sequencer's
  promise; the sealed block follows within the 2 s block period.
- So the thesis should report both: **~0.96 s to pre-confirmation, ~2.3–2.4 s to a sealed L2 block.**
  The earlier statement "inclusion ≈ half a 2 s block period" was wrong and is corrected here.
- Neither is finality. A Base block is final once its batch is posted to and finalised on L1; an
  Ethereum block after two epochs. Finality was measured in results-v2 — §4.7.

### 4.6 Dedicated RPC endpoint vs public (results-v2, session v2-1)
Source: `experiments/analysis/rpc_summary.csv`, `rpc_tests.csv`; raw `v2_base-sepolia_wifi_20261015T16*.csv`;
commit 985c64e. Base Sepolia, WiFi, controlled, 2026-10-15. Two arms back to back, same contract and channel:
gateway on publicnode (same-day control) vs gateway on a dedicated endpoint (Alchemy, free tier). Not pooled
with §4.1 — a same-day control, so the day-to-day RPC variation does not enter the comparison.

| mean ± 95% CI (n), ms | publicnode (control) | Alchemy (dedicated) | Alchemy − publicnode, Welch |
|---|---|---|---|
| receipt definition | pre-confirmation 30/30 | pre-confirmation 30/30 | like for like |
| **latency t4 − t0** | 901.0 ± 51.0 (30) | **782.4 ± 46.1 (30)** | **−118.6, p = 8.3e-04** |
| build + broadcast t3 − t2 | 674.1 ± 25.2 (30) | **542.1 ± 27.8 (30)** | **−132.0, p = 1.5e-09** |
| t4 − t3 (wait for pre-confirmation) | 187.7 ± 41.5 (30) | 200.5 ± 39.8 (30) | +12.8, p = 0.65 |
| to sealed block | 2138.3 ± 204.4 (30) | 2190.3 ± 209.4 (30) | +52.0, p = 0.72 |

Findings:
1. **A dedicated endpoint cuts pre-confirmation latency by ~119 ms (~13%)**, all of it in the gateway's
   build + broadcast (RPC round trips); the chain-side terms do not move. Confirms §4.2.3: the RPC, not the
   chain, is the largest L2 latency term to pre-confirmation.
2. **Time to a sealed block does not change** (p = 0.72): it is set by Base's 2 s block period, not the
   provider. For sealed-block latency the block period dominates.
3. The public endpoint itself varies by day: build + broadcast was 692.5 ms in §4.1 (2026-10-14), 674.1 ms
   here, and 542 ms on Ethereum Sepolia publicnode in v2-2 vs 682 ms in results-v1 (p = 2.5e-11,
   `measurements.md` v2-2). Hence the same-day control.

### 4.7 Time to finality — L2 vs L1 (results-v2, sessions v2-1, v2-2)
Source: `experiments/analysis/finality_summary.csv`, `finality_tests.csv`; raw `fin_*.csv` (observer
`experiments/finality.py`); commits 985c64e (Base), 615d34d (L1). WiFi, controlled, 2026-10-15, both chains
~25 min apart. Measured in ms **after t4** (the receipt): the time until the provider's `safe` / `finalized`
block tag first reaches the settlement's block. Base: n = 60 (both v2-1 arms), watched by two observers on
the same transactions; L1: n = 30. All 150 observations `finalized`; 0 timeouts, 0 reorgs.

| mean ± 95% CI (n), ms after t4 | `safe` | **`finalized`** | median finalized |
|---|---|---|---|
| **L1 Ethereum Sepolia** (publicnode observer) | 589,985 ± 42,162 (30) | **972,890 ± 42,128 (30)** | 977,119 |
| **L2 Base Sepolia**, publicnode observer | 121,945 ± 7,672 (60) | **1,048,889 ± 28,373 (60)** | 1,066,670 |
| L2 Base Sepolia, Alchemy observer | 76,275 ± 7,650 (60) | 1,309,808 ± 29,060 (60) | 1,311,835 |

Sensor event → finalized (`finality_ms`): L1 980,427 ± 42,204 (30); Base 1,049,731 ± 28,371 (60, publicnode).

Findings:
1. **L1 finalizes in ~16.2 min** (two epochs ≈ 12.8 min plus slot position and the epoch boundary).
2. **Base finalizes *later* than L1**: +76.0 s with the publicnode observer (p = 3.6e-03), +336.9 s with
   Alchemy (p = 2.3e-19). Expected from the rollup design — a Base block is final only once its batch is
   posted to L1 and that L1 block finalizes — and **the reverse of the latency ranking**: L2 is ~1 s to
   pre-confirmation and ~2.3 s to a sealed block, but ~1–5 min slower than L1 to finality.
3. **`safe` is the other way round**: Base 1.3–2 min vs L1 ~9.8 min (p ≈ 1e-20 per observer pair).
4. **The provider shifts the finality reading by ~4.4 min** (Alchemy − publicnode, finalized +261 s; safe
   −46 s). A node can only lag the chain's true finality, never lead it, so the earlier report is the closer
   bound: **Base time to finality ≤ ~17.5 min (mean)**. A gateway that waits for finality on a single
   provider inherits that provider's lag.
5. For the thesis: **micropayment UX runs on pre-confirmation or sealed-block latency; finality is the
   settlement-risk horizon**, and on this L2 it is not shorter than on L1. Report both, do not merge them.

## 5. Results — security (H2)

### 5.1 The gateway cannot move funds alone

Foundry suite: **55 tests, 5 suites, all passing.** The seven required abuse cases all revert:

| # | Abuse case | Test | Revert reason |
|---|---|---|---|
| 1 | Gateway-only signature | `GatewayAbuse::test_gatewayOnlySignatureReverts` | `bad device sig` |
| 2 | Forged device signature | `GatewayAbuse::test_forgedDeviceSignatureReverts` | `bad device sig` |
| 3 | Replayed voucher | `Replay::test_replayingTheSameVoucherReverts` | `not monotonic` |
| 4 | Non-monotonic amount | `GatewayAbuse::test_nonMonotonicAmountReverts` | `not monotonic` |
| 5 | Expired deadline | `GatewayAbuse::test_expiredDeadlineReverts` | `expired` |
| 6 | Voucher from another channel | `Replay::test_voucherFromAnotherChannelReverts` | `bad device sig` |
| 7 | Voucher from another chainId | `Replay::test_voucherSignedForAnotherChainIdReverts` | `bad device sig` |

Cases 6–7 each have a positive control (same voucher valid on its own channel / chain), so the reverts
are not explained by the contract rejecting everything. `close()` has 13 further tests (forged device
sig, wrong merchant, double close, settle-after-close, escrow isolation between channels).

**Live attack on Base Sepolia** (`scripts/h2_live.py`), the gateway as adversary:
- Through the honest gateway API: 3/3 refused before any transaction (empty device sig, gateway's own
  sig in the device slot, 65 junk bytes).
- **Gateway attacking the contract directly**, bypassing its own off-chain checks and overriding gas
  estimation: transaction `0x41b2dd32…3bffa` **mined and reverted on-chain** (`status 0`,
  `bad device sig`, block 47070886, 39,388 gas burnt). Escrow and merchant balances unchanged.
- **4/4 live attacks blocked. The boundary is enforced by the chain, not by the gateway's honesty.**

### 5.2 Throughput — what the 3-of-3 costs (vs a single-signature baseline)
Reference: `SingleSigChannel` — the same contract with the device signature as the only check (a classic
unidirectional channel), test-only, never deployed. Source: `throughput.json`, `throughput_onchain.csv`,
`throughput_gateway.csv`; commit e831b13.

**On chain** (eth_simulateV1 on mainnet state, read-only; validated: simulated 3-of-3 settle 77,217 vs
77,290 real on Base Sepolia, 88,333 vs 88,365 on Ethereum Sepolia; prices of 2026-10-14):

| per later `settle()` | single-sig | 3-of-3 | difference |
|---|---|---|---|
| gas | 62,929 | 77,233 | **+14,304 (+22.7%)** |
| calldata | 324 B | 644 B | +320 B |
| Base L1 data fee | 1.95e9 wei | 2.92e9 wei | +50% |
| cost, Ethereum mainnet | €0.016316 | €0.020025 | +22.7% |
| cost, Base mainnet | €0.000846 | €0.001039 | +22.9% |
| settles per block, Ethereum (60M gas) / Base (400M gas) | 953 / 6,356 | 776 / 5,179 | **−18.5%** |

Settles-per-block is a ceiling (a whole block for this one operation); report it as the ratio.

**Gateway, per voucher** (laptop, one core, N = 1000): verify device sig 6.36 ms, verify merchant sig
6.43 ms, co-sign 5.22 ms, encode 0.99 ms → **19.00 ± 0.15 ms (3-of-3) vs 7.07 ± 0.07 ms (single-sig):
53 vs 141 vouchers/s per core (2.7×).** Python crypto; the ratio is the finding, not the absolute rate.

**Live pipeline, back to back** (Pi → gateway → Base Sepolia, WiFi, controlled, N = 30, no gap):
**30/30 ok, 0.50 settled payments/s — exactly one settlement in each of 30 consecutive L2 blocks.**
The prototype gateway answers only once the block is sealed, and the device waits for the answer,
so the pipeline is bound to one settlement per 2 s block. That is the prototype gateway's sequential
design, not the 3-of-3. Per-payment latency under back-to-back load: 924.3 ± 41.2 ms vs 966.5 spaced
(p = 0.18) — unchanged.

**Reading for H2:** the 3-of-3 costs a constant factor — ~23% more gas, ~19% fewer settlements per
block, 2.7× the gateway's per-voucher CPU — and none of these is the binding limit. With the channel,
one settlement carries up to 200 ticks, so micropayment throughput is set by the off-chain voucher
rate (Pi signs ~74/s at 13.5 ms; gateway verifies 53/s per core). Concurrent devices and a pipelining
gateway: §5.3.

### 5.3 Concurrent load — K devices, pipelining gateway (results-v2, sessions v2-3, v2-4)
Source: `experiments/analysis/concurrency.csv` (one row per session); raw `cc_base-sepolia_k*_*.csv`;
driver `experiments/concurrency.py`. Base Sepolia, WiFi, controlled, no gap between a device's runs.
K device processes **on the Pi** (sharing its 4 cores and WiFi link), each with its own key and its own
3-of-3 channel (device_i, load gateway, merchant). The gateway **pipelines**: it takes nonces locally and
broadcasts the next settlement without waiting for the previous receipt. Separate gateway key, so the
results-v1 nonce sequence is untouched. K = 1–4 and the first K = 8: 2026-10-15, commit c60e32f, Alchemy
RPC; K = 8 rerun: 2026-10-16, commit 548892d, publicnode RPC.

| K | RPC | ok / attempted | settled/s | settles per block (max) | latency t4 − t0, ms | to sealed block, ms |
|---|---|---|---|---|---|---|
| 1 | Alchemy | 30 / 30 | 0.51 | 1.00 (1) | 677.3 ± 43.1 (30) | 1966.9 ± 67.5 (30) |
| 2 | Alchemy | 30 / 30 | 1.02 | 2.00 (2) | 671.9 ± 42.7 (30) | 1988.2 ± 75.7 (30) |
| 4 | Alchemy | 40 / 40 | 2.08 | 4.00 (4) | 732.1 ± 54.0 (40) | 1983.4 ± 48.9 (40) |
| 8 | Alchemy (free tier) | 51 / 80 | 3.86 | 7.29 (8) | 872.8 ± 71.7 (51) | 2024.1 ± 78.9 (51) |
| **8** | **publicnode** | **64 / 64** | **4.11** | **8.00 (8)** | 941.0 ± 76.6 (64) | 1995.4 ± 63.5 (64) |

Latency/sealed over ok rows; settled/s and settles per block are one value per session (no CI).

Findings:
1. **Throughput scales linearly with the number of devices: exactly K settlements per 2 s block** for
   K = 1, 2, 4, 8 (0 failures, 0 nonce resyncs on the clean runs). The one-per-block limit in §5.2 was the
   prototype's sequential gateway, as stated there; the 3-of-3 contract does not impose it.
2. **Per-payment latency rises modestly with load**: ~0.67 s at K = 1–2 to ~0.87–0.94 s at K = 8, still
   below 1 s to pre-confirmation; time to a sealed block stays ~2.0 s at every K.
3. **The first K = 8 session failed 29/80 on the RPC provider's rate limit**, not on the chain or the
   contract: 28 HTTP 429 "Too Many Requests" from Alchemy's free tier, plus one consequence of a gateway
   bug (below). The rerun on publicnode with the same devices, channels and contract: **64/64**. Latency
   difference between the two K = 8 sessions +68 ms, p = 0.20; sealed −29 ms, p = 0.57 — no contradiction.
   **Practical point: at ~4 settlements/s a free-tier RPC plan, not the L2, is the first limit to hit.**
4. **Gateway bug found and fixed under load:** in the pipelining path a receipt-*lookup* error (a 429)
   released the transaction's nonce although the transaction had been broadcast — and in fact settled
   on-chain (tx `0x96e030ae…7188`). The re-used nonce then failed `nonce_too_low`. No funds lost, both rows
   recorded; that session has 52 settlements on-chain, 51 recorded `ok`. Fixed in 548892d (a broadcast
   transaction's nonce is consumed whatever happens to the receipt poll), with a regression test; the
   K = 8 rerun ran on the fix. Worth a paragraph in the implementation chapter: nonce management is the
   hard part of a pipelining relayer.
5. K = 1 latency here (677 ms, Alchemy) is below §4.6's Alchemy figure (782 ms): the pipelining gateway
   takes nonces locally and skips one RPC call per settlement. Different gateway code path, not a
   contradiction.

---

## 6. Results — cost (H1)

### 6.1 Channel amortisation (M4, Base Sepolia, real transactions)
200 charging ticks signed on the Pi, **0 on-chain transactions**, then one `close()`:

| | gas per micropayment |
|---|---|
| per-tick `settle()` (steady state) | 75,029 |
| channel: `close()` / 200 | **718.0** |
| **reduction (marginal)** | **104.5× (2.02 orders of magnitude)** |
| lifecycle: (open + 200 settles)/200 vs (open + close)/200 | 75,864.3 vs 1,553.3 → 48.8× |

The 48.8× is a floor set by the 200-tick run length (open amortises further on longer channels).
Off-chain cost moved to the device: mean 13.2 ms signing per tick (2,639 ms for 200).

### 6.2 Mainnet-priced cost table (reference date 2026-10-14)
Source: `experiments/analysis/costs_2026-10-14.csv` / `.json`; method in `experiments/costs.py`.
- Gas units from read-only `eth_simulateV1` of the full contract lifecycle on each mainnet;
  **method validated by reproducing 10 real testnet transactions within 61 gas.**
- Gas price: 24 h before the run, per block base fee + median tip. ETH = 2,228.86 EUR (CoinGecko).

| | Ethereum L1 | Base L2 |
|---|---|---|
| gas price, median [p10–p90] | 0.1163 gwei [0.0670–0.8216] | 0.0060 gwei [0.0060–0.0065] |
| **settle, per payment** (77,229 / 77,217 gas) | **€0.0200** [0.0115–0.1414] | **€0.00104** [0.00104–0.00115] |
| open, later channel (149,967 gas) | €0.0389 | €0.00201 |
| close, only settlement (≈143,570 gas) | €0.0372 | €0.00193 |
| deploy, one-off (2,037,870 gas) | €0.528 | €0.0273 |
| **per tick in a 200-tick channel** (close/200, 717.8 gas) | €0.000186 | **€0.0000097** |

Findings:
1. **L2 ≈ 19× cheaper per settlement at the median price, ≈ 120× at the day's p90** — Ethereum's fee
   is volatile, Base's is not. Ethereum gas was cheap on this date; the ratio moves with L1 congestion.
2. **The channel matters more than the chain**: ~108× per payment on either chain. Channel on L2 vs
   per-tick settle on L1: **€0.0000097 vs €0.0200, ~2,000×.**
3. Gas units are identical on both mainnets for this contract — **the L1/L2 cost gap is entirely gas
   price**.
4. **Base's L1 data fee is ~0.6% of its cost** (2.8e9 wei of ~4.7e11 per settle) at 2026 blob prices —
   a common pre-EIP-4844 concern that no longer dominates; still measured, as the term that could move.

### 6.3 Why testnet costs are not used
Sepolia gas ≈ 0.001 gwei, Base Sepolia ≈ 0.006 gwei — on testnets L1 looked *cheaper* than L2.
Additionally, **Ethereum Sepolia runs the Glamsterdam upgrade (since 2026-10-06), including EIP-8037
State Creation Gas Cost Increase**, which mainnet did not have on the reference date. Measured effect
on the same build: deploy 14,375,662 vs 2,037,870 gas (7×); new-account surcharge 183,588 vs 24,976
(spec: 183,600 vs 25,000); recurring settle 88,365 vs 77,290 (+14%). EIP-8037 was in *Last Call*
(deadline 2026-11-01) when checked. **The cost table therefore describes pre-Glamsterdam mainnet**;
Glamsterdam on mainnet would raise L1 costs and widen the gap (known direction).

---

## 7. Implementation notes worth a paragraph each
- **Single-clock latency** (§1.4) — the core methodological contribution; cross-machine latency
  without NTP trust.
- **Gateway holds the request until inclusion** — lets the device observe settlement directly.
- **Merchant countersignature co-located with the device** (car plugged into the station) — avoids a
  second round trip; a stand-in for a separate merchant process.
- **Device key generated on the Pi**, encrypted keystore, never leaves it; Pi holds no ETH and sends no
  transactions (verified: balance 0, tx count 0).
- **Upgradeability lesson:** adding `close()` required a new deployment; the earlier contract (no exit
  path) permanently locked ~0.000288 testnet ETH. An escrow without an exit strands funds when its
  logic must change.
- **Channel IDs omit contract address and chainId** (`keccak256(device, gateway, merchant, index)`), so
  identical participants produce identical IDs across deployments — not exploitable (EIP-712 binds
  `verifyingContract`, tested), but datasets must key on (chainId, contract, channel).

---

## 8. Limitations / threats to validity (complete list so far)
1. **Testnets only**; costs extrapolated to mainnet prices on one date (§6.2), pre-Glamsterdam.
2. **Public RPC endpoints** (publicnode / mainnet.base.org) dominate L2 latency (~70%). A dedicated
   endpoint was tested (§4.6, −119 ms) — but **one provider (Alchemy), free tier, one session**; other
   providers and paid tiers may differ.
3. **One device, one gateway, one LAN, one router**, one session per condition per day; N = 30.
4. **Cooperative close only** — no unilateral close / challenge period; a counterparty who stops
   signing strands the deposit.
5. **Funder = gateway key** on the public testnets (faucet constraint); roles were distinct on anvil.
   Nothing H1/H2 measures depends on it.
6. **Merchant countersigns on the Pi**, not in its own process/device.
7. **M4 used a 100× scaled-down price** (budget); gas is independent of amount transferred, so no
   measured figure changes.
8. **Pi configuration matters** (power save, governor) — both reported; the "controlled" config is a
   tuned setup, the "default" one is stock.
9. **Network term ~2–5 ms higher in L1 sessions** than L2 controlled sessions on the same links (same
   config, ~30 min later) — ambient load; negligible vs 8 s.
10. **Clock**: no reported number crosses machines; per-session mean offset diagnostic −63 to +7.3 ms
    (the −63 ms is the power-save artefact in default WiFi; all other 2026-10-14 sessions +2.1 to
    +7.3 ms). The 2026-10-15 session shows +487.6 ms: the laptop's Windows clock had drifted 0.48 s
    from NTP (Pi chrony in sync). Diagnostic only — no reported figure uses it.
11. **Simulated sensor** (kWh meter), not a real charger.
12. **Bluetooth not yet measured; energy only as a CPU-time proxy** (§4.4, §9) — no power meter.
13. **L2 latency depends on the receipt definition** (§4.5): Base receipts are Flashblocks
    pre-confirmations; both pre-confirmation and sealed-block latency are reported.
14. **Throughput**: the M5/H2 gateway settles sequentially (one per L2 block, §5.2). Concurrent load
    (§5.3) used a separate pipelining gateway, K ≤ 8 device processes on **one** Pi, one gateway, one
    session per level, and two RPC providers across the levels (K = 1–4 Alchemy, K = 8 both). Beyond
    K = 8 not tested; the ceiling was not reached.
15. **Finality is measured through provider block tags** (`safe`, `finalized`), polled every 2 s (the
    resolution), not from the L1 batch-inbox/derivation directly. The reading depends on the provider
    (~4.4 min apart on Base, §4.7); reported as an upper bound per observer. One session per chain,
    one day, testnets (Sepolia's finality and Base Sepolia's batch cadence need not match mainnet).
16. **RPC rate limits**: the free-tier dedicated endpoint returned HTTP 429 at ~4 settlements/s
    (§5.3). Throughput figures are bounded by the RPC plan as well as the chain.
17. **Results-v2 sessions are not pooled with results-v1**: they ran on later commits and different
    days; same-day controls were used where a comparison was made (§4.6).

---

## 9. Pending — placeholders, do not fill from literature
- **Bluetooth (BLE) transport** — blocked on a USB Bluetooth adapter for the gateway laptop. Planned:
  Pi as BLE GATT peripheral, gateway as central (bleak). Will add L2/L1 × BLE rows to §4.1.
- **M7 energy in joules** — no USB power meter. The CPU-time fallback is measured (§4.4) and must be
  stated as a limitation. Still unmeasured: actual energy, and the energy cost of disabling WiFi
  power save (§4.3).
- **H3** regulatory analysis (MiCA / PSD2) — author.

---

## 10. Files to give the writer (all in the handoff folder)
| File | What it is |
|---|---|
| `thesis-brief.md` | this document |
| `CLAUDE.md` | original prototype spec: hypotheses, layers, milestones |
| `measurements.md` | chronological lab log — every run, commit, decision; the evidence behind every claim |
| `summary.csv` | per-condition statistics (n, mean, sd, ci95, median, min, max per metric) |
| `tests.csv` | Welch t-tests (transport and chain comparisons) |
| `costs_2026-10-14.csv` / `.json` | mainnet-priced cost table and its inputs |
| `fig_latency.pdf/.png` | latency per run, mean ± 95% CI, panels per chain |
| `fig_decomposition.pdf/.png` | mean latency by term, per condition |
| `fig_device_terms.pdf/.png` | signing and network per run (shows governor and power-save effects) |
| `throughput.json`, `throughput_onchain.csv`, `throughput_gateway.csv` | H2 throughput results (§5.2) |
| `rpc_summary.csv`, `rpc_tests.csv` | dedicated vs public RPC, per provider, Welch (§4.6) |
| `finality_summary.csv`, `finality_tests.csv` | time to `safe`/`finalized` per chain and observer, Welch L2 vs L1 (§4.7) |
| `concurrency.csv` | concurrent load, one row per session and K (§5.3) |
| `finality.py`, `concurrency.py` | finality observer and concurrent-load driver, for the method chapter |
| `PaymentChannel.sol`, `VoucherLib.sol` | the contract |
| `SingleSigChannel.sol` | test-only single-signature baseline for §5.2 (never deployed) |
| `voucher.py` | EIP-712 voucher definition |
| `http_client.py` | device transport incl. the single-clock latency logic (docstrings explain it) |
| `runner.py`, `run_batch.py`, `analyze.py`, `costs.py`, `throughput.py` | experiment harness, for the method chapter |
| `forge-test-output.txt` | `forge test -vv` output for the ch. 4 security appendix |
