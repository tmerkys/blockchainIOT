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

Latency note, and the reason this module stamps anything at all. From M2 the
device and the gateway are different machines with different clocks, so `t4 -
t0` across that line is a subtraction of two unrelated clocks and whatever NTP
skew sits between them. `DeviceMarks` closes that hole: this module stamps the
moment the gateway's reply lands (`t4_dev`) on the *device's* monotonic clock,
the same clock `sensor.py` stamps `t0` on. The reported H1 latency is therefore
`t4_dev - t0`, one clock, no skew term at all.

The gateway's own `t2`-`t5` still come back in the response and are still worth
having — differences *among* them share the gateway's clock and are equally
exact, which is what lets ch. 4 decompose inclusion time out of the total. The
only quantities that cross the clock boundary are `clock_offset_ns()` (an
estimate, reported as the skew sanity check CLAUDE.md asks for and never
subtracted from anything) and nothing else.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
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


@dataclass(frozen=True)
class DeviceMarks:
    """Send and receive instants for one voucher POST, on the device's clocks.

    Both clocks are kept for each instant on purpose. The monotonic pair is
    what latency is computed from — it cannot be stepped by NTP mid-run, so a
    `chrony` correction landing between `t0` and `t4_dev` cannot manufacture or
    erase milliseconds. The wall-clock pair exists only to estimate the offset
    against the gateway, which needs two clocks that mean the same thing by
    definition, and monotonic clocks do not (they count from an arbitrary
    epoch, typically boot).
    """

    t_send_ns: int
    """Wall clock, just before the request goes out."""

    t_send_mono_ns: int
    """Monotonic, same instant as `t_send_ns`."""

    t4_dev_ns: int
    """Wall clock, the moment the gateway's reply is fully received. This is
    mark `t4` as the *device* observes it: the gateway only answers once it
    holds the receipt, so the reply landing means the tx is in a block."""

    t4_dev_mono_ns: int
    """Monotonic, same instant as `t4_dev_ns`. The end of the reported span."""

    @property
    def rtt_ns(self) -> int:
        """Device-observed round trip: request out to reply in, one clock."""
        return self.t4_dev_mono_ns - self.t_send_mono_ns

    def network_ns(self, t2: float | None, t4: float | None) -> int | None:
        """Time on the wire in both directions, with no clock-skew term.

        `rtt_ns` is measured entirely on the device's clock; `t4 - t2` is
        measured entirely on the gateway's. Subtracting one span from the
        other leaves what the gateway was not doing — the two network legs
        plus HTTP framing — and the two clocks' offsets never enter, because
        neither span ever crossed between them. This is the one honest way to
        get the transport term of the decomposition out of a two-machine run.
        """
        if t2 is None or t4 is None:
            return None
        return self.rtt_ns - round((t4 - t2) * 1e9)

    def clock_offset_ns(self, t2: float | None, t6: float | None) -> int | None:
        """Estimated gateway-clock-minus-device-clock offset, NTP-style.

        The usual four-timestamp estimator: with the outbound and return legs
        assumed symmetric, the mean of (arrival - departure) in each direction
        is the offset and the asymmetry cancels. It is a diagnostic only —
        CLAUDE.md asks for the measured offset in `docs/measurements.md`, and
        a large one now invalidates nothing, because no reported number is a
        difference across the two clocks any more. It does still mean the two
        boxes disagree about when things happened, which is worth knowing.
        """
        if t2 is None or t6 is None:
            return None
        return ((round(t2 * 1e9) - self.t_send_ns) + (round(t6 * 1e9) - self.t4_dev_ns)) // 2


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
        self.last_marks: DeviceMarks | None = None

    @property
    def last_rtt_ns(self) -> int | None:
        """Round trip of the most recent POST, or None if none has completed."""
        return self.last_marks.rtt_ns if self.last_marks else None

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

        # Both clocks, at both ends of the exchange. `t4_dev` is mark t4 taken
        # here rather than on the gateway: the gateway does not answer until it
        # has the receipt, so the reply landing is the device-side observation
        # of "included in a block" — and it is on the same clock as t0, which
        # is the entire point (see the latency note at the top of this module).
        t_send_ns, t_send_mono_ns = time.time_ns(), time.monotonic_ns()
        try:
            response = self.session.post(endpoint, json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise GatewayError(f"gateway unreachable at {endpoint}: {exc}") from exc
        t4_dev_ns, t4_dev_mono_ns = time.time_ns(), time.monotonic_ns()

        # Recorded even for a rejection below: a failed attempt still gets a
        # row in the dataset (M5), and "how long did the refusal take" is a
        # real number, not a gap.
        self.last_marks = DeviceMarks(
            t_send_ns=t_send_ns,
            t_send_mono_ns=t_send_mono_ns,
            t4_dev_ns=t4_dev_ns,
            t4_dev_mono_ns=t4_dev_mono_ns,
        )

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
