# Development log — how the prototype was built (Claude Code sessions)

Reconstructed 2026-11-24 for the thesis appendix / methodology chapter. One row per milestone
(M0–M7, CLAUDE.md) and per results-v2 session (v2-1…v2-4).

## Sources and how far each claim can be trusted

The full Claude Code transcripts for August–October were deleted automatically by Claude Code's
30-day local cleanup (retention raised to 365 days on 2026-11-24). What survives:

| Tag | Source | Covers | What it can prove |
|---|---|---|---|
| **[P]** | Author's own prompts, `~/.claude/history.jsonl` (127 prompts, 2026-08-30 → 11-24) | All stages | What the author asked for or proposed, in their own words. Pasted multi-line prompts from 09-14/09-20 are stored only as "[Pasted text]" — their content is lost. |
| **[C]** | Git history, 66 commits, 2026-08-11 → 10-16 | All stages | What was built, fixed and measured, and the stated reason |
| **[M]** | `docs/measurements.md` | M1–v2-4 | Every run, its conditions, commit, failures and deviations |
| **[B]** | `docs/thesis-brief.md` | results-v1/v2 | What is reported, and the limitations list (§8) |
| **[I]** | Inference from the above | — | Used only where a prompt and a later commit together imply who proposed what. Treat as unverified. |

Claude's own replies for M0–v2-4 are **not** recoverable from local files. Two sessions are
linked from commit messages and may still be viewable on claude.ai (they ran with Remote Control
or in the cloud):
`https://claude.ai/code/session_01CfzD4d3kbj31pXMr3dvctE` (M2–M6, 2026-09-20) and
`https://claude.ai/code/session_01NT8R8jscKnNcBoGJswfYwy` (v2-1–v2-3, 2026-10-15).
If they open, use them to fill or correct the [I] cells.

Division of work, in general: the author wrote the spec (CLAUDE.md), set the milestone order,
issued the task prompts, did all physical work (Pi, router, cables, faucet, funding, sudo
setup, NTP resync) and made the budget and scope calls. Claude Code wrote the code, tests,
analysis scripts and measurement-log entries and drove the runs over SSH.

---

## Main table

