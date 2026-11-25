"""Child processes the live modes need: the gateway.

The gateway is started through the unchanged `scripts/gateway_serve.py`, so the
demo talks to exactly the gateway the measurements used. Overrides go in as
process environment variables, which `load_env` lets win over the env file.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME = REPO_ROOT / "demo" / ".runtime"


def runtime_dir() -> Path:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    return RUNTIME


def lan_ip_towards(ssh_host: str) -> str:
    """This computer's IP on the network it shares with `ssh_host` (the Pi).

    Resolves the SSH alias (e.g. magis-pi -> magis-pi.local -> 192.168.32.65),
    then asks the OS which local address it would route from. No packet is sent.
    """
    out = subprocess.run(["ssh", "-G", ssh_host], capture_output=True, text=True, timeout=10).stdout
    host = next((ln.split()[1] for ln in out.splitlines() if ln.startswith("hostname ")), ssh_host)
    try:
        ip = socket.getaddrinfo(host, 22, socket.AF_INET)[0][4][0]
    except OSError as exc:
        raise RuntimeError(f"cannot find the Pi on the network ({host}): is it on, and on the same WiFi?") from exc
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((ip, 9))
        return sock.getsockname()[0]


def start_gateway(
    env_file: Path,
    overrides: dict[str, str],
    host: str,
    port: int,
    timeout: float = 60.0,
) -> subprocess.Popen:
    """Start scripts/gateway_serve.py and wait until /health says ok."""
    env = dict(os.environ)
    env.update(overrides)
    env["PYTHONUNBUFFERED"] = "1"
    log = open(runtime_dir() / "gateway.log", "w", encoding="utf-8")  # noqa: SIM115
    proc = subprocess.Popen(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "gateway_serve.py"),
            "--env-file",
            str(env_file),
            "--host",
            host,
            "--port",
            str(port),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "127.0.0.1") else host
    url = f"http://{probe_host}:{port}/health"
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"gateway exited (code {proc.returncode}); see demo/.runtime/gateway.log")
        try:
            r = requests.get(url, timeout=2)
            if r.ok:
                last = r.json()
                if last.get("status") == "ok":
                    return proc
        except requests.RequestException:
            pass
        time.sleep(0.3)
    stop(proc)
    raise RuntimeError(f"gateway did not become healthy: {last}")


def stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
