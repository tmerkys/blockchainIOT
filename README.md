# IoT L2 micropayments — thesis prototype

Artifact for the master's thesis *"Blokų grandinės pagrindu veikiantys finansiniai atsiskaitymai tarp
IoT įrenginių"*. A Raspberry Pi 4 signs EIP-712 payment vouchers for a simulated EV-charging session;
a gateway on a laptop relays them to a 3-of-3 `PaymentChannel` contract on Base Sepolia (L2) or
Ethereum Sepolia (L1) and pays the gas. The device never holds chain state or ETH, and the gateway
cannot move funds on its own signature.

- **Spec and hypotheses:** [`CLAUDE.md`](CLAUDE.md) (H1 latency/cost, H2 security/throughput).
- **Lab log — every run, commit and decision:** [`docs/measurements.md`](docs/measurements.md).
- **Results summary for the thesis text:** [`docs/thesis-brief.md`](docs/thesis-brief.md).
- **Final results:** tag `results-v1`. Raw data in `experiments/results/`, statistics and figures
  in `experiments/analysis/`.

Testnets only. No mainnet keys, funds or transactions; the mainnet cost table uses read-only RPC.

## Layout

| Path | What |
|---|---|
| `contracts/` | Foundry project: `PaymentChannel.sol`, `VoucherLib.sol`, tests incl. the H2 abuse suite; `test/baseline/SingleSigChannel.sol` is a test-only single-signature baseline for the throughput comparison |
| `shared/voucher.py` | canonical EIP-712 voucher, shared by device and gateway |
| `device/` | runs on the Pi: key generation, agent, simulated kWh meter, HTTP transport, `run_batch.py` (experiment leg) |
| `gateway/` | FastAPI gateway: verify, co-sign, submit, await receipt |
| `scripts/` | setup (`m2_setup.py`), gateway launcher (`gateway_serve.py`), M1 local e2e, H2 live attack |
| `experiments/` | `runner.py` (drives sessions over SSH), `analyze.py`, `costs.py`, `throughput.py` |

## Reproduce

**Prerequisites:** Python 3.11, Foundry, a Raspberry Pi 4 on the same LAN as the laptop (Ethernet and
WiFi), SSH access to it (`magis-pi` host alias), testnet ETH for the gateway and funder keys.

```bash
# laptop
python -m venv venv && venv/Scripts/pip install -r requirements.txt   # venv/bin/ on Linux/macOS
cd contracts                  # contracts/lib/ is gitignored; install the pinned libraries:
forge install --no-git foundry-rs/forge-std@v1.16.2 OpenZeppelin/openzeppelin-contracts@v5.7.0
forge build && forge test -vv && cd ..
cp .env.example .env          # fill in RPC, chain id and testnet keys; see comments in the file

# Pi (clone of this repo at ~/magis, same commit as the laptop)
python -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python device/keygen.py --write-env   # key generated on the Pi, never leaves it

# laptop: deploy the contract and open the Pi's channel, then start the gateway
python scripts/m2_setup.py --device-address 0xTHE_PI_ADDRESS
python scripts/gateway_serve.py                  # --env-file .env.sepolia for the L1 baseline
```

Before each session: chrony on the Pi and a synced laptop clock (`w32tm /resync` on Windows); for the
"controlled" configuration, WiFi power save off and the Pi's CPU governor set to `performance`
(it resets on reboot). The runner records both per row.

```bash
python experiments/runner.py --transport eth  --n 30   # one CSV per session in experiments/results/
python experiments/runner.py --transport wifi --n 30
python experiments/runner.py --experiment tp --transport wifi --n 30 --gap-min 0 --gap-max 0  # H2 burst

python experiments/analyze.py          # summary.csv, tests.csv, figures
python experiments/costs.py --date 2026-10-14   # mainnet-priced cost table (read-only RPC)
python experiments/throughput.py       # H2: 3-of-3 vs single-signature baseline
python scripts/h2_live.py              # H2: live attack from the gateway (expected to fail)
```

Every session is logged in `docs/measurements.md` with the commit both machines ran.

## Not included

Bluetooth transport (no adapter), energy in joules (no power meter; CPU time is reported as a proxy),
and the regulatory analysis (H3), which is in the thesis text.