| Stage | Dates | Commits / tag | Goal (CLAUDE.md) | What was done | Changed from the plan | What went wrong | What went right |
|---|---|---|---|---|---|---|---|
| **M0** scaffold | 08-11 → 08-30 | `d91bdbc`…`a32959e` | Foundry, venv, anvil, trivial deploy | Foundry project, `.gitignore`, `requirements.txt` with pinned versions [P][C] | — | forge-std submodule and `lib/`, `out/`, `cache/`, `foundry.lock` were committed and had to be untracked (3 fix commits) [C] | Done in one day |
| **M1** end-to-end on one machine | 08-30 → 09-14 | `81bacfb`…`cfa01d4`, `a184969`; PR #1 | Sensor → sign → HTTP → gateway → `Settled`, t0–t4 printed | `VoucherLib` + `shared/voucher.py` (EIP-712), `PaymentChannel.settle()` 3-of-3, `sensor.py` + `agent.py`, FastAPI gateway + relayer, unit tests; one local run, t4−t0 = 168.7 ms, 109,217 gas [C][M] | Built as separate feature branches (`feature/device-agent`, `feature/gateway`) merged into `develop` [C] | A stray `contracts/shared/` directory appeared (duplicate of `shared/`); author spotted it and asked for it to be investigated before deletion, then deleted it and asked for the repo to be reorganised [P] | Author's prompts were written straight from the spec, task by task ("exactly as specified in CLAUDE.md's 'Contract, core shape'") [P]; N=1 run recorded as a sanity check only, explicitly not an H1 sample [M] |
| **M2** device on the Pi | 09-20 | `bfd2a0f`, `627ec7b` | Key generated on the Pi; sensor event originates on the Pi; Ethernet | `keygen.py` (scrypt keystore on the Pi), `m2_setup.py`, `gateway_serve.py`; **single-clock latency**: Pi measures `t4_dev − t0` on its own monotonic clock, so NTP skew never enters the reported number [C] | Spec said "run chrony and check skew"; implementation went further and removed cross-machine subtraction entirely [C][M]. chrony was not yet running (timesyncd / w32time), recorded as a deviation [M] | Recurring friction over SSH: Claude kept asking the author to type the Pi password; author asked four times for it to be used directly [P]. (Resolved properly at M5 with key-based SSH and narrow sudo [P]) | 1,430 ms vs M1's 168.7 ms correctly explained as a different point in anvil's 2 s block cycle, not a regression [M] |
| **M3** real L2 (Base Sepolia) | 09-20 | `ef65e5b`…`c0634d3` | Explorer link to a real `Settled` triggered by the Pi | Two Pi-triggered `Settled` txs, verified via Blockscout; first settle 134,217 gas, later ones 75,029 [M] | Keystore passphrase must be typed on the Pi (no env/CLI); RPC timeout 2 → 10 s; `resume_from()` added so the device continues from on-chain `paid`/`nonce`; gateway funded with 0.0005 ETH by the author [P][C]. **Funder collapsed into the gateway key** (one faucet drip) [M] | 5 bugs that only appear on a public chain: OP-stack `l1Fee` returned as hex strings (would crash after the tx was mined); load-balanced RPC returned stale nonces; gateway's confirmation wait counted as 2 s of "network"; second settle reverted "not monotonic" (device restarted from 0); **`m2_setup.py` printed the merchant private key** → key treated as burned and rotated [C][M] | Each bug fixed with a test before the swap was trusted; RPC benchmarked → publicnode, t3−t2 1135 → 670 ms; chrony installed on the Pi, offset drift −9.0 → +2.7 ms [C][M] |
| **M4** the channel | 09-20 | `70a41de`, `7b2a93a` | 200 ticks, one settlement, ~100× cheaper | `close()` with funder refund, `run_channel.py`; **104.5× (2.02 orders) amortised gas reduction**, 718 gas per micropayment; 48.8× if `open()` is also amortised [C][M] | **Price scaled 100× down** for this run because 200 ticks at the nominal price exceeded the faucet budget; Claude laid out three options before the run, option 1 was taken [M] (who chose: [I] author). Refund goes to `funder`, not the gateway or device — design decision justified by H2 [C] | **~0.000288 ETH permanently stranded** in the M3 contract: adding `close()` required a new deployment and the old one had no exit path. This contradicted Claude's earlier expectation that `close()` would recover it [M] | Gas is independent of the amount, so the scaling changes no measured figure [M]; the cost of 200 signatures (13.2 ms each on the Pi) is reported rather than hidden [C] |
| **M6** security (H2) | 09-20 | `51ef5f6` | 7 abuse cases revert + live attack | `GatewayAbuse.t.sol` (15 tests), `Replay.t.sol` (10), shared harness; **live attack on Base Sepolia** with the device signature replaced: mined in block 47070886, status 0, "bad device sig", funds unchanged; 55 forge tests pass [C][M] | **Done before M5**, against CLAUDE.md's "do these in order" [C] | The 13 M4 `close()` tests turned out to cover none of the 7 `settle()` cases; all 7 were written from scratch [C]. Found: channel IDs are not unique across deployments → CSVs keyed on (contract, channel) [M] | Each domain-separator test has a positive control, so reverts can't be explained by "the contract rejects everything" [C] |
| **M5** experiment harness | 10-13 → 10-15 | `02b8bb5`…`b2bdc48`, `86d1ec1`, `f54f47d`; tag **`results-v1`** (`d334683`) | N≥30 per condition, {Eth, WiFi, BLE} × {L2, L1}, CSV, CI, plots | `runner.py` over SSH, `run_batch.py`, `analyze.py` (Welch tests, 95% CI, figures). L2: Eth 957.5 ± 54.5 ms, WiFi 984.6 ± 67.0 ms (controlled); L1: 8.17 / 8.74 s. Mainnet-priced cost table: settle €0.00104 on L2 vs €0.0200 on L1 [C][M] | **Bluetooth dropped** (laptop has no Bluetooth, work Mac has corporate VPNs) [P]. A "default vs controlled" Pi-config dimension was added after the first session [C]. Thesis brief + handoff bundle prepared so the writing could be delegated to Claude on claude.ai, at the author's request [P][C] | Pi could not join the router's WiFi; Claude's first hypothesis (MAC filter) was **wrong**; real cause: WPA2/WPA3 transition mode → set to WPA2-only [M]. Author was locked out of the router admin page and factory-reset it [P]. First session confounded by WiFi power save (11/30 slow runs) and CPU governor (signing 39 ms) [C][M] | Both confounds identified, removed, and kept as a finding (default config costs ~190 ms on WiFi) [M]; route checked before every run, so a "WiFi" run can't silently go over Ethernet [C]; Sepolia gas anomaly traced to Glamsterdam / EIP-8037 [C] |
| **M7** energy | 10-15 | `c602b61`, `3ef8486` | USB power meter, or CPU time as a stated limitation | Per-run CPU time: signing 13.50 ± 0.13 ms, whole device side 16.17 ± 0.16 ms, merchant countersign 13.18 ms [C] | **No power meter → CPU-time fallback**, as CLAUDE.md allows; **proposed by the author** ("add per-run CPU-time recording … with its proxy status stated as a limitation") [P] | Clock-offset diagnostic showed +488 ms: the laptop's Windows clock had drifted. Diagnostic only; no reported figure uses it [C] | Same condition as the 10-14 WiFi session (p = 0.46), so pooled to n = 60 [C] |
| **H2 throughput** (part of results-v1) | 10-15 | `e831b13`, `bdcee57`, `48d4553` | "without destroying throughput" | 3-of-3 vs a single-signature baseline: +22.7% gas, −18.5% settles/block, gateway 19.0 vs 7.1 ms/voucher; live burst 30/30 [C] | — | **Correction of an earlier claim**: Base `t4` is a Flashblocks pre-confirmation, not block inclusion. The "half a block period" reading in the M5 log was wrong; sealed-block latency added (L2 2.3–2.4 s vs L1 8.3–8.8 s) [C][M] | Error caught and corrected in the log in place, with the old text kept and marked [M] |
| **v2 setup** | 10-15 | `ed8f99d`…`354c1ad` | (beyond the spec) | Three code branches built in parallel: `exp/finality`, `exp/rpc`, `exp/concurrency`; `v2_` file prefix so new sessions can never move a published results-v1 number [C] | Dedicated RPC (Alchemy) provided by the author [P] | Author questioned whether the three tasks could run in parallel on the same testnet account [P]. Outcome visible in the code: the live sessions ran one after another, and concurrency got a **separate load-gateway key** [M] ([I] whether Claude changed course because of the question). Claude flagged the API key in `.env`; author: testnet, doesn't matter [P] → RPC URL still never written to logs/CSVs, host only [C] | Merge conflicts resolved with regression tests; `summary.csv`/`tests.csv` verified byte-identical to results-v1 [C] |
| **v2-1** dedicated RPC | 10-15 | `615d34d` | — | Alchemy vs same-day publicnode, N = 30 each: −118.6 ms latency (p = 8.3e-4); Base finality ~17.5–21.8 min after t4, depending on the provider [C] | Same-day control instead of comparing with results-v1 [C] | Finality reading depends on which provider you ask (~4.4 min apart) → reported per observer [C][B] | Author asked Claude to start the session by itself while away, and followed via Remote Control [P] |
| **v2-2** L1 finality | 10-15 | `c60e32f` | — | Ethereum Sepolia N = 30: finalized 972.9 s after t4; Base finalizes *later* than L1 by 76–337 s [C] | — | — | Latency agrees with results-v1 (p = 0.20) [C] |
| **v2-3** concurrent load | 10-15 | `8a2d148` | — | K = 1, 2, 4, 8 devices on the Pi, pipelining gateway: exactly K settles per Base block up to K = 4 [C] | K separate keys generated on the Pi; passphrase passed via env with the author's approval [M] | K = 8: 51/80 ok. 28 × HTTP 429 (Alchemy free-tier rate limit); 1 × **nonce bug in Claude's gateway code** (a receipt-poll error released a nonce that was already used) [C] | Failures recorded as distinct statuses, raw CSVs kept as written [C] |
| **v2-4** K = 8 rerun | 10-16 | `548892d`, `da13d67`; tag **`results-v2`** (`e0c98cc`) | — | Nonce bug fixed with a regression test; K = 8 rerun on publicnode: **64/64 ok, 8 settles per block** [C][M] | RPC switched back to publicnode to avoid the rate limit [M] | Pi reboot reset the CPU governor, so it had to be set again before the run [M] | Analysis changed to report per session, so the two K = 8 sessions aren't pooled [C] |

