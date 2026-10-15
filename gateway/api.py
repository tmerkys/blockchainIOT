"""Gateway HTTP API: `POST /voucher`, `GET /health`.

The device's WiFi/Ethernet transport (`device/transport/http_client.py`) talks
to this; the Bluetooth transport will reach the same `Relayer` through
`gateway/ble_server.py`, which is why all the real work lives in the relayer
and none of it here.

Run from the repo root, so `shared/` and `gateway/` resolve as packages::

    uvicorn gateway.api:app --host 0.0.0.0 --port 8000

`--host 0.0.0.0` is what lets the Pi reach it at M2. The API is unauthenticated
on purpose: the device signature *is* the authentication, and the contract
re-checks it on-chain regardless (H2). Keep it on the lab network.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any

# Allow `python gateway/api.py` as well as `uvicorn gateway.api:app` - the
# former puts gateway/ on sys.path instead of the repo root, which hides shared/.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from hexbytes import HexBytes  # noqa: E402
from pydantic import BaseModel, Field, field_validator  # noqa: E402

from gateway.relayer import (  # noqa: E402
    Relayer,
    RelayerNotReady,
    RelayResult,
    VoucherRejected,
)
from shared.voucher import Voucher  # noqa: E402

log = logging.getLogger(__name__)

SIGNATURE_LENGTH = 65

# Spelled out rather than taken from `fastapi.status`, whose 422 constant is
# mid-rename across the versions this project pins.
HTTP_UNPROCESSABLE = 422
HTTP_UNAVAILABLE = 503


class VoucherRequest(BaseModel):
    """What the device POSTs: the voucher, its signature, and its own clock.

    `serviceRef` and the signatures travel as 0x-hex strings; `Voucher`'s
    validators turn the former back into 32 raw bytes.
    """

    voucher: Voucher
    sigDevice: str
    sigMerchant: str | None = None

    # The device's marks, echoed back untouched so one POST yields one complete
    # CSV row. The gateway never rewrites them - t0/t1 are the Pi's clock and
    # comparing them to t2+ is only valid if chrony has both boxes in sync.
    t0: float | None = Field(default=None, description="Pi: sensor event fires")
    t1: float | None = Field(default=None, description="Pi: voucher signed")

    close: bool = Field(
        default=False,
        description=(
            "Submit close() instead of settle(): pay the merchant this voucher's "
            "delta, refund the unspent deposit to the funder, and finish the "
            "channel. This is the single on-chain transaction M4 amortises 200 "
            "off-chain ticks over."
        ),
    )

    @field_validator("sigDevice", "sigMerchant")
    @classmethod
    def _validate_signature(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            raw = HexBytes(v)
        except Exception as exc:
            raise ValueError(f"signature is not valid hex: {v!r}") from exc
        if len(raw) != SIGNATURE_LENGTH:
            raise ValueError(f"signature must be {SIGNATURE_LENGTH} bytes, got {len(raw)}")
        return v


def create_app(relayer: Relayer | None = None) -> FastAPI:
    """Build the app. Pass a `Relayer` to override the environment in tests."""
    app = FastAPI(
        title="IoT L2 Micropayments - Gateway",
        version="0.1.0",
        summary="Accepts signed device vouchers and settles them on L2.",
    )

    # Built once at import: a bad RelayerConfig should fail loudly at startup,
    # not silently on the first voucher of a measurement run.
    app.state.relayer = relayer or Relayer()

    @app.middleware("http")
    async def stamp_t2(request: Request, call_next):
        """Mark t2 before FastAPI parses or validates the body.

        Stamping inside the handler would fold JSON parsing and pydantic
        validation into transport time and quietly inflate the transport term
        of the latency decomposition.
        """
        request.state.t2 = time.time()
        return await call_next(request)

    @app.exception_handler(VoucherRejected)
    async def _rejected(request: Request, exc: VoucherRejected) -> JSONResponse:
        """A voucher the contract would revert on. The device's problem, so 422."""
        log.info("rejected voucher: %s", exc.reason)
        return JSONResponse(
            status_code=HTTP_UNPROCESSABLE,
            content={"accepted": False, "submitted": False, "reason": exc.reason},
        )

    @app.exception_handler(RelayerNotReady)
    async def _not_ready(request: Request, exc: RelayerNotReady) -> JSONResponse:
        """The gateway is misconfigured. Our problem, not the device's, so 503."""
        log.error("relayer not ready: %s", exc)
        return JSONResponse(
            status_code=HTTP_UNAVAILABLE,
            content={"accepted": False, "submitted": False, "reason": str(exc)},
        )

    if app.state.relayer.config.pipeline:  # exp/concurrency; off by default
        # Registered first, so it shadows the default route below.
        _register_pipelined_voucher(app)

    @app.post("/voucher", response_model=RelayResult)
    async def post_voucher(req: VoucherRequest, request: Request) -> RelayResult:
        """Verify a signed voucher and build its settlement transaction.

        Returns 200 with `submitted=false` when the gateway is in calldata-only
        mode (`GATEWAY_SUBMIT` off): the voucher was accepted and the calldata
        built, nothing was broadcast. 422 means the voucher itself is bad.
        """
        relayer: Relayer = request.app.state.relayer
        result = relayer.relay(
            req.voucher,
            HexBytes(req.sigDevice),
            HexBytes(req.sigMerchant) if req.sigMerchant else None,
            close=req.close,
            t0=req.t0,
            t1=req.t1,
            t2=getattr(request.state, "t2", None),
        )
        # Pairs with t2 to let the device estimate the clock offset between the
        # two machines (M2). Taken here, the last gateway-side instant that can
        # still ride inside the body - serialisation and the write to the
        # socket fall outside it, which biases the estimate by microseconds and
        # is why it stays a diagnostic rather than a correction.
        result.t6 = time.time()
        return result

    @app.get("/channel/{channel}")
    async def channel(channel: str, request: Request) -> dict[str, Any]:
        """On-chain state of one channel, so the device can resume from it.

        The device has no RPC of its own and must not grow one: it signs and
        transmits, and the gateway is its only window onto the chain. What it
        needs from that window is exactly `paid` and `nonce` — the two values
        that decide whether its next voucher is monotonic.
        """
        relayer: Relayer = request.app.state.relayer
        return relayer.channel_state(channel)

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        """Liveness plus the config the run depends on.

        Hit this before every measurement session and paste the result into
        `docs/measurements.md` - `chainIdMatches` and `paymentChannel` are
        exactly the two things that, when wrong, produce a whole CSV of
        plausible-looking but invalid rows.
        """
        relayer: Relayer = request.app.state.relayer
        facts = relayer.health()
        ready = (
            facts["canSign"]
            and facts["rpcReachable"]
            and facts["chainIdMatches"]
            # Submission needs something to submit to; calldata-only mode does not.
            and (facts["contractDeployed"] or not facts["submit"])
        )
        return {
            "status": "ok" if ready else "degraded",
            "time": time.time(),
            **facts,
        }

    return app


