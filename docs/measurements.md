
## M1 — local anvil, single run (sanity check, not a real H1 sample)
- Date: 2026-09-14
- Chain: anvil local, --block-time 2, chainId 31337
- Commit: cfa01d42e6f3b7cf55a6ce2855557d5942d0a34e
- Conditions: single device tick, loopback HTTP, single run (N=1 — not yet a real measurement)
- t4-t0 (reported): 168.7 ms
- gasUsed: 109,217
- Note: single run only, landed near a scheduled block boundary by chance.
  Real H1 numbers require N>=30 per condition per M5 — this row exists only
  to confirm the instrumentation and pipeline work end-to-end.

## M2 — device leg on the Raspberry Pi, Ethernet (single run, not a real H1 sample)
- Date: 2026-09-20
- Chain: anvil local, `--block-time 2`, chainId 31337
- Commit: bfd2a0f6484f8cc572d4bc29df6b95599e0f3259 (same commit on both machines)
- Conditions: N=1, transport **Ethernet** (Pi eth0 → laptop, 192.168.32.63 → 192.168.32.54,
  same /24, no intervening switch config), one device tick, `settle()` submitted live
- Topology: device agent on Raspberry Pi 4 (Debian, kernel 6.18.50, Python 3.13.5);
  gateway + anvil on the laptop (Windows 11, Python 3.11.9)
- Contract: `0x71C95911E9a5D330f4D621842EC243EE1343292e`
  channel `0x27B171c149E82DDC2c8a7568C0E742a7050Cd359`, deposit 1 ETH
- tx: `0x4dffdc49a6c9e6a758112c0a6e4511588aaa6def7d4d84a8aec8e7238d551474`, block 185,
  `Settled` nonce 1, cumulativeAmount 3,829,000,000,000 wei

### Latency
| Span | Clock | Value |
|---|---|---|
| signing `t1-t0` | Pi, monotonic | 14.8 ms |
| round trip `t4_dev - t_send` | Pi, monotonic | 1402.1 ms |
| **reported `t4_dev - t0`** | **Pi, monotonic** | **1430.2 ms** |
| build + broadcast `t3-t2` | gateway | 58.0 ms |
| inclusion `t4-t3` | gateway | 1336.3 ms |
| confirmation `t5-t4` | gateway | 1.5 ms |
| network + framing (both legs) | derived, skew-free | 7.8 ms |

### Cost
- gasUsed: 109,229 · effectiveGasPrice: 1,000,000,007 wei · l1Fee: n/a (anvil is not OP-stack)
- Gas paid by the gateway (`0x3C44…93BC`). Pi balance 0 wei, tx count 0 — the device has
  never sent a transaction, which is the invariant H1's cost argument rests on.

### Clock handling — changed at M2, read before comparing to the M1 row
t0 is the Pi's clock and t2–t5 are the laptop's, so the reported number deliberately no
longer crosses that boundary. The gateway holds the HTTP request open until it has the
receipt, so the reply landing on the Pi is the Pi's own observation of "included in a
block"; `t4_dev` is stamped there, on the same monotonic clock as `t0`. **Reported latency
is therefore one clock end to end and carries no skew term.** It over-measures the
gateway's own `t4` by the reply's trip back down the wire (~4 ms here, half of the 7.8 ms
figure), which is the direction that cannot flatter the result.

Gateway spans above are differences among gateway marks only. The network figure is
`rtt − (t4−t2)`: one span measured wholly on each machine, so the offset cancels
(tested against ±500 ms of injected skew in `device/test_marks.py`).

- **Measured clock offset (gateway − device): −4.5 ms**, NTP-style four-timestamp estimate
  from (t_send, t2, t6, t4_dev). Diagnostic only; nothing above is corrected by it.
- Deviation from CLAUDE.md: `chrony` is **not** running on either box. The Pi is synced by
  `systemd-timesyncd` (`NTPSynchronized=yes`), the laptop by w32time — different daemons,
  not a common pinned NTP source. The −4.5 ms offset is consistent with that. This does not
  invalidate the row, because no reported number is a cross-machine difference, but chrony
  against one source should be in place before the M5 runs so the offset column stays
  small and explainable.

### Reading this row
N=1 — instrumentation and topology proof, not an H1 measurement (M5 needs N≥30 per
condition). The total is dominated by `inclusion` (1336 of 1430 ms), which is anvil's
2-second block cadence: a tick arriving uniformly at random within a block period waits
~1 s on average, so ~1.4 s is the expected value here, not an anomaly. The M1 row's 168.7 ms
is the same pipeline landing near a block boundary by luck and the two are **not**
comparable as latency figures. The device-side terms are the ones that carry over: signing
14.8 ms on the Pi and 7.8 ms of Ethernet round trip are real, chain-independent costs.

## M3 — real L2: Base Sepolia, device leg on the Pi, Ethernet (N=1, not an H1 sample)
- Date: 2026-09-20
- Chain: **Base Sepolia (public testnet), chainId 84532**, RPC `https://sepolia.base.org`
- Commit: e57f5cfdc499444a19bb58de1cafc3d0e71a1112 (same commit on both machines)
- Conditions: N=1, transport Ethernet (Pi eth0 192.168.32.63 → laptop 192.168.32.54),
  one device tick, `settle()` submitted live by the gateway