---

## Disagreements, corrections and who proposed what

The rows the author asked for specifically. Only what the sources support; [I] = inferred.

| # | Stage | Author's position / request | Claude's position / action | Outcome | Source |
|---|---|---|---|---|---|
| 1 | M1 | Investigate the stray `contracts/shared/` and report before deleting anything; then "just reorganize the repo according to best practices" | Investigated (details lost) | Deleted on the author's instruction | [P] |
| 2 | M2 | Use the Pi's SSH password directly, stop asking | Kept asking the author to enter it (4 times) | Replaced at M5 by key-based SSH and a narrow sudoers rule set up by the author | [P]; reason on Claude's side [I] |
| 3 | M3 | — | Claude's own script printed the merchant private key | Claude flagged it, treated the key as burned, rotated it without redeploying | [C][M] |
| 4 | M4 | Decide how to fit 200 ticks into the faucet budget | Laid out 3 options (scale price / shorter tick / faucet top-up), recommended scaling the price | Price scaled 100× | [M]; decision by author [I] |
| 5 | M4 | — | Expected `close()` would recover the M3 escrow | Wrong: ~0.000288 ETH stranded for good; recorded as a lesson on upgradeability | [M] |
| 6 | M6 | — | Claude did M6 before M5 | Deviation from the "in order" rule in CLAUDE.md; no harm found | [C] |
| 7 | M5 | Asked how to fix the WiFi problem from last time | First hypothesis: MAC filter on the router | Wrong; cause was WPA2/WPA3 transition mode | [P][M] |
| 8 | M5 | No Bluetooth on the laptop, the Mac has corporate VPNs; do the L1 baseline instead | — | BLE left pending; L1 baseline measured | [P] |
| 9 | M5 | "Could you start writing the thesis now … maybe a frontend is needed for demonstration?" | CLAUDE.md excludes a UI | No frontend built; cost table and thesis brief done instead | [P][C]; Claude's reply [I] |
| 10 | M5 | Delegate thesis writing to Claude on claude.ai with the session context | Produced `thesis-brief.md` + gitignored handoff bundle | Done | [P][C] |
| 11 | M7 | **Proposed** per-run CPU-time recording + one more N = 30 session, stated as a limitation | Implemented | Measured | [P][C] |
| 12 | H2 | — | Earlier analysis read Base `t4` as block inclusion | Wrong: Flashblocks pre-confirmation; corrected in the log and figures | [C][M] |
| 13 | v2 | Should the three tasks really run in parallel, given they share the testnet account? | Built the code branches in parallel | Live sessions run one after another; separate load key for concurrency | [P][C][M]; causal link [I] |
| 14 | v2 | Testnet API key exposure doesn't matter | Kept host-only RPC recording anyway | Key never reaches logs or CSVs | [P][C] |
| 15 | v2-3 | — | Claude's pipelining gateway had a nonce-release bug | Found from the K = 8 failure, fixed with a regression test, rerun 64/64 | [C] |

---

## Still open (from thesis-brief §9)

- Bluetooth transport: not measured (no adapter).
- M7 energy in joules: not measured (no power meter); CPU-time proxy only.
- H3 (MiCA / PSD2): analytical, author.