# --- pipelining mode (exp/concurrency; GATEWAY_PIPELINE=1, off by default) ---

HTTP_BAD_GATEWAY = 502


def _register_pipelined_voucher(app: FastAPI) -> None:
    """`POST /voucher` for pipelining mode.

    The default handler is `async def` around a blocking `relay()`, so the
    event loop runs one voucher at a time — the measured one-settle-per-block
    ceiling. This one is a plain `def`, which FastAPI runs in its thread pool,
    so K devices' vouchers are relayed concurrently; the relayer's
    `NonceManager` keeps their transaction nonces apart.

    A voucher that was accepted but whose transaction failed to broadcast or
    to be mined comes back as 502 with the full `PipelinedResult` (its
    `failure` field names the status), so a device can record *why*. A mined
    but reverted settle keeps the default path's 200 + `reason`.
    """
    # Imported here so the default path's imports are unchanged.
    from gateway.relayer import FAIL_REVERTED, PipelinedResult

    @app.post("/voucher", response_model=PipelinedResult)
    def post_voucher_pipelined(req: VoucherRequest, request: Request) -> Any:
        relayer: Relayer = request.app.state.relayer
        result = relayer.relay(
            req.voucher,
            HexBytes(req.sigDevice),
            HexBytes(req.sigMerchant) if req.sigMerchant else None,
            close=req.close,
            t0=req.t0,
            t1=req.t1,
            t2=getattr(request.state, "t2", None),
        )
        result.t6 = time.time()
        if result.failure and result.failure != FAIL_REVERTED:
            return JSONResponse(status_code=HTTP_BAD_GATEWAY, content=result.model_dump(mode="json"))
        return result


app = create_app()


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8000)