- Contract: [`0xd0D2D1680c17F1F11d246A80fc1E034c3d067766`](https://sepolia.basescan.org/address/0xd0D2D1680c17F1F11d246A80fc1E034c3d067766)
  channel `0x0d8a46e92D7cbf246e1CEA9A04112DBD6570EC02`, deposit 0.0002 ETH

### The M3 "done when": a real `Settled` triggered by the Pi
| tick | tx | block | gasUsed |
|---|---|---|---|
| 1 | [`0x3519002e…19ad84f`](https://sepolia.basescan.org/tx/0x3519002ea34c42f7fbc50af06f1bbe67574545192b3d3685d7251c63319ad84f) | 47067802 | 134,217 |
| 2 (**reference row**) | [`0xdcdf66f4…62e7a92`](https://sepolia.basescan.org/tx/0xdcdf66f4a606611d40277f07288f03d2d6a5a12b44c01bbf04f34033c62e7a92) | 47068142 | 75,029 |

Blockscout mirror (API-readable, no Cloudflare): `https://base-sepolia.blockscout.com/tx/0xdcdf66f4a606611d40277f07288f03d2d6a5a12b44c01bbf04f34033c62e7a92`
— verified `status=success`, `to=0xd0D2…7766`, `gas_used=75029`, so the links are publicly indexed
and not merely present on our own RPC.

Final channel state: `paid` 7,923,020,000,000 wei, `nonce` 2, deposit 200,000,000,000,000 wei.
Pi `0x8E0EF096D41166B7E5585498E3b6dDe4C843FDA8`: **balance 0 wei, transaction count 0** — the
device signed both settlements and has never sent a transaction.

### Latency (tick 2)
| Span | Clock | Value |
|---|---|---|
| signing `t1-t0` | Pi, monotonic | 15.1 ms |
| **reported `t4-t0`** | **Pi, monotonic, skew-free** | **1337.9 ms** |
| gateway build + broadcast `t3-t2` | gateway | 1135.2 ms |
| L2 inclusion `t4-t3` | gateway | 167.8 ms |
| confirmation `t5-t4` | gateway | 1288.2 ms |
| network + framing, both legs | derived, skew-free | 6.3 ms |
| measured clock offset (gateway − device) | diagnostic | −9.0 ms |

**Inclusion on Base Sepolia was 167.8 ms** — the headline is dominated not by the chain but by
`t3-t2` = 1135 ms of *gateway↔public-RPC round trips* (nonce lookup, `eth_estimateGas`, fee
fields, `eth_sendRawTransaction`). That is a property of talking to a shared public endpoint over
the internet, not of the L2. A dedicated RPC (Alchemy/QuickNode key) should cut it sharply and
is the single highest-leverage change before M5.

The 1288 ms `t5-t4` confirmation wait is likewise mostly **RPC pool lag**, not block time:
`CONFIRMATIONS=1` targets the receipt's own block, so it should return instantly, but
`eth_blockNumber` was answered by a node that had not yet seen our block.

### Cost
| | tick 1 (first settle) | tick 2 (subsequent) |
|---|---|---|
| gasUsed | 134,217 | 75,029 |
| effectiveGasPrice | 6,000,000 wei | 6,000,000 wei |
| L2 execution fee | 805,302,000,000 wei | 450,174,000,000 wei |
| l1Fee (data availability) | 19,930,078,132 wei | 18,422,278,450 wei |
| L1 gas × price | 5,301 × 1,061,851,641 | 5,381 × 981,870,504 |
| **total** | **825,232,078,132 wei** | **468,596,278,450 wei** |
| L1 share of total | 2% | 4% |

**The first settlement on a channel costs ~1.8× a later one** (134,217 vs 75,029 gas): the first
write turns `paid` and `nonce` from zero to non-zero and funds the merchant account, all cold-slot
costs that later settlements do not repeat. The cost table in ch. 4 must not quote one figure for
"a settlement" — and M4's amortisation claim should be computed against the **75,029** steady-state
number, since 200 ticks settle as one *subsequent* transaction in a channel that is already open.

L1 share is only 2–4% here because the calldata is small (~5.3k L1 gas) and Base Sepolia's L1 gas
price was ~1 gwei. It is still recorded per run, because on a busier L1 it is the term that moves.

### Deviations to carry forward
- **Funder = gateway.** On anvil these were separate roles (vehicle owner's wallet vs onboard
  unit). One faucet drip has to cover both gas and the deposit, so `FUNDER_PRIVATE_KEY` is the
  gateway key here. Nothing H1 or H2 measures changes — the Pi still holds no ETH and sends no
  transaction — but the topology differs from the M2 row.
- **Anvil dev keys replaced.** The gateway and merchant were using anvil's well-known
  deterministic keys, which are swept by bots on public testnets. Fresh keys were generated for
  Base Sepolia; the anvil `.env` is preserved as `.env.anvil` so the M1/M2 local runs stay
  reproducible.
- **Merchant key was printed once** by `scripts/m2_setup.py` before that line was removed. It is
  testnet-only and holds ~0.0000079 ETH, but treat it as burned and rotate before any run whose
  output is published.
- **chrony still not running** on either box (Pi: systemd-timesyncd, laptop: w32time). Measured
  offset −9.0 ms, up from −4.5 ms at M2. Still invalidates nothing — no reported number is a
  cross-machine difference — but it is drifting, and should be fixed before M5.
- **Gateway balance remaining: 287,124,418,903,068 wei** (~0.000287 ETH) after deploy, open and
  two settlements. At ~469 Gwei-equivalent per steady-state settle that is ~600 more settlements,
  which is enough for M5's N≥30 per condition but not for a careless M4 loop.

### Reading this row
N=1. Instrumentation and topology proof on a real L2, not an H1 measurement. Tick 1's *latency*
numbers are not usable — they were taken before the confirmation-wait correction in bb61aea and
reported 3898.7 ms end-to-end with a nonsensical 2010 ms "network" term; its *cost* figures are
valid and are the first-settle column above. Tick 2 is the reference row.

## M3 cleanup — merchant key rotated, dedicated-ish RPC, chrony (2026-09-20)

Closing the three open items from the M3 row before M4. Not a new measurement
condition: same topology, same commit lineage, one tick run to verify the
changes took effect.

### 1. Merchant key rotated (burned key retired)
`scripts/m2_setup.py` printed the merchant private key to a terminal before that
line was removed, so the key is treated as compromised regardless of its value.

- burned: `0xE02981BB6C254CABFf6614c8d9Ea9D8F2dD91910`
- active: `0xc154F645c9f7308C5D5533Da97c97e615BA99C04`

**No redeploy was needed.** `PaymentChannel.open()` takes the merchant as an
argument and stores it per channel, so the signer is channel-scoped, not
contract-scoped — a new channel on the same contract carries the new merchant.
The contract at `0xd0D2D1680c17F1F11d246A80fc1E034c3d067766` is unchanged, which
also keeps the M3 explorer links valid.

| | old channel `0x0d8a46e9…70EC02` | new channel `0x387109a3…3beDbb` |
|---|---|---|
| merchant | burned key | rotated key |
| deposit | 200,000,000,000,000 wei | 100,000,000,000,000 wei |
| paid / nonce | 7,923,020,000,000 / 2 | 3,790,360,000,000 / 1 |
| status | retired, not used | **active** |

The burned key is kept in `.env` as `RETIRED_MERCHANT_PRIVATE_KEY_BURNED` rather
than deleted: it holds 7,923,020,000,000 wei of already-settled value, and it is
the record of which key countersigned the two M3 vouchers. It is not referenced
by any running component, and does not appear in any tracked file.

**192,076,980,000,000 wei is locked in the retired channel** (deposit minus paid).
Not lost — `close()` in M4 can refund it — but unavailable until then.

### 2. RPC: public pool → publicnode
The M3 row showed `t3-t2` = 1135 ms of gateway↔RPC round trips, not chain time.
Benchmarked the candidates on the four read calls the relayer makes before it can
broadcast, **with connection reuse** (web3's HTTPProvider keeps a `requests.Session`;
benchmarking with a fresh TLS handshake per call overstates cost ~2x and would have
picked a different winner):

| endpoint | median, 4 calls | per call |
|---|---|---|
| `sepolia.base.org` (was) | 679 ms | 169.8 ms |
| **`base-sepolia-rpc.publicnode.com`** (now) | **328 ms** | **82.0 ms** |
| `base-sepolia.drpc.org` | 1687 ms | 421.9 ms |

Effect on a real tick:

| span | M3 (`sepolia.base.org`) | after (`publicnode`) |
|---|---|---|
| gateway build + broadcast `t3-t2` | 1135.2 ms | **670.4 ms** |
| confirmation `t5-t4` | 1288.2 ms | **68.8 ms** |
| network + framing | 6.3 ms | 7.4 ms |
| signing `t1-t0` | 15.1 ms | 15.0 ms |

`t3-t2` fell 41%, and the confirmation wait collapsed 19x — that span was almost
entirely RPC-pool lag (`eth_blockNumber` answered by a node that had not yet seen
our block), which is exactly what the M3 row predicted.

**The reported `t4-t0` went 1337.9 → 1475.2 ms, which is not a regression.**
`t4-t3` (L2 inclusion) was 167.8 ms then and 769.2 ms now: the tick landed at a
different point in Base's 2 s block period. With N=1 that term is a uniform draw
over ~0–2 s and swamps a 465 ms improvement elsewhere. This is the same
block-cadence lottery flagged in the M1/M2 rows, and it is why only the
*component* spans are comparable at N=1.

**This is not yet a dedicated endpoint.** publicnode is still a shared pool, just
a faster one. A keyed Alchemy/Infura endpoint needs an account signup (captcha),
so it remains open; expect `t3-t2` to fall further when one is in place.

### 3. chrony
- **Pi:** chrony installed, active, enabled at boot, `systemd-timesyncd` yielded.
  Pinned via `/etc/chrony/conf.d/magis.conf` to `time.cloudflare.com` (anycast,
  reachable by name from both machines) with `0.europe.pool.ntp.org` as backup.
  Tracking: stratum 4, system time 0.68 ms slow, RMS offset 2.5 ms.
- **Laptop:** still `time.windows.com` — `w32tm /config` needs an elevated shell
  and this session is not elevated. Measured against the Pi's source anyway:
  `w32tm /stripchart /computer:time.cloudflare.com` gives −0.07, +0.41, −0.78,
  +1.60 ms over four samples, i.e. **already sub-2 ms from the Pi's reference**.

Application-level offset (gateway − device), from the paired (t_send,t2,t6,t4_dev)
estimator: **−4.5 ms (M2) → −9.0 ms (M3) → +2.7 ms (now)**. The drift is arrested.
Still a diagnostic only — no reported number is a cross-machine difference.

### Verification tick
`0x2c84e428ee8fb2e77aff8becad4b9a40d177e69b40d61dc364bcfbda4a37c731`, block
47069959, channel `0x387109a3…3beDbb`, nonce 1, gasUsed **134,229**.

That gas figure independently confirms the cold-slot finding in the M3 row: this
is a *first* settlement on a fresh channel and costs 134,229, against 134,217 for
the previous channel's first settlement and 75,029 for a subsequent one.

### Budget before M4
Gateway holds **185,540,217,548,292 wei** (~0.000186 ETH); a further ~288,286,620,000,000
wei sits locked across the two channels until `close()` lands.

**M4 as specified does not fit this budget.** 200 ticks at the current
`PRICE_WEI_PER_KWH` (140,000,000,000,000 wei/kWh, ~3.8e12 wei/tick) accumulate to
~7.6e14 wei, which exceeds both the gateway balance and any deposit it could fund.
Options, in order of preference: lower `PRICE_WEI_PER_KWH` for the M4 run (gas is
the reproducible figure; the EUR price is nominal anyway), shorten the tick, or
top up from the faucet. Needs a decision before M4 starts.

## M4 — the channel: 200 ticks, one on-chain settlement (Base Sepolia)
- Date: 2026-09-20
- Chain: Base Sepolia, chainId 84532, RPC `https://base-sepolia-rpc.publicnode.com`
- Commit: 70a41de (both machines)
- Contract (**redeployed** — `close()` and the `funder`/`closed` fields are new):
  [`0xb746530c7e38aB163dE7238f70FE1e91c5E14bF9`](https://sepolia.basescan.org/address/0xb746530c7e38aB163dE7238f70FE1e91c5E14bF9)
- Channel `0x8800F104092fAA7f5A0F0DF6aa47671Dad264685`, deposit 10,000,000,000,000 wei
- Sensor ticks originated on the Pi, as since M2. Meter seed 4, 50 kW, 2 s ticks.

### The result: 104.5x, 2.02 orders of magnitude
| | gas per micropayment |
|---|---|
| per-tick `settle()` (M3 steady state) | 75,029 |
| channel: `close()` / 200 | **718.0** |
| **reduction** | **104.5x (2.02 orders of magnitude)** |

**CLAUDE.md's M4 bar — "~2 orders of magnitude" — is met on the marginal figure.**

Including one-time channel setup, which is the fuller lifecycle question:

| | gas per micropayment |
|---|---|
| baseline: (open + 200 x settle) / 200 | 75,864.3 |
| channel: (open + close) / 200 | 1,553.3 |
| reduction | 48.8x (1.69 orders of magnitude) |

Both are reported because they answer different questions. The 104.5x is the
*marginal* cost of one more micropayment inside an open channel, which is what the
micropayment claim is actually about. The 48.8x amortises `open()` (167,067 gas)
over exactly 200 ticks; that term shrinks as a channel is used longer, so 48.8x is
a floor set by this run's length, not a property of the design. At 600 ticks the
same channel would show ~93x; the marginal figure does not move.

### Transactions
| step | tx | gasUsed |
|---|---|---|
| deploy (with `close()`) | [`0x4b41fc5d…7eac5e`](https://sepolia.basescan.org/tx/0x4b41fc5d9b09f867ff1ba0aa7d8a1b2d88ef8a8bb7aeea06cd01d771207eac5e) | 2,037,870 |
| `open()` | [`0x8bdeb59a…10d2ca6`](https://sepolia.basescan.org/tx/0x8bdeb59a938f36fce31221ee7d7c321714913ba2db05d9229654aa10c10d2ca6) | 167,067 |
| **`close()` — the only settlement** | [`0x7d4884a0…1a3e6dde`](https://sepolia.basescan.org/tx/0x7d4884a0d99aa7cbe67ee451b20347b451426ee6f1634cbc6b0b8ee81a3e6dde) | **143,590** |

`close()` emitted both `Settled(nonce=200, cumulative=7,776,822,200,000, delta=same)`
and `ChannelClosed(paidTotal=7,776,822,200,000, refunded=2,223,177,800,000)`.
Those sum to exactly the 10,000,000,000,000 wei deposit, and the contract's balance
for this channel is 0 — the escrow is conserved and fully drained.

`close()` (143,590) costs more than a subsequent `settle()` (75,029) and about the
same as a first settlement (134,229): it does the same cold-slot writes plus a second
value transfer for the refund. That is the number the amortisation divides, so the
comparison above is not flattered by using a cheaper transaction.

### Off-chain accumulation (the cost that moved, not vanished)
- 200 vouchers signed on the Pi, **0 on-chain transactions**
- total signing 2639.4 ms; mean 13.2 ms/tick, median 13.2 ms, slowest 14.8 ms
- wall time 2.65 s (not realtime-paced; see below)

"The micropayment became free" is only true on chain. Each tick still costs the Pi
~13.2 ms of CPU to sign — this is the term M7 must measure in joules, and the honest
claim is that per-micropayment cost moved off the chain and onto the device, where it
is ~5 orders of magnitude cheaper but not zero.

### Scaled price for this run — and why it changes nothing measured
`PRICE_WEI_PER_KWH` was set to **1,400,000,000,000 wei/kWh on the Pi for this run,
100x below the nominal 140,000,000,000,000** (0.35 EUR/kWh at 2500 EUR/ETH).

Purely a budget accommodation: 200 ticks at the nominal price accumulate ~7.8e14 wei
(~0.00078 ETH), more than the faucet-funded gateway holds, so no deposit it could fund
would cover the final voucher. At the scaled price the same 200 ticks accumulate
7,776,822,200,000 wei inside a 10,000,000,000,000 wei deposit.

**This does not touch what M4 measures.** Gas is a function of the calldata and the
storage writes, not of the amount transferred: `close()` costs 143,590 gas whether it
moves 7.8e12 or 7.8e14 wei. The gas column, the amortisation ratio and the 104.5x are
all identical at the nominal price. It is *not* a change to the protocol's economics
and the nominal price remains the default in `device/agent.py`.

### close() and H2
Closing is no weaker than spending: `close()` carries the same 3-of-3 signature
requirement as `settle()`, and the two share `_applyVoucher()` so a check can never
guard one and not the other. The refund goes to the recorded `funder`, never to
`msg.sender` — the gateway submits this transaction and pays its gas, and gains
nothing by doing so. Covered by 13 Foundry tests in `contracts/test/Close.t.sol`,
including a forged device signature, a wrong merchant signature, double-close,
settle-after-close, and closing one channel leaving another's escrow untouched.

**Stated limitation:** cooperative close only. There is no unilateral close with a
challenge window, so a counterparty who simply stops signing strands the funder's
remaining deposit. Production channels answer this with a challenge period during
which a higher-nonce voucher overrides a stale one. Out of scope for CLAUDE.md's M4
(open / accumulate / close) and documented in `PaymentChannel.sol` itself.

### Pacing
Ticks ran flat out (2.65 s) rather than paced at 2 s each (~6.7 min). `--realtime`
does the latter. Pacing changes neither the energy series (the meter integrates
`tick_seconds`, not wall time) nor any gas figure; it would only matter for a
wall-clock-duration claim, which M4 does not make.

### Budget after M4
Gateway holds **163,391,004,341,322 wei** (~0.000163 ETH). This run cost ~22.1e12 wei
of which 2,223,177,800,000 wei came back as the refund — the first time a channel has
returned anything, because `close()` did not exist before.

### Funds stranded in the earlier contract — cannot be recovered
`close()` required a contract change (`funder` and `closed` fields), so it is a **new
deployment**. The previous contract `0xd0D2D1680c17F1F11d246A80fc1E034c3d067766` has
no withdrawal path of any kind, and still holds **288,286,620,000,000 wei** (~0.000288
ETH) across its two channels. That is permanently locked: adding `close()` to a new
deployment cannot reach an old one, and the old contract has no `selfdestruct`, no
admin withdraw and no close.

This contradicts the expectation that implementing `close()` would recover it. It does
not, and no future milestone will — the ETH is gone for practical purposes. The lesson
for the thesis write-up is a real one about upgradeability: an escrow contract with no
exit path strands whatever is in it the moment its logic needs to change.

## M6 — security (H2): abuse suite + live attack (2026-09-20)
- Commit: (this commit)
- Foundry: 55 tests, 5 suites, 0 failed. `forge test` on commit below.
- Live: Base Sepolia, contract `0xb746530c7e38aB163dE7238f70FE1e91c5E14bF9`,
  channel `0x387109a3C241D0722F96BB9ac8eefDa1aB3beDbb`.

### CLAUDE.md M6 checklist — coverage map
Audit first: M4's 13 `close()` tests covered **none** of the seven required cases for
`settle()` — they exercise the analogous cases on the exit path only, and
`test_closingOneChannelLeavesAnotherIntact` is escrow isolation, not signature replay.
All seven were written fresh.

| # | Required case | Test | Reverts with |
|---|---|---|---|
| 1 | Gateway-only signature | `GatewayAbuse::test_gatewayOnlySignatureReverts` | `bad device sig` |
| 2 | Forged device signature | `GatewayAbuse::test_forgedDeviceSignatureReverts` | `bad device sig` |
| 3 | Replayed voucher | `Replay::test_replayingTheSameVoucherReverts` | `not monotonic` |
| 4 | Non-monotonic amount | `GatewayAbuse::test_nonMonotonicAmountReverts` | `not monotonic` |
| 5 | Expired deadline | `GatewayAbuse::test_expiredDeadlineReverts` | `expired` |
| 6 | Voucher from another channel | `Replay::test_voucherFromAnotherChannelReverts` | `bad device sig` |
| 7 | Voucher from another chainId | `Replay::test_voucherSignedForAnotherChainIdReverts` | `bad device sig` |

Cases 6 and 7 are the EIP-712 domain doing its job: `channel` sits inside the signed
struct and `chainId`/`verifyingContract` inside the domain separator, so re-aiming a
voucher invalidates every signature over it. Both have a matching **positive control**
(`test_theSameVoucherIsValidForItsOwnChannel`, `test_theSameVoucherUnderThisChainIdSettles`)
so the reverts cannot be explained by the contract simply rejecting everything.

### Live attack (CLAUDE.md: "a live attempt from the running gateway")
`scripts/h2_live.py`. Two halves, because they answer different questions.

**Through the live gateway API** — what an honest gateway does. 3/3 refused, no
transaction: empty device signature (HTTP 422, `signature must be 65 bytes, got 0`),
gateway's own signature in the device slot (422, `bad device sig: signer is not the
channel's device`), 65 junk bytes (422, `signature is not recoverable`).

This proves little on its own and is reported as such: in the H2 threat model the
gateway *is* the adversary, and an adversary deletes its own checks.

**The gateway attacking the contract** — the question that matters. The script holds
the gateway key and nothing more, builds calldata through the gateway's production
path (`Relayer.build_settle_tx`), forges the device slot with its own key, and steps
over every off-chain guard including `estimate_gas`:

```
estimate_gas refused: execution reverted: bad device sig
  -> overriding the gas limit and submitting anyway.
forced tx      0x41b2dd32bee1ee17348f59f88ccb2d50ca7cb1b1ee1aceb21dead94af493bffa
mined in block 47070886, status 0, gasUsed 39,388
```

Mined and **reverted on-chain**: `status = 0`, revert reason `bad device sig`. Escrow
100,000,000,000 wei before and after; merchant balance 11,567,182,200,000 wei before
and after; channel `paid` still 0. The attack cost the gateway 236,328,000,000 wei in
burnt gas and bought nothing.

**4/4 live attacks blocked.** This is the H2 evidence: the boundary is enforced by the
chain, not by the gateway's good manners.

### Incidental finding: channel IDs are not unique across deployments
`open()` derives the id as `keccak256(device, gateway, merchant, _channelIndex)` — no
contract address, no chainId. Two deployments with the same participant triple
therefore mint **identical channel ids**. Observed live: channel
`0x387109a3C241D0722F96BB9ac8eefDa1aB3beDbb` exists on *both* the M3 contract
(`0xd0D2D168…`, deposit 100,000,000,000,000 wei, paid 3,790,360,000,000) and the M4
contract (`0xb746530c…`, deposit 100,000,000,000 wei, paid 0).

Not a vulnerability — storage is per-contract and the EIP-712 domain binds
`verifyingContract`, which `Replay::test_voucherSignedForAnotherDeploymentReverts`
demonstrates. But it has a direct consequence for M5: **the unique key for a run is
(contract, channel), not channel alone.** Keying the CSV on channel would silently
merge rows from two deployments.

### Full `forge test` output for ch. 4
See the table below; regenerate with `forge test -vv` from `contracts/`.

## M5 setup — Pi WiFi link brought up (2026-10-14)
Not a measurement. Records the network configuration the M5 WiFi condition runs on,
because it is part of the experimental setup and was changed to make the condition
possible at all.

### What was wrong
The Pi's `wlan0` (`D8:3A:DD:1A:23:26`, Broadcom, brcmfmac) could not join the lab
router's network `5G_Gateway_9382C2` on either radio. Every attempt ended in
`Authentication with <bssid> timed out` — the AP never answered the 802.11
Authentication frame. No `ASSOC-REJECT`, no status code, no 4-way-handshake failure.

### Diagnosis, in order
1. **MAC ACL suspected, ruled out.** The router (ZTE 5G CPE) was factory-reset, which
   clears any access control list. The failure reproduced unchanged afterwards. The
   earlier hypothesis that a MAC filter was responsible was wrong.
2. **Pi radio ruled out.** The same `wlan0` joined a phone hotspot (WPA2-PSK, 2.4 GHz)
   on the first attempt: associated, 4-way handshake complete, DHCP lease.
3. **Cause: the router's WPA2/WPA3 transition mode.** Both BSSIDs advertised
   `WPA2 WPA3` with RSN AKMs `psk sae`. Setting the router to **WPA2-PSK (AES), PMF off**
   fixed it immediately; the Pi associated to the 5 GHz BSSID on the next attempt.
   The Pi's profile was already pinned to `wpa-psk`/`rsn`/`ccmp`, so the incompatibility
   was in how the AP handled a WPA2-only client in transition mode, not in the client.

### Configuration as of this entry
| | |
|---|---|
| Router | ZTE 5G CPE, LAN `192.168.32.1`, factory-reset 2026-10-14 |
| Router security | **WPA2-PSK, AES, PMF off** (both radios) — changed from WPA2/WPA3 default |
| SSID | `5G_Gateway_9382C2` — 2.4 GHz BSSID `B8:D4:BC:93:82:C2` (ch 11), 5 GHz BSSID `B8:D4:BC:95:82:C2` (ch 36) |
| Pi WiFi profile | `netplan-wlan0-5G_Gateway_9382C2`: `wpa-psk`, `rsn`, `ccmp`, PMF disabled, **band locked to 5 GHz (`a`)**, permanent MAC |
| Pi `eth0` | `192.168.32.63`, route metric 100 |
| Pi `wlan0` | `192.168.32.64`, route metric 600 |
| Laptop (gateway) | `192.168.32.54`, **wired Ethernet** — so the WiFi condition varies only the Pi's hop |

5 GHz was chosen over 2.4 GHz deliberately: 2.4 GHz channels 1/3/6/9/11 all carry
neighbouring networks (`Cgates-*`, `RUT_6A74_2G`, `Internetelis`, `TP-LINK1`, …), while
5 GHz ch 36 is nearly clear. Pinning the band stops the Pi roaming between radios
mid-run, which would add a second, unrecorded variable to the condition.

Link sanity, 5 pings Pi → router: `eth0` avg 0.74 ms, `wlan0` avg 3.65 ms (max 7.9 ms),
0% loss on both.

### Consequence for the runner
With both links up the kernel routes everything over `eth0` (metric 100 < 600), so a
"WiFi" run with the cable still connected would silently be an Ethernet run. Each
condition must take the other interface down, and each CSV row must record the
interface the traffic actually left by, not the one the condition intended.

## M5 — Base Sepolia (L2), Ethernet and WiFi, N=30 each (2026-10-14)
- Chain: Base Sepolia, chainId 84532, RPC `https://base-sepolia-rpc.publicnode.com`
- Commit: **02b8bb5** on both machines (recorded per row as `commit` / `piCommit`)
- Contract `0xb746530c7e38aB163dE7238f70FE1e91c5E14bF9`, channel
  `0x69af732caC97151F7bE06C2DeF0E84F222D420D2` (opened for M5:
  [`0xd45879cc…6a36a`](https://sepolia.basescan.org/tx/0xd45879cca24a9aae690803e60209aa6157137085c7cf74945c8899206296a36a),
  149,967 gas, deposit 0.00001 ETH). Nonces 1–6 were smoke runs, not in the dataset.
- Runner: `experiments/runner.py`, gap uniform 2–4 s, other interface taken down per condition,
  route checked before every tick. Network setup as in the "M5 setup" entry above.
- Data: `experiments/results/m5_base-sepolia_eth_20261014T145732Z-251679.csv`,
  `experiments/results/m5_base-sepolia_wifi_20261014T150030Z-880a87.csv`
- **Pi config, as-is (defaults):** WiFi power save **on** (NM `powersave=default`, brcmfmac default);
  CPU governor **`ondemand`**, 600–1800 MHz. Both shape the numbers below — see "Reading this row".

### Results (60/60 ok, 0 failed, 0 reverted)
| | Ethernet mean ± 95% CI (median) | WiFi mean ± 95% CI (median) |
|---|---|---|
| **reported `t4-t0`** | **1041.9 ± 66.0 ms** (1005.6) | **1173.2 ± 100.3 ms** (1102.8) |
| signing `t1-t0` | 34.1 ± 3.5 ms (38.9) | 30.8 ± 4.4 ms (38.6) |
| network, both legs | 7.4 ± 0.7 ms (7.3) | 145.7 ± 67.6 ms (17.8) |
| gateway build + broadcast `t3-t2` | 735.9 ± 45.8 ms | 741.4 ± 37.5 ms |
| L2 inclusion `t4-t3` | 233.9 ± 47.1 ms | 225.5 ± 57.4 ms |
| confirmation `t5-t4` (not in reported) | 1572.0 ± 184.9 ms | 1428.4 ± 199.7 ms |
| clock offset (diagnostic) | +2.1 ms | −63.0 ms (median +0.4) |
| gasUsed | 77,278 | 77,290 |
| total fee (L2 exec + l1Fee) | 4.66e11 wei | 4.64e11 wei |

Welch t-test, latency eth vs wifi: t = −2.24, p = 0.030. Network term: t = −4.19, p = 0.0002.

### Reading this row
- **H1 latency: ~1.0–1.2 s end-to-end on L2**, "low seconds" as hypothesised. The biggest
  component is not the chain: `t3-t2` (gateway ↔ public RPC round trips) is ~740 ms, inclusion
  only ~230 ms. A keyed RPC endpoint remains the highest-leverage improvement (open since M3).
- **WiFi network term is bimodal.** 19/30 runs at 10–18 ms; 11/30 at 187–518 ms. In each slow run
  the offset estimate ≈ −½ × network, i.e. the whole excess is on the gateway→Pi leg: the AP
  buffering the reply for a dozing radio. This is WiFi power save, not link quality. The eth/wifi
  latency difference (~130 ms) is almost entirely this effect.
- **Signing ~39 ms median, vs 13.2 ms in M4.** M4 signed back-to-back with the CPU already
  clocked up; here 2–4 s idle gaps let `ondemand` drop to 600 MHz first. The same 13 ms appears
  in runs that caught a high clock. Not a regression — a governor effect, and an M7 confound.
- **l1Fee ≈ 100–3000 wei**, vs ~1.8e10 wei at M3. Base Sepolia's L1 data cost has collapsed
  since September; the L1 share of the fee is now ~0%. Recorded as observed, stated in ch. 4.
- **gasUsed ≈ 77,280** per settle on this channel, vs 75,029 at M3. Small, stable (sd ~10);
  to be stated, not silently compared.
- **`t5-t4` ≈ 1.5 s again** with `CONFIRMATIONS=1` (was 69 ms after the M3 RPC switch):
  publicnode's pool lag is back. Excluded from the reported number by construction.

### Budget after
Gateway holds **120,341,615,040,646 wei** (~0.000120 ETH) after 66 settles, one open and the 0.00001 ETH deposit.

## M5 controlled config — WiFi power save off (2026-10-14)
`netplan-wlan0-5G_Gateway_9382C2`: `802-11-wireless.powersave` `default` → **`2` (disable)**.
Verified with laptop→Pi pings over `wlan0`, 3 s apart (the idle pattern the runner produces):

| powersave | 10 pings, ms |
|---|---|
| default (on) | 16, 7, 5, 6, 5, **447, 507, 505**, 7, **479** |
| disable | 5, 6, 6, 5, 7, 7, 5, 6, 6, 5 |

The 450–510 ms tail on 4/10 pings matches the slow-run cluster in the first M5 WiFi session, so
the attribution there is confirmed. From this commit the runner records `wifiPowersave` and
`cpuGovernor` in every row.

## M5 — Base Sepolia, controlled Pi config, Ethernet and WiFi, N=30 each (2026-10-14)
- Commit **23ca4a2** both machines. Same chain, contract, channel and runner settings as the
  default-config pair above (channel nonces 67–126).
- Pi config: WiFi powersave **disable**, CPU governor **performance** (all 4 cores, 1.8 GHz).
  The governor was set by hand (`sysfs`, root) and does **not** survive a reboot; each row's
  `cpuGovernor` column records what was actually in force.
- Data: `m5_base-sepolia_eth_20261014T160935Z-dbbbcc.csv`, `m5_base-sepolia_wifi_20261014T161224Z-0ef90b.csv`
- 60/60 ok.

### Default vs controlled (mean ± 95% CI)
| | eth default | eth controlled | wifi default | wifi controlled |
|---|---|---|---|---|
| **reported `t4-t0`** | 1041.9 ± 66.0 | **957.5 ± 54.5** | 1173.2 ± 100.3 | **984.6 ± 67.0** |
| signing `t1-t0` | 34.1 ± 3.5 | **13.6 ± 0.1** | 30.8 ± 4.3 | **13.5 ± 0.1** |
| network, both legs | 7.4 ± 0.7 | 6.1 ± 0.4 | 145.7 ± 67.5 | **13.5 ± 0.7** |
| runs with network > 100 ms | 0 | 0 | 11 | **0** |
| build + broadcast `t3-t2` | 735.9 | 709.7 | 741.3 | 704.1 |
| L2 inclusion `t4-t3` | 233.9 | 214.7 | 225.4 | 240.4 |

All in ms. gasUsed 77,290, total fee ≈ 4.65e11 wei per settle (unchanged by config, as expected).

### Reading this row
- **Both confounds removed.** Signing collapses to 13.5 ms with a CI of ±0.1 ms — the M4
  back-to-back figure — confirming the governor attribution. The WiFi slow cluster is gone
  (0/30 runs over 100 ms, down from 11/30), confirming power save.
- **Controlled, WiFi costs ~7 ms more than Ethernet on the wire** (13.5 vs 6.1 ms; Welch
  p ≈ 2e-22), but that is invisible end-to-end: latency eth vs wifi p = 0.52. At L2 block
  cadence and public-RPC overhead, the device's link choice is not what limits latency.
- **The default-config WiFi penalty (~190 ms) was almost entirely power save,** not the radio
  link. That is a device-configuration finding for ch. 4: an IoT agent left with stock power
  management pays it on every reply, and disabling power save costs energy (an M7 question).
- **Where the second goes, controlled:** ~70% gateway↔RPC round trips (`t3-t2` ≈ 705 ms), ~23%
  L2 inclusion (≈ 215–240 ms, half a block period on average as expected), ~2% signing, ~1%
  network. The chain is not the bottleneck; the public RPC endpoint is.
- Kurt et al. (2021) WiFi reference: 2.6 s. Ours: 0.98 s on WiFi with on-chain settlement per tick.

### Budget after
Gateway holds **92,463,517,804,653 wei** (~0.000092 ETH) after 126 settles on this channel.

## M5 — L1 baseline: Ethereum Sepolia, controlled Pi config, Ethernet and WiFi, N=30 each (2026-10-14)
- Chain: **Ethereum Sepolia, chainId 11155111**, RPC `https://ethereum-sepolia-rpc.publicnode.com`
  (client reth/v2.7.0). Gateway env `.env.sepolia`; Pi overlay `~/magis/.env.sepolia`.
- Commit **c9b0329** both machines. Pi config controlled (powersave disable, governor performance).
- Funding: 0.05 ETH from the Google Cloud Sepolia faucet to the gateway `0xc2084f9f…B324`.
- Contract `0xd0D2D1680c17F1F11d246A80fc1E034c3d067766`
  ([deploy `0x90b9a838…a973f`](https://sepolia.etherscan.io/tx/0x90b9a83829449730ab329bfa88d36eb2041913c8151f4ea2e07d16ad093a973f)),
  channel `0x8800F104092fAA7f5A0F0DF6aa47671Dad264685`
  ([open `0x0d1dd6a5…eed9`](https://sepolia.etherscan.io/tx/0x0d1dd6a5ba7506e88aa43dcbdf5899061525ae7ecfc06817992a345febcdeed9)),
  deposit 0.00001 ETH. Nonces 1–2 smoke, 3–62 dataset.
- **Address collision, not a mix-up:** the Sepolia contract has the same address as the retired M3
  contract on Base Sepolia, and the channel id equals the closed M4 Base channel. Both follow from the
  same deployer key at the same account nonce (and the M6 finding that channel ids omit the contract).
  Every row carries `chainId` and `contract`; never key on address alone.
- Gap 12–24 s (≥ one 12 s slot). Data: `m5_sepolia_eth_20261014T163602Z-960d1c.csv`,
  `m5_sepolia_wifi_20261014T164908Z-4086b4.csv`. **60/60 ok.**

### Same build, much higher gas for state creation
Creation bytecode is byte-identical to the Base deployment (verified on-chain); the runtime code
differs only in three short runs holding the EIP-712 immutables (cached chainId and
`address(this)`). The gas is not:

| | Base Sepolia (L2) | Ethereum Sepolia (L1) | ratio |
|---|---|---|---|
| deploy | 2,037,870 | **14,375,662** | 7.05x |
| `open()` | 167,067 (M4) / 149,967 (M5) | **694,887** | ~4.2x |
| first `settle()` on a channel | 111,477 | **467,793** | 4.2x |
| subsequent `settle()` | 77,290 | **88,365** | 1.14x |

The pattern — large multipliers exactly where new state is created (code deposit, fresh storage
slots), small where existing slots are updated — is consistent with Ethereum Sepolia running a
**state-creation gas repricing that Base Sepolia has not adopted**. *Which* upgrade/EIP must be
confirmed from the Sepolia fork schedule before ch. 4 names it. Consequence for H1: "gas units are
reproducible" holds per chain, but the **same contract no longer costs the same gas units on L1 and
L2**, so the cost table must state gas per chain.

### Results, controlled config (mean ± 95% CI, ms)
| | L2 Base eth | L2 Base wifi | **L1 Eth eth** | **L1 Eth wifi** |
|---|---|---|---|---|
| **reported `t4-t0`** | 957.5 ± 54.5 | 984.6 ± 67.0 | **8169.9 ± 1454.7** | **8736.4 ± 1354.8** |
| median | 981.5 | 984.6 | 8400.3 | 8695.5 |
| signing | 13.6 | 13.5 | 13.6 | 13.5 |
| network, both legs | 6.1 | 13.5 | 8.5 | 18.3 |
| build + broadcast `t3-t2` | 709.7 | 704.1 | 676.6 | 682.4 |
| block inclusion `t4-t3` | 214.7 | 240.4 | **7458.0** | **8009.0** |
| gasUsed (settle) | 77,290 | 77,290 | 88,360 | 88,378 |
| effectiveGasPrice | ~6.0e6 wei | ~6.0e6 wei | ~1.0e6 wei | ~1.0e6 wei |

L1 − L2 latency: **+7.2 s** (eth, p = 4.7e-11), **+7.8 s** (wifi, p = 1.6e-12). The whole difference
is block inclusion; build + broadcast does not differ (p = 0.11 / 0.19). WiFi vs Ethernet on L1:
p = 0.56 — as on L2, the device's link is invisible end-to-end.

### Inclusion verified against block timestamps
For all 60 L1 rows, broadcast (`t3`) → block timestamp is 0.8–13.4 s (mean 6.5 s, ~half a slot) and
**every transaction landed in the next block** — none waited an extra slot at 0.001 gwei. Block
timestamp → receipt seen (`t4`) is 1.2 s mean (max 3.5 s): propagation + RPC lag, inside `t4-t3`.
So the ~8 s is the slot time, not RPC slowness.

### Reading this row (H1)
- **Latency:** L2 settles ~8.5x faster (≈1.0 s vs ≈8.2–8.7 s). On L2 the public RPC round trips
  dominate (~70%); on L1 the block slot dominates (~90%). Kurt et al. (2021) WiFi 2.6 s sits between.
- **Cost: testnet prices do not show the L1/L2 gap and must not be used for it.** Sepolia gas was
  ~0.001 gwei, Base Sepolia ~0.006 gwei — on testnets L1 *looks* cheaper. The cost comparison must be
  gas units per chain (table above) × mainnet gas prices pinned to one stated date, plus Base's L1
  data fee. Not done yet — needs the reference date chosen.
- Network term is ~2–5 ms higher in the L1 sessions than the L2 controlled sessions on the same
  links (eth 8.5 vs 6.1, wifi 18.3 vs 13.5). Same config, ~30 min later; ambient LAN/RF load is the
  plausible cause. Small against an 8 s total, noted so it is not mistaken for a chain effect.

### Budget after
Sepolia gateway: **49,969,070,816,102,527 wei** (~0.04997 ETH). The whole L1 baseline — deploy, open,
deposit, 62 settles — cost ~3.1e13 wei at testnet prices.

## H1 cost table — mainnet prices, reference date 2026-10-14
- Script: `experiments/costs.py --date 2026-10-14`; outputs `experiments/analysis/costs_2026-10-14.{csv,json}`.
- **Read-only mainnet access** (approved 2026-10-14): `eth_simulateV1`, `eth_feeHistory`, `eth_call`.
  No mainnet key, no funds, nothing broadcast. RPCs: Ethereum `ethereum-rpc.publicnode.com`,
  Base `mainnet.base.org` (publicnode's Base endpoint refuses history without a token).
- Gas price: per block, base fee + median priority fee, 24 h before the run — Ethereum 7,201 blocks
  to #26192178, Base 24 × 1,024-block windows to #52604441. ETH/EUR: CoinGecko daily, 2,228.86.

### Gas units by simulation, validated against real transactions
Ethereum Sepolia's repricing (fresh SSTORE 126k vs 43k; code deposit ~7x) is **not on mainnet**:
Ethereum mainnet, Base mainnet and Base Sepolia charge identical gas for the probes and for the full
lifecycle. So Sepolia's measured gas cannot stand in for mainnet L1. Gas units below come from
`eth_simulateV1` of the whole lifecycle on each chain (throwaway keys, EIP-712 signed for that
chainId, one contract, three channels). Controls — simulation vs the real testnet transactions:

| control | simulated | measured | diff |
|---|---|---|---|
| Base Sepolia deploy / open first / open later | 2,037,870 / 167,067 / 149,967 | same | 0 / 0 / 0 |
| Base Sepolia settle first / settle / close-only | 111,441 / 77,229 / 143,578 | 111,477 / 77,290 / 143,590 | −36 / −61 / −12 |
| Eth Sepolia deploy / open first | 14,375,662 / 694,875 | 14,375,662 / 694,887 | 0 / −12 |
| Eth Sepolia settle first (new merchant) / settle | 467,757 / 88,329 | 467,793 / 88,365 | −36 / −36 |

All within 61 gas (calldata zero-byte noise from random signatures). Scenario matters and is
explicit: `open` later vs first channel differs by 17,100 (`_channelIndex` 0→1); a first settle paying
a never-funded merchant costs +25k on mainnet, +183k on Sepolia.

### Mainnet prices, 24 h
| | median | p10 | p90 |
|---|---|---|---|
| Ethereum gas price | 0.1163 gwei | 0.0670 | 0.8216 |
| Base gas price | 0.0060 gwei | 0.0060 | 0.0065 |
| Base L1 data fee, one settle (760 B signed) | 2.77e9 wei | 2.33e9 | 1.51e10 |

### The table (EUR at 2,228.86 EUR/ETH; median [p10–p90])
| operation | gas (both chains) | Ethereum L1 | Base L2 (exec + L1 data) |
|---|---|---|---|
| settle, per payment | 77,229 / 77,217 | **€0.0200** [0.0115–0.1414] | **€0.00104** [0.00104–0.00115] |
| settle, first on channel | 111,441 | €0.0289 | €0.00150 |
| open (later channel) | 149,967 | €0.0389 | €0.00201 |
| close (only settlement, M4) | 143,566 | €0.0372 | €0.00193 |
| deploy (one-off) | 2,037,870 | €0.528 | €0.0273 |
| **per tick, channel, close/200** | 717.8 | €0.000186 | **€0.0000097** |
| per tick, channel, (open+close)/200 | 1,467.7 | €0.000381 | €0.0000197 |

### Reading this (H1 cost)
- **Per settlement, L2 is ~19x cheaper than L1 at the median price** (€0.00104 vs €0.0200), and ~120x
  at the day's p90 (€0.00115 vs €0.141): Ethereum's fee is volatile, Base's barely moves.
  "Far below L1" holds, but the median gap is ~1.3 orders of magnitude, not more — Ethereum mainnet
  gas was cheap on this date (0.12 gwei). State the date; the ratio moves with L1 congestion.
- **The channel matters more than the chain.** 200 ticks per `close()` cuts per-payment cost ~108x
  on either chain. Channel on L2 vs per-tick settle on L1: **€0.0000097 vs €0.0200, ~2,000x.**
- **Base's L1 data fee is now negligible:** 2.8e9 wei of ~4.7e11 per settle, ~0.6%. CLAUDE.md's warning
  that forgetting `l1Fee` "understates rollup cost by a lot" was true before blob data (EIP-4844); at
  these blob prices it is not. Still measured and reported, because it is the term that would move
  if blob demand rose (its own p90 is 5.5x its median).
- Gas units are identical on both mainnets for this contract, so **the whole L1/L2 cost difference is
  price, not gas**. On testnets it was the reverse (Sepolia's repricing changed the gas, the prices
  were meaningless) — which is exactly why testnet cost figures are not used for H1.
