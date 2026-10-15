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
T0_MONO = SEND_MONO - 20 * MS  # sensor fired 20 ms before the POST went out
RTT = 2_400 * MS
GATEWAY_TO_RECEIPT = 300 * MS
CONFIRM_HOLD = 2_000 * MS
"""The gateway keeps the request open past the receipt while it waits for
confirmation depth. On Base Sepolia this was 2 s — large enough that treating
it as network time turned a 7 ms LAN into a reported 2010 ms."""

GATEWAY_OCCUPANCY = GATEWAY_TO_RECEIPT + CONFIRM_HOLD
NETWORK = RTT - GATEWAY_OCCUPANCY  # 100 ms of actual wire time


def _marks(*, skew_ns: int = 0) -> tuple[DeviceMarks, float, float, float]:
    """Build one exchange, with the gateway's wall clock offset by `skew_ns`.

    Returns the device's marks plus the gateway's t2/t4/t6 as float seconds,
    which is how they arrive over the wire.
    """
    send_wall = 1_789_000_000 * MS
    recv_wall = send_wall + RTT

    # The gateway sees the request 50 ms after it is sent, has the receipt
    # 300 ms later, then holds the reply for the confirmation wait — all on a
    # clock running `skew_ns` ahead of the Pi's.
    t2 = send_wall + 50 * MS + skew_ns
    t4 = t2 + GATEWAY_TO_RECEIPT
    t6 = t4 + CONFIRM_HOLD

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


def test_network_term_excludes_all_gateway_work():
    """rtt - (t6 - t2) leaves the two wire legs and HTTP framing.

    Measured to t6, not t4: the gateway's confirmation wait falls between the
    two, and booking it as network time is what produced a 2010 ms "network"
    figure on the first Base Sepolia run.
    """
    marks, t2, _, t6 = _marks()
    assert marks.network_ns(t2, t6) == NETWORK


def test_network_term_does_not_absorb_the_confirmation_wait():
    """A longer confirmation hold must not inflate the transport term."""
    marks, t2, _, t6 = _marks()
    baseline = marks.network_ns(t2, t6)
    # Same exchange, but the gateway waits ten times as long for confirmations.
    slow_t6 = t6 + 10 * CONFIRM_HOLD / 1e9
    slow = DeviceMarks(
        t_send_ns=marks.t_send_ns,
        t_send_mono_ns=marks.t_send_mono_ns,
        t4_dev_ns=marks.t4_dev_ns + 10 * CONFIRM_HOLD,
        t4_dev_mono_ns=marks.t4_dev_mono_ns + 10 * CONFIRM_HOLD,
    )
    assert slow.network_ns(t2, slow_t6) == baseline


def test_reported_latency_excludes_the_post_receipt_hold():
    """The H1 number is sensor event -> receipt, not -> reply.

    CLAUDE.md scores confirmation depth separately as t5 - t4 and keeps it out
    of the reported t4 - t0.
    """
    marks, _, t4, t6 = _marks()
    reported = marks.reported_latency_ns(T0_MONO, t4, t6)
    assert reported == (RTT + 20 * MS) - CONFIRM_HOLD


def test_reported_latency_is_immune_to_skew():
    """The headline number must not move when the clocks disagree.

    This is the M2 property in one assertion. The device-clock span and the
    gateway-clock span subtracted from it are both *durations*, which carry no
    epoch, so an offset between the two machines cancels instead of leaking
    into the reported latency. Half a second of skew — far more than a working
    NTP setup would ever show — changes nothing.
    """
    baseline, _, t4, t6 = _marks()
    expected = baseline.reported_latency_ns(T0_MONO, t4, t6)

    for skew_ms in (-500, -7, 7, 500):
        marks, t2, t4, t6 = _marks(skew_ns=skew_ms * MS)
        assert marks.reported_latency_ns(T0_MONO, t4, t6) == expected
        assert marks.network_ns(t2, t6) == NETWORK


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
