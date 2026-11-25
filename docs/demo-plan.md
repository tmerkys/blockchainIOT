# Defence demo — plan for review

Status (2026-11-25): **built, live only.** After a first rehearsal the user dropped the offline
modes: replay and anvil are removed, and the page is in Lithuanian with fewer words and three tabs
(payment, 200 payments, attack; no results tab). Two modes remain, both on Base Sepolia:
`python -m demo` (Pi, default; this computer's IP on the Pi's network is found automatically, or
pass `--gateway-host`) and `python -m demo --mode laptop`. Open http://127.0.0.1:8080. Sections
below that mention replay, anvil or a results tab describe the first version.

## 1. Goal

A live demonstration for the thesis defence, about 5 minutes. It shows the prototype working and
makes the two hypotheses visible:

- **H1:** a voucher signed on the device settles on Base Sepolia in about a second, cheaply.
- **H1 cost:** 200 micropayments go on-chain as a single transaction.
- **H2:** the gateway cannot take money without the device's signature.

The demo is a **presentation aid, not a measurement**. No number it shows goes into the thesis.
The thesis numbers come from results-v1 and results-v2.

## 2. Hard rules

1. **Kept apart from the measurement code.** Everything lives in a new `demo/` folder. It does
   not change `experiments/`, `experiments/results/`, `gateway/relayer.py`, `device/agent.py` or
   any timing code. It only imports and calls them. results-v1 and results-v2 stay reproducible
   from their tags.
2. **Honest about what is live.** Every screen shows a mode badge: `LIVE · Raspberry Pi`,
   `LIVE · simulated device (laptop)` or `REPLAY · recorded 2026-10-15`. A replay is never
   presented as live.
3. **Testnets only.** Base Sepolia, plus anvil for rehearsal. No mainnet.
4. **Work Mac:** the repo lives in its own folder, away from company code. The VPN is off during
   the demo. The demo server and gateway listen only on the hotspot or localhost, never on all
   interfaces. Only testnet keys go in `.env`.
5. **CLAUDE.md gets one line:** "`demo/` is a presentation aid for the defence, outside the
   measurement scope." That way the "no web UI" rule keeps protecting the measurements.

## 3. Setup on the day

```
 Raspberry Pi ──WiFi──┐
                      iPhone hotspot ──mobile data──► Base Sepolia (public testnet)
 Mac ─────────WiFi────┘
  ├─ gateway  (scripts/gateway_serve.py, unchanged)
  └─ demo server + web page (new, demo/)  ──► projector
```

- The iPhone hotspot replaces the router, so the Pi doesn't depend on the venue network.
- iPhone setting **"Maximize Compatibility" ON**: forces WPA2. The Pi fails on mixed WPA2/WPA3,
  which is what broke WiFi on the home router.
- The Pi reaches the gateway at `<mac-name>.local` (mDNS), because hotspot IPs change.

## 4. Three modes (fallback ladder)

The audience sees the same page in every mode. Only the badge changes.

| Mode | Device | Chain | Needs | Used when |
|---|---|---|---|---|
| **1. Live Pi** | Raspberry Pi, signs with its own key | Base Sepolia | Pi, hotspot, mobile data | Everything works |
| **2. Live, simulated device** | Same `DeviceAgent` code on the Mac, with a demo key | Base Sepolia | Mobile data | Pi won't connect |
| **3. Replay** | Recorded real runs from `experiments/results/` and `docs/measurements.md` | None (offline) | Nothing | No internet |

Plus **anvil mode** for rehearsing at home. It works like mode 2 against a local chain, with no
faucet funds spent.

Switch modes with a dropdown on the page, or a start flag: `python -m demo --mode pi|laptop|replay|anvil`.

## 5. What the page shows (the 5-minute script)

**Tab 1. One payment (H1)**
- A "Charge" button fires one meter tick.
- A pipeline of boxes lights up as each mark arrives:
  `⚡ Meter tick (t0) → ✍ Signed on device (t1) → 📡 Gateway received (t2) → ⛓ Tx broadcast (t3) → ✅ Settled in block (t4)`
- A latency bar splits the run into signing, transport and chain time, the same way
  `fig_decomposition` does in the thesis.
- A settlement card shows the nonce, amount, gasUsed, l1Fee and the total fee, plus a
  **BaseScan link** that you click to show the real transaction on the public explorer.
- A small panel with your thesis reference figures (mean latency and 95% CI per condition), so
  that one noisy live run doesn't stand for the result.

**Tab 2. The channel (cost)**
- The "Open channel" button runs `open()`: one transaction, with a BaseScan link.
- The "Charge 200 ticks" button signs 200 vouchers off-chain. A counter runs up: *vouchers signed
  200 · on-chain transactions 0*. This takes a few seconds, with no on-chain wait per tick.
