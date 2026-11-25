"""Demo web server: one page, actions over POST, events over server-sent events.

    python -m demo                  # Base Sepolia, the Raspberry Pi signs
    python -m demo --mode laptop    # Base Sepolia, device code on this computer

Then open http://127.0.0.1:8080.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from demo.modes import LiveMode, Mode  # noqa: E402
from shared.rpc import redact  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
ANALYSIS = REPO_ROOT / "experiments" / "analysis"
MODES = ("pi", "laptop")


class Hub:
    """Fans events out from worker threads to every open page."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queues: set[asyncio.Queue] = set()
        self.history: deque[dict[str, Any]] = deque(maxlen=400)
        self.lock = threading.Lock()

    def publish(self, event: dict[str, Any]) -> None:
        event = {"ts": time.time(), **event}
        with self.lock:
            self.history.append(event)
            queues = list(self.queues)
        if self.loop is None:
            return
        for q in queues:
            self.loop.call_soon_threadsafe(q.put_nowait, event)


def reference_rows() -> list[dict[str, Any]]:
    """Thesis latency results per condition, from results-v1's summary.csv."""
    path = ANALYSIS / "summary.csv"
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["metric"] != "latency_ms":
                continue
            rows.append({
                "chain": r["chain"], "transport": r["transport"], "config": r["config"],
                "n": int(r["n"]), "mean": float(r["mean"]), "sd": float(r["sd"]),
                "ci95": float(r["ci95"]), "median": float(r["median"]),
            })
    return rows


class Demo:
    def __init__(self, args: argparse.Namespace, hub: Hub) -> None:
        self.args = args
        self.hub = hub
        self.mode: Mode | None = None
        self.mode_key = args.mode
        self.busy: str | None = None
        self.status = "starting"
        self.error: str | None = None
        self.lock = threading.Lock()
        self.redact_url: str | None = None

    def emit_for(self, flow: str):
        def emit(event: dict[str, Any]) -> None:
            if "message" in event and isinstance(event["message"], str):
                event = {**event, "message": redact(event["message"], self.redact_url) or event["message"]}
            self.hub.publish({"flow": flow, **event})
        return emit

    def make_mode(self, key: str) -> Mode:
        a = self.args
        env_file = Path(a.env_file)
        if key == "pi":
            return LiveMode("pi", env_file=env_file, gateway_host=a.gateway_host,
                            gateway_port=a.pi_gateway_port, ssh_host=a.ssh_host, rpc_url_var=a.rpc_url_var)
        return LiveMode(key, env_file=env_file, gateway_port=a.gateway_port, rpc_url_var=a.rpc_url_var)

    def _run(self, name: str, fn) -> None:
        emit = self.emit_for(name)
        self.hub.publish({"flow": "busy", "type": "busy", "busy": name})
        try:
            fn(emit)
        except Exception as exc:  # noqa: BLE001 - shown on the page, not lost
            emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            with self.lock:
                self.busy = None
            self.hub.publish({"flow": "busy", "type": "busy", "busy": None})
            self.publish_state()

    def start_action(self, name: str, fn) -> None:
        with self.lock:
            if self.busy:
                raise HTTPException(409, f"busy: {self.busy}")
            self.busy = name
        threading.Thread(target=self._run, args=(name, fn), daemon=True).start()

    def switch(self, key: str) -> None:
        def do(emit) -> None:
            if self.mode is not None:
                emit({"type": "log", "text": f"stopping mode {self.mode.key}"})
                self.mode.stop()
                self.mode = None
            self.mode_key = key
            self.status = "starting"
            self.error = None
            self.publish_state()
            mode = self.make_mode(key)
            try:
                mode.start(emit)
            except Exception as exc:
                mode.stop()
                self.status = "failed"
                self.error = redact(str(exc), self.redact_url) or str(exc)
                raise
            if isinstance(mode, LiveMode) and mode.chain is not None:
                self.redact_url = mode.chain.rpc_url
            self.mode = mode
            self.status = "ready"
            emit({"type": "ready", "mode": key})
        self.start_action("mode", do)

    def state(self) -> dict[str, Any]:
        info: dict[str, Any] = {"mode": self.mode_key}
        if self.mode is not None:
            info = self.mode.info()
        return {
            "status": self.status,
            "error": self.error,
            "busy": self.busy,
            "modes": list(MODES),
            "info": info,
        }

    def publish_state(self) -> None:
        try:
            self.hub.publish({"flow": "state", "type": "state", "state": self.state()})
        except Exception as exc:  # noqa: BLE001
            self.hub.publish({"flow": "state", "type": "log", "text": f"state refresh failed: {exc}"})

    def stop(self) -> None:
        if self.mode is not None:
            self.mode.stop()


