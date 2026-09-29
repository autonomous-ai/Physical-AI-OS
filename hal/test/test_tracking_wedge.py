"""The backstop for a tracking flag left set with no owner (#312)."""

from hal.drivers.motors.tracking_wedge import TrackingWedgeWatchdog


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _watchdog(grace_s=30.0):
    clock = _Clock()
    return TrackingWedgeWatchdog(grace_s=grace_s, clock=clock), clock


def test_a_free_body_is_never_reported():
    dog, clock = _watchdog()
    for _ in range(5):
        assert dog.check(flag=False, owners=0) is None
        clock.advance(60.0)


def test_a_live_tracking_session_is_never_reported():
    dog, clock = _watchdog()
    for _ in range(5):
        assert dog.check(flag=True, owners=1) is None
        clock.advance(60.0)


def test_the_startup_gap_is_not_reported():
    # Flag set and follower acquire are milliseconds apart; not a wedge.
    dog, clock = _watchdog()
    assert dog.check(flag=True, owners=0) is None
    clock.advance(0.2)
    assert dog.check(flag=True, owners=0) is None
    assert dog.check(flag=True, owners=1) is None


def test_a_flag_with_no_owner_is_reported_once_the_grace_expires():
    dog, clock = _watchdog(grace_s=30.0)
    assert dog.check(flag=True, owners=0) is None
    clock.advance(29.0)
    assert dog.check(flag=True, owners=0) is None
    clock.advance(2.0)
    held = dog.check(flag=True, owners=0)
    assert held is not None and 30.0 <= held <= 32.0


def test_the_report_does_not_repeat_every_tick():
    # The loop ticks at fps. One ERROR line per wedge, not thirty a second.
    dog, clock = _watchdog(grace_s=30.0)
    dog.check(flag=True, owners=0)
    clock.advance(31.0)
    assert dog.check(flag=True, owners=0) is not None
    assert dog.check(flag=True, owners=0) is None


def test_an_owner_arriving_resets_the_clock():
    dog, clock = _watchdog(grace_s=30.0)
    dog.check(flag=True, owners=0)
    clock.advance(29.0)
    dog.check(flag=True, owners=1)
    clock.advance(29.0)
    assert dog.check(flag=True, owners=0) is None, "the grace restarts from here"
