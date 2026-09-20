
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
