
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
