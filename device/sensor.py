"""Simulated kWh meter — the service-event source for the device agent.

Stands in for the energy meter of an EV charging station. Produces one
`SensorEvent` per charging tick, each carrying the cumulative energy delivered
in the session so far. `t0` (the first instrumentation mark in CLAUDE.md) is
stamped here, at the instant the event fires, on the Pi's clock.

Energy is carried as an integer count of milliwatt-hours, never a float:
`serviceRef` is a keccak hash over the raw numbers, and device, gateway and
merchant have to hash byte-identical input to agree on it. Floats do not
survive that requirement across languages or JSON round-trips.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

from eth_utils import keccak

# Milliwatt-hours per kWh. 1 mWh over a 2 s tick of a 50 kW charger is ~0.004%
# of the tick, so the integer rounding below is far below meter accuracy.
MWH_PER_KWH = 1_000_000

SECONDS_PER_HOUR = 3600

UINT64_MAX = 2**64 - 1
UINT256_MAX = 2**256 - 1


def service_ref(session_id: str, cumulative_mwh: int, timestamp: int) -> bytes:
    """keccak256(sessionId ‖ kWh ‖ timestamp) — the service fact, as 32 bytes.

    Every component is encoded to a fixed width so the concatenation is
    unambiguous (a variable-length session id would let two different facts
    collide onto the same preimage):

        keccak256(session_id)        32 bytes
        cumulative_mwh as uint256    32 bytes
        timestamp as uint64           8 bytes

    Cumulative — not per-tick — energy is bound here on purpose: it is the
    quantity the voucher's `cumulativeAmount` is derived from, so anyone
    holding the service fact can recompute the amount and check it.
    """
    if not session_id:
        raise ValueError("session_id must be a non-empty string")
    if not 0 <= cumulative_mwh <= UINT256_MAX:
        raise ValueError(f"cumulative_mwh {cumulative_mwh} out of uint256 range")
    if not 0 <= timestamp <= UINT64_MAX:
        raise ValueError(f"timestamp {timestamp} out of uint64 range")

    return keccak(
        keccak(text=session_id)
        + cumulative_mwh.to_bytes(32, "big")
        + timestamp.to_bytes(8, "big")
    )


@dataclass(frozen=True)
class SensorEvent:
    """One charging tick: a metered energy reading plus its `t0` mark."""

    session_id: str
    sequence: int
    """1-based index of this tick within the session."""

    energy_mwh: int
    """Energy delivered by this tick alone."""

    cumulative_mwh: int
    """Energy delivered by the session so far, including this tick."""

    timestamp: int
    """Wall-clock second the reading is attributed to; part of `serviceRef`."""

    t0_ns: int
    """Mark t0 — wall clock (`time.time_ns`), comparable to the gateway's marks
    only as far as chrony keeps the two hosts in step. See CLAUDE.md."""

    t0_mono_ns: int
    """Mark t0 on the monotonic clock. Immune to NTP steps, so device-local
    deltas (t1 - t0, the signing cost) should be computed from this pair."""

    service_ref: bytes = field(init=False)

    def __post_init__(self) -> None:
        if self.energy_mwh <= 0:
            raise ValueError("energy_mwh must be positive, else cumulative energy stalls")
        if self.cumulative_mwh < self.energy_mwh:
            raise ValueError("cumulative_mwh cannot be smaller than this tick's energy")
        object.__setattr__(
            self,
            "service_ref",
            service_ref(self.session_id, self.cumulative_mwh, self.timestamp),
        )

    @property
    def kwh(self) -> float:
        """This tick's energy in kWh. Display only — never hash or price this."""
        return self.energy_mwh / MWH_PER_KWH

    @property
    def cumulative_kwh(self) -> float:
        """Session energy in kWh. Display only — never hash or price this."""
        return self.cumulative_mwh / MWH_PER_KWH


class KwhMeter:
    """Simulated meter of a charging station, one session at a time.

    Deterministic when constructed with a `seed`, which is what the experiment
    runner wants: the same seed replays the same energy series, so a latency
    difference between conditions cannot be an artefact of different amounts.
    """

    def __init__(
        self,
        *,
        power_kw: float = 50.0,
        tick_seconds: float = 2.0,
        jitter: float = 0.05,
        seed: int | None = None,
    ) -> None:
        if power_kw <= 0:
            raise ValueError("power_kw must be positive")
        if tick_seconds <= 0:
            raise ValueError("tick_seconds must be positive")
        if not 0 <= jitter < 1:
            raise ValueError("jitter must be in [0, 1)")

        self.power_kw = power_kw
        self.tick_seconds = tick_seconds
        self.jitter = jitter

        # Local RNG, not the global one: seeding `random` globally would make
        # the meter's determinism depend on whatever else the process draws.
        self._rng = random.Random(seed)

        self.session_id: str | None = None
        self.cumulative_mwh = 0
        self.sequence = 0

    def start_session(self, session_id: str | None = None) -> str:
        """Begin a new charging session, resetting cumulative energy to zero."""
        self.session_id = session_id or uuid.uuid4().hex
        self.cumulative_mwh = 0
        self.sequence = 0
        return self.session_id

    def tick(self, *, elapsed_seconds: float | None = None) -> SensorEvent:
        """Fire one service event and stamp `t0`.

        `elapsed_seconds` overrides the nominal tick length, for replaying a
        recorded session or for testing.
        """
        if self.session_id is None:
            self.start_session()
        assert self.session_id is not None  # narrowed for type checkers

        elapsed = self.tick_seconds if elapsed_seconds is None else elapsed_seconds
        if elapsed <= 0:
            raise ValueError("elapsed_seconds must be positive")

        factor = 1.0 + self._rng.uniform(-self.jitter, self.jitter)
        energy_mwh = round(self.power_kw * (elapsed / SECONDS_PER_HOUR) * MWH_PER_KWH * factor)
        # A tick that rounds to zero would leave cumulativeAmount flat, which
        # the contract rejects as non-monotonic. Charge for the smallest unit.
        energy_mwh = max(energy_mwh, 1)

        self.cumulative_mwh += energy_mwh
        self.sequence += 1

        return SensorEvent(
            session_id=self.session_id,
            sequence=self.sequence,
            energy_mwh=energy_mwh,
            cumulative_mwh=self.cumulative_mwh,
            timestamp=int(time.time()),
            t0_ns=time.time_ns(),
            t0_mono_ns=time.monotonic_ns(),
        )

    def stream(self, ticks: int | None = None, *, realtime: bool = False) -> Iterator[SensorEvent]:
        """Yield events, optionally pacing them at `tick_seconds` wall time.

        `realtime=True` is what M4's 200-tick channel run wants; the default
        runs as fast as the consumer can take them, for unit tests.
        """
        emitted = 0
        while ticks is None or emitted < ticks:
            if realtime and emitted:
                time.sleep(self.tick_seconds)
            yield self.tick()
            emitted += 1

    @property
    def cumulative_kwh(self) -> float:
        """Display only — never hash or price this."""
        return self.cumulative_mwh / MWH_PER_KWH
