from hal.drivers.voice._internal.wakeword_focus import WakeWordFocus


def test_focus_is_active_until_its_idle_deadline_and_refreshes():
    now = [10.0]
    focus = WakeWordFocus(20, clock=lambda: now[0])

    assert not focus.is_active()
    assert focus.refresh()
    assert focus.is_active()

    now[0] = 29.9
    assert focus.is_active()
    assert focus.refresh()

    now[0] = 49.1
    assert focus.is_active()
    now[0] = 50.0
    assert not focus.is_active()


def test_zero_timeout_disables_focus():
    focus = WakeWordFocus(0)

    assert not focus.refresh()
    assert not focus.is_active()


def test_an_inferred_wake_claims_less_floor():
    now = [10.0]
    focus = WakeWordFocus(60.0, clock=lambda: now[0])

    assert focus.refresh(20.0)
    now[0] = 31.0
    assert not focus.is_active(), "a gaze window must not last a full minute"


def test_a_shorter_grant_can_never_exceed_the_configured_window():
    """The override is a cap, not a knob for enlarging the window."""
    now = [10.0]
    focus = WakeWordFocus(30.0, clock=lambda: now[0])

    focus.refresh(600.0)
    now[0] = 41.0
    assert not focus.is_active()


def test_a_short_grant_does_not_cut_back_a_window_already_open():
    """A deliberate wake mid-conversation keeps its full floor."""
    now = [10.0]
    focus = WakeWordFocus(60.0, clock=lambda: now[0])

    focus.refresh()          # full window, from a wake word
    now[0] = 15.0
    focus.refresh(20.0)      # gaze fires inside it
    now[0] = 55.0            # past the gaze window, inside the wake-word one
    assert focus.is_active()


def test_a_disabled_window_refuses_every_opener():
    focus = WakeWordFocus(0)

    assert not focus.refresh()
    assert not focus.refresh(20.0)


def test_the_gaze_allowance_is_configured_shorter_than_the_deliberate_one():
    """The config must express the intent, not merely permit it."""
    import hal.config as hal_config

    assert hal_config.GAZE_WAKE_FOCUS_S < hal_config.WAKEWORD_FOLLOWUP_TIMEOUT_S