- The "Close" button runs `close()`: one transaction, which pays the merchant and refunds the rest.
- Result: *200 payments · 1 settlement transaction · 104.5× less gas per payment* (live figure,
  with the recorded M4 figure next to it).

**Tab 3. Attack (H2)**
- "Gateway tries to settle without the device's signature." It runs two steps, the same as
  `scripts/h2_live.py`:
  1. **Through the gateway API:** refused with HTTP 422, no transaction.
  2. **The gateway attacking the contract directly:** it forges the device slot with its own
     key, skips every off-chain check and forces the transaction on-chain. The transaction is
     **mined and reverts** with `bad device sig`, and the balances don't change. A BaseScan link
     shows the failed transaction.
- The page shows the balance before and after, so "nothing moved" is visible.

**Tab 4. Results**
- The existing figures (`fig_latency`, `fig_decomposition`, the cost table, L2 vs L1) as static
  images, for questions.

## 6. How it works (technical)

```
demo/
  __main__.py      # starts the demo server: python -m demo --mode ...
  server.py        # FastAPI; serves the page, runs actions, pushes events via SSE
  modes/
    live_laptop.py # mode 2 + anvil: DeviceAgent + HttpTransport in-process
    live_pi.py     # mode 1: runs demo/pi_agent.py on the Pi over SSH, streams its output
    replay.py      # mode 3: plays recorded runs with their real timings and tx hashes
  pi_agent.py      # runs ON the Pi: same agent code, prints one JSON line per event
  static/
    index.html     # one page, plain JS + CSS, no framework, no build step
  test_demo.py     # replay mode + event format tests (no chain needed)
```

- **Events:** each step (`tick`, `signed`, `received`, `broadcast`, `settled`, `error`) is one
  JSON event pushed to the page over server-sent events. The same format is used in all modes,
  so the page doesn't know or care which mode is running, apart from the badge.
- **Mode 1, the Pi:** the demo server runs `ssh magis-pi python demo/pi_agent.py …` and reads its
  JSON lines. `pi_agent.py` is a thin wrapper around the existing `DeviceAgent`. The device key
  stays on the Pi as before. No server process runs on the Pi. A side panel can show the raw Pi
  output for the "this is really the Pi" moment.
- **The gateway** is the existing `scripts/gateway_serve.py`, unchanged. The demo only sends it
  vouchers, the same as the Pi does.
- **Channel open/close:** reuses `open_channel` from `scripts/m2_setup.py` and the close path from
  `device/run_channel.py`.
- **Attack:** reuses `scripts/h2_live.py`'s logic (`Relayer.build_settle_tx` plus a forced send).
  Refactoring it into importable functions is fine if the script's behaviour doesn't change.
- **Replay data:** a run from `m5_base-sepolia_wifi_20261015T145753Z-5c1e14.csv` (real t0–t4 and
  tx hash), the M4 close tx `0x7d4884a0…`, and the H2 forced tx `0x41b2dd32…`. The BaseScan links
  in replay mode still work, because those transactions are permanently on the testnet.

## 7. Before the day (checklist)

- [ ] Top up the Base Sepolia gateway from a faucet. It has about 3.6e13 wei, roughly 70
      settlements. Aim for ≥ 0.01 ETH.
- [ ] Fund a demo channel and the funder key for a fresh `open()`.
- [ ] Save the iPhone hotspot on the Pi (you run this, so the password stays with you):
      `sudo nmcli dev wifi connect "<iPhone name>" password "<pw>"`
- [ ] Full rehearsal at home on the hotspot: modes 1, 2 and 3, with the VPN off.
- [ ] Check that the Mac's firewall or company security software allows the gateway port.
- [ ] Record a screen video of a full mode 1 run as a last-resort backup.
- [ ] Charge a power bank for the Pi.

## 8. Build order

1. **Replay mode and the page** (offline, needs nothing): the event format, SSE and all four tabs.
2. **Anvil mode:** the real code path against a local chain, for rehearsal.
3. **Mode 2** on Base Sepolia, after the faucet top-up.
4. **Mode 1** with the Pi on the hotspot. Needs the Pi switched on.

Estimated time: about 1 day for steps 1–3, plus a session with the Pi for step 4.

## 9. Out of scope

- No changes to the measurement code, the contracts or the results.
- No new measurements. Live demo numbers are not added to `docs/measurements.md`.
- No login, database, deployment or mobile layout. It runs on localhost or the hotspot only.
- No Bluetooth in the demo.

## 10. Decisions (2026-11-25)

1. Page language: **English**.
2. Channel: **both**. By default a channel is opened before the presentation (a "Prepare" button
   or start-up step), and the demo does the 200 ticks plus `close()` live. An optional
   "Open live" button does a fresh `open()` on stage, if there's time.
3. Attack: **both steps**, the API refusal and then the on-chain revert.
