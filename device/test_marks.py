"""Tests for the M2 clock arithmetic in `transport/http_client.DeviceMarks`.

These cover the one property the whole M2 latency measurement depends on: no
reported number is a difference between the Pi's clock and the gateway's. Each
test states the skew it injects and asserts the answer did not move.
"""

from transport.http_client import DeviceMarks

MS = 1_000_000  # ns per ms

# One plausible exchange, in ns. The device sends at t=0 on its own monotonic
# clock and gets the reply 400 ms later; the gateway spent 300 ms of that on
# building, broadcasting and waiting for the receipt, so 100 ms was the wire.
SEND_MONO = 5_000 * MS
RTT = 400 * MS
GATEWAY_SPAN = 300 * MS


def _marks(*, skew_ns: int = 0) -> tuple[DeviceMarks, float, float, float]:
    """Build one exchange, with the gateway's wall clock offset by `skew_ns`.

    Returns the device's marks plus the gateway's t2/t4/t6 as float seconds,
    which is how they arrive over the wire.
    """
    send_wall = 1_789_000_000 * MS
    recv_wall = send_wall + RTT

    # The gateway sees the request 50 ms after it is sent, finishes 300 ms
    # later, and replies — all on a clock running `skew_ns` ahead of the Pi's.
    t2 = send_wall + 50 * MS + skew_ns
    t4 = t2 + GATEWAY_SPAN
    t6 = t4 + 1 * MS

    marks = DeviceMarks(
        t_send_ns=send_wall,
        t_send_mono_ns=SEND_MONO,
        t4_dev_ns=recv_wall,
        t4_dev_mono_ns=SEND_MONO + RTT,
    )
    return marks, t2 / 1e9, t4 / 1e9, t6 / 1e9


def test_rtt_is_monotonic_only():
    """The round trip never touches a wall clock, so NTP cannot move it."""
    marks, *_ = _marks(skew_ns=90 * MS)
    assert marks.rtt_ns == RTT


def test_network_term_excludes_gateway_work():
    """rtt - (t4 - t2) leaves the two wire legs and HTTP framing."""
    marks, t2, t4, _ = _marks()
    assert marks.network_ns(t2, t4) == RTT - GATEWAY_SPAN


def test_network_term_is_immune_to_skew():
    """The headline decomposition must not move when the clocks disagree.

    This is the M2 property in one assertion: `rtt` is measured wholly on the
    device and `t4 - t2` wholly on the gateway, so an offset between the two
    cancels instead of leaking into the reported transport time. Half a second
    of skew — far more than a working NTP setup would ever show — changes
    nothing.
    """
    baseline, t2, t4, _ = _marks()
    expected = baseline.network_ns(t2, t4)

    for skew_ms in (-500, -7, 7, 500):
        marks, t2, t4, _ = _marks(skew_ns=skew_ms * MS)
        assert marks.network_ns(t2, t4) == expected


def test_clock_offset_recovers_injected_skew():
    """The NTP-style estimator reports the offset it was given.

    Legs here are symmetric by construction, which is the estimator's own
    assumption; it recovers the skew to within the 1 ms the gateway spends
    between t4 and t6. It stays a diagnostic — nothing above is corrected by
    it — but it has to be roughly right to be worth logging.
    """
    for skew_ms in (-40, 0, 40):
        marks, t2, t4, t6 = _marks(skew_ns=skew_ms * MS)
        offset = marks.clock_offset_ns(t2, t6)
        assert abs(offset - skew_ms * MS) <= 2 * MS


def test_missing_gateway_marks_give_none():
    """A gateway that returned no marks yields no number, rather than a zero.

    A missing mark has to stay missing all the way into the CSV: a silent 0
    would land in the mean and there would be nothing left to notice it by.
    """
    marks, t2, t4, t6 = _marks()
    assert marks.network_ns(None, t4) is None
    assert marks.network_ns(t2, None) is None
    assert marks.clock_offset_ns(None, t6) is None
    assert marks.clock_offset_ns(t2, None) is None
