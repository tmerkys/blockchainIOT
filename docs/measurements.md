
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