def create_app(args: argparse.Namespace) -> FastAPI:
    hub = Hub()
    demo = Demo(args, hub)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.loop = asyncio.get_running_loop()
        demo.switch(args.mode)
        yield
        demo.stop()  # stops the gateway, anvil and the Pi session with the server

    app = FastAPI(title="IoT L2 micropayments: defence demo", lifespan=lifespan)
    app.state.demo = demo

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        # The page changes between rehearsals; a cached app.js would show stale behaviour.
        response = await call_next(request)
        if not request.url.path.startswith("/api/events"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return await asyncio.to_thread(demo.state)

    @app.get("/api/history")
    async def history() -> list[dict[str, Any]]:
        """Recent events, for the page's ?snapshot view (screenshots, no live stream)."""
        with hub.lock:
            return list(hub.history)

    @app.get("/api/reference")
    async def reference() -> dict[str, Any]:
        return {"latency": reference_rows()}

    @app.post("/api/mode/{key}")
    async def mode(key: str) -> dict[str, Any]:
        if key not in MODES:
            raise HTTPException(400, f"unknown mode {key}")
        demo.switch(key)
        return {"ok": True}

    def require_ready() -> Mode:
        if demo.mode is None or demo.status != "ready":
            raise HTTPException(409, "the mode is not ready yet")
        return demo.mode

    @app.post("/api/pay")
    async def pay() -> dict[str, Any]:
        m = require_ready()
        demo.start_action("payment", m.pay)
        return {"ok": True}

    @app.post("/api/channel/open")
    async def channel_open() -> dict[str, Any]:
        m = require_ready()
        demo.start_action("channel", m.channel_open)
        return {"ok": True}

    @app.post("/api/channel/run")
    async def channel_run(ticks: int = 200) -> dict[str, Any]:
        m = require_ready()
        ticks = max(10, min(ticks, 500))
        demo.start_action("channel", lambda emit: m.channel_run(emit, ticks))
        return {"ok": True}

    @app.post("/api/attack")
    async def attack() -> dict[str, Any]:
        m = require_ready()
        demo.start_action("attack", m.attack)
        return {"ok": True}

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        q: asyncio.Queue = asyncio.Queue()
        with hub.lock:
            hub.queues.add(q)

        async def stream():
            try:
                snapshot = await asyncio.to_thread(demo.state)
                yield f"data: {json.dumps({'flow': 'state', 'type': 'state', 'state': snapshot})}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(event, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                with hub.lock:
                    hub.queues.discard(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Defence demo server (presentation aid, not a measurement).")
    p.add_argument("--mode", choices=MODES, default="pi")
    p.add_argument("--host", default="127.0.0.1", help="where the demo page listens")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--env-file", default=str(REPO_ROOT / ".env"), help="gateway env for laptop/pi modes")
    p.add_argument("--rpc-url-var", default=None, help="take the RPC URL from this env variable instead")
    p.add_argument("--gateway-port", type=int, default=8100, help="gateway port for laptop mode")
    p.add_argument("--gateway-host", default=None,
                   help="pi mode: this computer's IP on the Pi's network (found automatically if omitted)")
    p.add_argument("--pi-gateway-port", type=int, default=8000,
                   help="pi mode gateway port (8000 already has a firewall rule from M2)")
    p.add_argument("--ssh-host", default="magis-pi")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    import uvicorn

    print(f"  demo: http://{args.host}:{args.port}   (mode: {args.mode})")
    uvicorn.run(create_app(args), host=args.host, port=args.port, log_level="warning")
    return 0
