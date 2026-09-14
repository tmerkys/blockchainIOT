"""HTTP transport: device -> gateway over WiFi or Ethernet.

The `Transport` implementation `device/agent.py` plugs in for the M1/M2/M3
conditions. Bluetooth (`ble_client.py`, M5) is the other implementation of the
same seam, which is why nothing chain-shaped lives here: this module knows how
to put a signed voucher on a wire and nothing else.

It owns exactly one thing beyond `requests.post` — the translation between the
device's wire format (`SignedVoucher.to_wire()`, which carries the service fact
and nanosecond marks) and the gateway's `VoucherRequest` schema (which wants
`t0`/`t1` as float seconds). Keeping that mapping here means the agent never
has to know what the gateway's JSON looks like, and the BLE transport can map
the same payload differently.

Latency note: `t2` is stamped by the gateway when the request lands, so
`t2 - t1` is transport time as the gateway sees it. This module also records a
local round-trip (`rtt_ns`) — the gateway-side sanity check CLAUDE.md asks for
against clock skew. If `t2 - t1` ever exceeds the round-trip, the two clocks
are out of step and the latency numbers from that run are not usable.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import requests

# Same flat-import convention as device/agent.py: device/ and shared/ are the
# only directories that get copied to the Pi, so imports must work without a
# repo root above them.
_HERE = Path(__file__).resolve().parent
for _path in (_HERE.parent, _HERE.parent.parent / "shared"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

if TYPE_CHECKING:
    # Type-checking only. Importing `agent` flat here while the caller has
    # already imported it as `device.agent` would load the module a second
    # time under a second name; nothing below needs it at runtime.
    from agent import SignedVoucher
    from voucher import Voucher

DEFAULT_TIMEOUT_S = 30.0
"""Generous on purpose: the gateway holds the request open until the receipt
arrives, so this has to outlast block inclusion, not just the network."""

Countersigner = Callable[["Voucher"], str]
"""Produces the merchant's signature over a voucher, as a 0x-hex string."""


class GatewayError(RuntimeError):
    """The gateway refused the voucher, or could not be reached.

    Carries the parsed response body when there was one, so the caller can
    record *why* a run failed instead of just that it did - `runner.py` has to
    write a row for a failed attempt without corrupting the dataset (M5).
    """

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class HttpTransport:
    """POSTs signed vouchers to the gateway's `/voucher` endpoint.

    `countersign` supplies the merchant's third signature. The device and the
    merchant are physically co-located in this scenario - the car is plugged
    into the charging station - so the countersignature is collected on the
    device side and travels with the voucher. It is optional here because the
    gateway will still verify and build calldata for a 2-of-3 payload; it just
    will not spend gas on one, since `settle()` is 3-of-3.
    """

    def __init__(
        self,
        url: str,
        *,
        countersign: Countersigner | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        session: requests.Session | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.countersign = countersign
        self.timeout = timeout
        # One session, so a measurement run reuses the TCP connection instead of
        # paying a fresh handshake per voucher and calling it transport latency.
        self.session = session or requests.Session()

        self.last_response: dict[str, Any] | None = None
        self.last_rtt_ns: int | None = None

    def build_request(self, signed: SignedVoucher) -> dict[str, Any]:
        """Map the device's wire payload onto the gateway's request schema."""
        wire = signed.to_wire()
        body: dict[str, Any] = {
            "voucher": wire["voucher"],
            "sigDevice": wire["sigDevice"],
            # Nanoseconds on the device, seconds on the wire: the gateway's own
            # marks come from time.time(), and one unit across the CSV is worth
            # more than the resolution being thrown away here.
            "t0": wire["marks"]["t0_ns"] / 1e9,
            "t1": wire["marks"]["t1_ns"] / 1e9,
        }
        if self.countersign is not None:
            body["sigMerchant"] = self.countersign(signed.voucher)
        return body

    def send(self, signed: SignedVoucher) -> dict[str, Any]:
        """POST one voucher and return the gateway's `RelayResult` as a dict.

        Raises `GatewayError` on a refusal or a dead gateway. A voucher that was
        accepted but not submitted is *not* an error - that is calldata-only
        mode, and the caller reads `submitted` to tell the two apart.
        """
        body = self.build_request(signed)
        endpoint = f"{self.url}/voucher"

        sent_ns = time.monotonic_ns()
        try:
            response = self.session.post(endpoint, json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise GatewayError(f"gateway unreachable at {endpoint}: {exc}") from exc
        self.last_rtt_ns = time.monotonic_ns() - sent_ns

        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text}

        if response.status_code != 200:
            reason = payload.get("reason") or payload.get("detail") or response.text
            raise GatewayError(
                f"gateway rejected the voucher ({response.status_code}): {reason}",
                status=response.status_code,
                body=payload,
            )

        self.last_response = payload
        return payload

    def health(self) -> dict[str, Any]:
        """`GET /health`. Call it before a run, not during one."""
        try:
            response = self.session.get(f"{self.url}/health", timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise GatewayError(f"gateway health check failed: {exc}") from exc
