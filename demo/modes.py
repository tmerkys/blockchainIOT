"""The demo's two live modes, both on Base Sepolia (from .env).

| mode   | device                          |
|--------|---------------------------------|
| pi     | Raspberry Pi over SSH           |
| laptop | this computer, a demo key       |

Both emit the same events, so the page differs only by its badge.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from eth_account import Account

from demo import procs
from demo.chain import Chain
from demo.device_flow import DEMO_PRICE_WEI_PER_KWH, DeviceSession

Emit = Callable[[dict[str, Any]], None]
REPO_ROOT = procs.REPO_ROOT

OPEN_FEE_RESERVE = 1_000_000_000_000
"""Headroom for open()'s gas and Base's L1 data fee (~0.5k gwei each, measured)."""

PER_TICK_SETTLE_GAS = 75_029
"""Steady-state settle() gas measured at M3 (docs/measurements.md); the baseline
the channel's close() is compared against, same as device/run_channel.py."""

CHAINS = {
    84532: ("Base Sepolia", "https://sepolia.basescan.org"),
    11155111: ("Ethereum Sepolia", "https://sepolia.etherscan.io"),
}


def parse_env(path: Path) -> dict[str, str]:
    """dotenv to dict, without touching os.environ (unlike scripts' load_env)."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Mode:
    key = "base"
    live = True

    def start(self, emit: Emit) -> None: ...
    def stop(self) -> None: ...
    def info(self) -> dict[str, Any]:
        return {"mode": self.key, "live": self.live}

    def pay(self, emit: Emit) -> None:
        raise NotImplementedError

    def channel_open(self, emit: Emit) -> None:
        raise NotImplementedError

    def channel_run(self, emit: Emit, ticks: int) -> None:
        raise NotImplementedError

    def attack(self, emit: Emit) -> None:
        raise NotImplementedError


# =============================================================================
# Device runners: where the device code executes.
# =============================================================================


class LocalRunner:
    """The device code runs in this process, on this computer."""

    where = "this computer"

    def __init__(self, session: DeviceSession) -> None:
        self.session = session

    @property
    def address(self) -> str:
        return self.session.address

    def pay(self, channel: str, emit: Emit) -> None:
        self.session.pay_once(channel, emit)

    def run_channel(self, channel: str, ticks: int, emit: Emit) -> None:
        self.session.run_channel(channel, ticks, emit)

    def close(self) -> None:
        pass


class PiRunner:
    """The device code runs on the Raspberry Pi; we talk to it over SSH.

    One long-lived `ssh` session running demo/pi_agent.py, fed one JSON
    command per line. The Pi decrypts its keystore once, at start-up, and its
    key never leaves it.
    """

    where = "Raspberry Pi"

    def __init__(self, ssh_host: str, gateway_url: str, contract: str, chain_id: int, emit: Emit) -> None:
        remote = (
            "cd ~/magis && exec venv/bin/python -u demo/pi_agent.py "
            f"--gateway-url {gateway_url} --contract {contract} --chain-id {chain_id} "
            f"--price {DEMO_PRICE_WEI_PER_KWH}"
        )
        self.proc = subprocess.Popen(
            ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", "ServerAliveInterval=10", ssh_host, remote],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._emit = emit
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        hello = self._next(timeout=60)
        if hello.get("type") != "hello":
            raise RuntimeError(f"Pi agentas nepasileido: {hello.get('message', hello)}")
        self.hello = hello

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                event = {"type": "log", "text": line}
            self.events.put(event)
        self.events.put({"type": "fatal", "message": "connection to the Pi closed"})

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            if line.strip():
                self._emit({"type": "log", "source": "pi", "text": line.rstrip()})

    def _next(self, timeout: float) -> dict[str, Any]:
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return {"type": "fatal", "message": f"no answer from the Pi in {timeout:.0f} s"}

    @property
    def address(self) -> str:
        return self.hello["device"]

    def _command(self, cmd: dict[str, Any], emit: Emit, timeout: float = 300) -> None:
        if self.proc.poll() is not None:
            raise RuntimeError("the SSH session to the Pi has ended; restart the mode")
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(cmd) + "\n")
        self.proc.stdin.flush()
        while True:
            event = self._next(timeout)
            if event.get("type") == "done":
                return
            if event.get("type") == "fatal":
                raise RuntimeError(event.get("message"))
            emit({**event, "source": "pi"})

    def pay(self, channel: str, emit: Emit) -> None:
        self._command({"cmd": "tick", "channel": channel}, emit)

    def run_channel(self, channel: str, ticks: int, emit: Emit) -> None:
        self._command({"cmd": "channel", "channel": channel, "ticks": ticks}, emit)

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self.proc.kill()


# =============================================================================
# Live modes: a real gateway, a real chain, a real device key.
# =============================================================================


class LiveMode(Mode):
    live = True

    def __init__(
        self,
        kind: str,
        *,
        env_file: Path,
        gateway_host: str | None = None,
        gateway_port: int = 8100,
        ssh_host: str = "magis-pi",
        rpc_url_var: str | None = None,
    ) -> None:
        assert kind in ("laptop", "pi")
        self.key = kind
        self.env_file = env_file
        self.gateway_host = gateway_host
        self.gateway_port = gateway_port
        self.ssh_host = ssh_host
        self.rpc_url_var = rpc_url_var

        self.gateway: subprocess.Popen | None = None
        self.runner: LocalRunner | PiRunner | None = None
        self.chain: Chain | None = None
        self.state: dict[str, Any] = {}
        self.state_path: Path | None = None

    # --- setup ----------------------------------------------------------------

    def start(self, emit: Emit) -> None:
        log = lambda text: emit({"type": "log", "text": text})  # noqa: E731

        env = parse_env(self.env_file)
        rpc_url = env.get(self.rpc_url_var, "") if self.rpc_url_var else env.get("RPC_URL", "")
        if not rpc_url:
            raise RuntimeError(f"{self.env_file.name} has no RPC_URL")
        chain_id = int(env["CHAIN_ID"])
        if chain_id == 1:
            raise RuntimeError("mainnet is never used (CLAUDE.md)")
        keys = {
            "gateway": env["GATEWAY_PRIVATE_KEY"],
            "merchant": env["MERCHANT_PRIVATE_KEY"],
            # The EV owner who escrows deposits. DEMO_FUNDER_PRIVATE_KEY is read only by
            # the demo, so the measurement scripts' FUNDER_PRIVATE_KEY stays as it was.
            "funder": env.get("DEMO_FUNDER_PRIVATE_KEY") or env.get("FUNDER_PRIVATE_KEY") or env["GATEWAY_PRIVATE_KEY"],
        }
        self.chain = Chain(rpc_url, chain_id, env["PAYMENT_CHANNEL_ADDRESS"])
        # Testnet ETH is scarce. At the M4 price 200 ticks cost 7.8e12 wei, and one
        # settled tick 3.9e10, so these cover one channel demo and ~50 payments.
        self.deposits = {"tick": 2_000_000_000_000, "demo": 9_000_000_000_000}

        self.keys = keys
        self.funder = Account.from_key(keys["funder"])
        self.gateway_addr = Account.from_key(keys["gateway"]).address
        self.merchant_addr = Account.from_key(keys["merchant"]).address

        if self.key == "pi":
            device_addr = env.get("DEVICE_ADDRESS")
            if not device_addr:
                raise RuntimeError(".env has no DEVICE_ADDRESS (the Pi's address)")
            if not self.gateway_host:
                log(f"finding this computer's IP on the network shared with {self.ssh_host}")
                self.gateway_host = procs.lan_ip_towards(self.ssh_host)
            bind_host = self.gateway_host
        else:
            keyfile = procs.runtime_dir() / "laptop-device.key"
            if not keyfile.exists():
                keyfile.write_text(Account.create().key.hex(), encoding="utf-8")
            keys["device"] = keyfile.read_text(encoding="utf-8").strip()
            device_addr = Account.from_key(keys["device"]).address
            bind_host = "127.0.0.1"

        overrides = {
            "RPC_URL": rpc_url,
            "DEVICE_ADDRESS": device_addr,
            "MERCHANT_ADDRESS": self.merchant_addr,
            "GATEWAY_SUBMIT": "1",
            "CONFIRMATIONS": "1",
            "GATEWAY_PIPELINE": "0",
        }
        log(f"starting the gateway on {bind_host}:{self.gateway_port}")
        self.gateway = procs.start_gateway(self.env_file, overrides, bind_host, self.gateway_port)
        self.gateway_url = f"http://{bind_host}:{self.gateway_port}"

        if self.key == "pi":
            log(f"connecting to the Pi over SSH; it will send to {self.gateway_url}")
            self.runner = PiRunner(self.ssh_host, self.gateway_url, self.chain.address, chain_id, emit)
            emit({"type": "pi.hello", **self.runner.hello})
            if self.runner.address != Web3checksum(device_addr):
                raise RuntimeError(f"the Pi's key is {self.runner.address} but .env DEVICE_ADDRESS is {device_addr}")
        else:
            self.runner = LocalRunner(
                DeviceSession(
                    account=Account.from_key(keys["device"]),
                    contract=self.chain.address,
                    chain_id=chain_id,
                    gateway_url=self.gateway_url,
                    merchant_key=keys["merchant"],
                )
            )

        self.state_path = procs.runtime_dir() / (
            f"state-{chain_id}-{self.chain.address[2:10]}-{self.runner.address[2:10]}.json"
        )
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self._ensure_channel("tick", emit, fresh=False)
        self._ensure_channel("demo", emit, fresh=True)

    def _save(self) -> None:
        assert self.state_path is not None
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def _open(self, which: str, emit: Emit, where: str) -> None:
        assert self.chain is not None and self.runner is not None
        need = self.deposits[which] + OPEN_FEE_RESERVE
        have = self.chain.balance(self.funder.address)
        if have < need:
            raise RuntimeError(
                f"not enough testnet ETH to open a channel: the funder has {have // 10**9:,} gwei, "
                f"needs {need // 10**9:,} gwei (deposit + fees). Top up {self.funder.address} from a faucet."
            )
        emit({"type": "log", "text": f"opening the {which} channel: open(), deposit {self.deposits[which] // 10**9:,} gwei"})
        info = self.chain.open(self.funder, self.runner.address, self.gateway_addr, self.merchant_addr,
                               self.deposits[which])
        info["where"] = where
        info["openedAt"] = time.strftime("%Y-%m-%d %H:%M")
        self.state[which] = info
        self._save()

    def _ensure_channel(self, which: str, emit: Emit, *, fresh: bool) -> None:
        """Reuse the saved channel if it is still usable, else open a new one."""
        assert self.chain is not None and self.runner is not None
        saved = self.state.get(which)
        if saved:
            st = self.chain.channel(saved["channel"])
            usable = (
                st["device"] == self.runner.address
                and not st["closed"]
                and st["deposit"] - st["paid"] >= self.deposits[which] // 10
                and (not fresh or st["nonce"] == 0)
            )
            if usable and st["funder"] != self.funder.address:
                # Every deposit comes from the EV owner, so both tabs tell the same story.
                emit({"type": "log", "text": f"{which} channel {saved['channel'][:10]}… was funded by "
                      f"{st['funder'][:10]}…, not the EV owner; opening a new one"})
                usable = False
            if usable:
                return
        self._open(which, emit, "before")

    # --- info -------------------------------------------------------------------

    def _channel_info(self, which: str) -> dict[str, Any] | None:
        saved = self.state.get(which)
        if not saved or self.chain is None:
            return None
        st = self.chain.channel(saved["channel"])
        return {**saved, "deposit": str(st["deposit"]), "paid": str(st["paid"]), "nonce": st["nonce"],
                "closed": st["closed"]}

    def info(self) -> dict[str, Any]:
        base = super().info()
        if self.chain is None or self.runner is None:
            return base
        name, explorer = CHAINS.get(self.chain.chain_id, (f"chain {self.chain.chain_id}", None))
        out = {
            **base,
            "chainName": name,
            "chainId": self.chain.chain_id,
            "explorer": explorer,
            "contract": self.chain.address,
            "device": self.runner.address,
            "deviceWhere": self.runner.where,
            "gateway": self.gateway_addr,
            "funder": self.funder.address,
            "merchant": self.merchant_addr,
            "settleGas": PER_TICK_SETTLE_GAS,
            "demoDeposit": str(self.deposits["demo"] + OPEN_FEE_RESERVE),
        }
        try:
            out["gatewayBalance"] = str(self.chain.balance(self.gateway_addr))
            out["funderBalance"] = str(self.chain.balance(self.funder.address))
            out["merchantBalance"] = str(self.chain.balance(self.merchant_addr))
            out["tickChannel"] = self._channel_info("tick")
            out["demoChannel"] = self._channel_info("demo")
        except Exception as exc:  # noqa: BLE001 - an RPC hiccup should not blank the page
            out["infoError"] = str(exc)
        if isinstance(self.runner, PiRunner):
            out["pi"] = self.runner.hello
        return out

    # --- actions ----------------------------------------------------------------

    def pay(self, emit: Emit) -> None:
        assert self.runner is not None
        self._ensure_channel("tick", emit, fresh=False)
        channel = self.state["tick"]["channel"]
        before = self.chain.channel(channel)
        self.runner.pay(channel, emit)
        after = self.chain.channel(channel)
        if after["paid"] > before["paid"]:
            # The payment leaves the EV owner's escrowed deposit for the merchant.
            emit({"type": "pay.money", "funder": after["funder"], "merchant": after["merchant"],
                  "funderIsGateway": after["funder"] == self.gateway_addr,
                  "amount": str(after["paid"] - before["paid"]), "left": str(after["deposit"] - after["paid"])})

    def channel_open(self, emit: Emit) -> None:
        self._open("demo", emit, "live")
        info = self._channel_info("demo")
        emit({"type": "ch.opened", **(info or {})})

    def channel_run(self, emit: Emit, ticks: int) -> None:
        assert self.runner is not None
        self._ensure_channel("demo", emit, fresh=True)
        channel = self.state["demo"]["channel"]
        merchant_before = self.chain.balance(self.merchant_addr)
        gateway_before = self.chain.balance(self.gateway_addr)
        self.runner.run_channel(channel, ticks, emit)
        st = self.chain.channel(channel)
        if not st["closed"]:
            return  # the run failed; its error is already on the page
        # Who paid whom, from the chain: the deposit's owner, the merchant, the gateway.
        emit({
            "type": "ch.money",
            "funder": st["funder"],
            "funderIsGateway": st["funder"] == self.gateway_addr,
            "deposit": str(st["deposit"]),
            "paid": str(st["paid"]),
            "refund": str(st["deposit"] - st["paid"]),
            "openTx": self.state["demo"].get("txHash"),
            "merchant": self.merchant_addr,
            "merchant_before": str(merchant_before),
            "merchant_after": str(self.chain.balance(self.merchant_addr)),
            "gateway": self.gateway_addr,
            "gateway_before": str(gateway_before),
            "gateway_after": str(self.chain.balance(self.gateway_addr)),
        })

    def attack(self, emit: Emit) -> None:
        assert self.chain is not None
        self._ensure_channel("tick", emit, fresh=False)
        self.chain.attack(
            channel=self.state["tick"]["channel"],
            gateway_key=self.keys["gateway"],
            merchant_key=self.keys["merchant"],
            gateway_url=self.gateway_url,
            emit=emit,
        )

    def stop(self) -> None:
        if self.runner is not None:
            self.runner.close()
        procs.stop(self.gateway)
        self.runner = self.gateway = None


def Web3checksum(address: str) -> str:
    from web3 import Web3

    return Web3.to_checksum_address(address)
