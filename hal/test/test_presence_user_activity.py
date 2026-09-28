"""User interaction keeps presence alive, not only the camera.

The idle→away timer used to hear only face / motion / emotion perception. A user
talking to the device with the camera off, or sitting outside the frame, timed
out to AWAY after AWAY_TIMEOUT_S and the device announced sleep mid-conversation.
Voice turns and physical gestures now reset the same clock via on_activity().
"""

import time
from unittest import mock

import hal.config as config
from hal.drivers.sensing.presence_service import PresenceState, PresenseService
from hal.presets import RGB_CMD_SOLID


def _service(rgb=None, enabled=True):
    svc = PresenseService(rgb_service=rgb or mock.Mock(), send_event=mock.Mock(),
                          auto_enabled=enabled)
    svc._last_color = (10, 20, 30)
    svc._light_is_off = lambda: False
    svc._is_guard_mode = lambda: False
    svc._is_sleeping = lambda: False
    return svc


def _age(svc, seconds):
    svc._last_motion_time = time.time() - seconds


def test_activity_resets_the_away_clock():
    svc = _service()
    _age(svc, config.AWAY_TIMEOUT_S - 1)

    svc.on_activity("voice_command")
    svc.tick()

    assert svc.state == PresenceState.PRESENT
    assert not svc._send_event.called


def test_without_activity_the_device_still_goes_away():
    # The camera-only path is unchanged: nobody seen, nobody talking → AWAY.
    svc = _service()
    _age(svc, config.IDLE_TIMEOUT_S + 1)
    svc.tick()
    _age(svc, config.AWAY_TIMEOUT_S + 1)
    svc.tick()

    assert svc.state == PresenceState.AWAY
    svc._send_event.assert_called_once()
    assert svc._send_event.call_args.args[0] == "presence.away"


def test_activity_while_dimmed_brings_the_light_back():
    rgb = mock.Mock()
    svc = _service(rgb)
    svc._state = PresenceState.IDLE

    svc.on_activity("button")

    assert svc.state == PresenceState.PRESENT
    rgb.dispatch.assert_called_once_with(RGB_CMD_SOLID, (10, 20, 30))


def test_activity_on_a_sleeping_device_leaves_the_strip_to_sleep():
    # A tap reaches presence before its wake runs; relighting here would paint
    # over sleep's dark strip.
    rgb = mock.Mock()
    svc = _service(rgb)
    svc._is_sleeping = lambda: True
    svc._state = PresenceState.AWAY

    svc.on_activity("button")

    assert svc.state == PresenceState.PRESENT
    assert not rgb.dispatch.called


def test_activity_does_not_enable_a_disabled_machine():
    # A device without `presence` starts disabled on purpose; talking to it
    # must not switch the auto-off timer on.
    svc = _service(enabled=False)

    svc.on_activity("voice")

    assert svc.state == PresenceState.DISABLED
    assert not svc.enabled


def test_note_user_activity_reaches_presence_and_never_raises():
    import hal.app_state as state

    presence = mock.Mock()
    with mock.patch.object(state, "sensing_service", mock.Mock(presence=presence)):
        state.note_user_activity("touch")
    presence.on_activity.assert_called_once_with("touch")

    presence.on_activity.side_effect = RuntimeError("boom")
    with mock.patch.object(state, "sensing_service", mock.Mock(presence=presence)):
        state.note_user_activity("touch")

    with mock.patch.object(state, "sensing_service", None):
        state.note_user_activity("touch")


# --- Sleep and wake ---------------------------------------------------------


def test_wake_restarts_the_countdown_even_past_away():
    # Woken by the web UI / API / an agent reply: nobody on camera, and the
    # pre-sleep timestamp is already past AWAY. It must not announce sleep again.
    svc = _service()
    _age(svc, config.AWAY_TIMEOUT_S + 60)

    svc.on_wake()
    svc.tick()

    assert svc.state == PresenceState.PRESENT
    assert not svc._send_event.called


def test_wake_out_of_away_does_not_repaint_the_strip():
    rgb = mock.Mock()
    svc = _service(rgb)
    svc._state = PresenceState.AWAY

    svc.on_wake()

    assert svc.state == PresenceState.PRESENT
    assert not rgb.dispatch.called


def test_the_clock_stands_still_while_asleep():
    # Asleep past both thresholds: no dim over the user's colour, no light off,
    # no presence.away, and the state is not left AWAY for the wake to inherit.
    rgb = mock.Mock()
    svc = _service(rgb)
    svc._is_sleeping = lambda: True
    _age(svc, config.AWAY_TIMEOUT_S + 60)

    svc.tick()
    svc.tick()

    assert svc.state == PresenceState.PRESENT
    assert not rgb.dispatch.called
    assert not rgb.clear.called
    assert not svc._send_event.called


def test_wake_does_not_enable_a_disabled_machine():
    svc = _service(enabled=False)

    svc.on_wake()

    assert svc.state == PresenceState.DISABLED


def test_note_presence_wake_reaches_presence_and_never_raises():
    import hal.app_state as state

    presence = mock.Mock()
    with mock.patch.object(state, "sensing_service", mock.Mock(presence=presence)):
        state.note_presence_wake()
    presence.on_wake.assert_called_once_with()

    presence.on_wake.side_effect = RuntimeError("boom")
    with mock.patch.object(state, "sensing_service", mock.Mock(presence=presence)):
        state.note_presence_wake()

    with mock.patch.object(state, "sensing_service", None):
        state.note_presence_wake()


def test_every_wake_through_express_emotion_resets_presence():
    # express_emotion is where the button, web UI, API and agent wakes meet.
    import hal.app_state as state
    from hal.models import EmotionRequest
    from hal.routes import emotion

    presence = mock.Mock()
    with mock.patch.object(state, "_sleeping", True), \
            mock.patch.object(state, "sensing_service", mock.Mock(presence=presence)), \
            mock.patch.object(state, "_persist_sleep_state"), \
            mock.patch.object(state, "_log_sleep_transition"), \
            mock.patch.object(state, "_wake_sleepy_peripherals"):
        emotion.express_emotion(EmotionRequest(emotion="stretching"), source="test")
        assert state._sleeping is False

    presence.on_wake.assert_called_once_with()


def test_an_emotion_while_awake_is_not_a_wake():
    import hal.app_state as state
    from hal.models import EmotionRequest
    from hal.routes import emotion

    presence = mock.Mock()
    with mock.patch.object(state, "_sleeping", False), \
            mock.patch.object(state, "sensing_service", mock.Mock(presence=presence)):
        emotion.express_emotion(EmotionRequest(emotion="greeting"), source="test")

    assert not presence.on_wake.called
