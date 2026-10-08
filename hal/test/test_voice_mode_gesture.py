"""Configurable touch mode toggles without network, audio, or I2C hardware."""

from types import SimpleNamespace
from unittest import mock

import pytest
import requests

from hal import config
import hal.app_state as state
from hal.board.mpr121 import MPR121Config
from hal.drivers import voice_mode_gesture as actions
from hal.drivers.mpr121 import MPR121Handler, _GestureEvent


@pytest.fixture
def allowed(monkeypatch):
    for name in ("_sleeping", "_enrolling", "_hw_mic_switch_muted"):
        monkeypatch.setattr(state, name, False)
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "automatic", raising=False)
    return {"enabled": False}


@pytest.mark.parametrize("values", [
    {"hold_action_s": True}, {"hold_action_s": float("nan")}, {"hold_action_s": .1},
    {"hold_action_s": 11}, {"gesture_actions": {"single": "toggle_voice_input_mode"}},
    {"gesture_actions": {"hold": "invalid"}}, {"gesture_actions": []},
])
def test_invalid_config(values):
    with pytest.raises(ValueError):
        MPR121Config(bus=0, **values)


def handler_for(snapshot, **kwargs):
    handler = MPR121Handler(MPR121Config(bus=0, debounce_ms=0, **kwargs))
    handler._harness_gestures = SimpleNamespace(snapshot=snapshot, mode_key=lambda: (False,),
                                               refresh_matches=lambda key: True)
    handler._mode_key = (False,)
    handler._detector = handler._new_detector()
    return handler


def test_release_hold_once_and_short_tap_immediate(allowed):
    handler = handler_for(allowed, gesture_actions={"hold": "toggle_voice_input_mode"})
    detector = handler._detector
    events = []
    for sample in [(False, 0), (True, 1), (True, 3), (True, 4)]:
        events += detector.update(*sample)
    assert not any(e.kind in ("hold", "single") for e in events)
    assert sum(e.kind == "hold_tier" for e in events) == 1
    released = detector.update(False, 4.1)
    assert [e.kind for e in released if e.kind in ("hold", "single")] == ["hold"]
    assert not detector.update(False, 5)
    detector.update(True, 6)
    assert "single" in [e.kind for e in detector.update(False, 6.1)]


def test_hot_mode_change_reseeds_held_contact(allowed, monkeypatch):
    handler = handler_for(allowed, gesture_actions={"hold": "toggle_voice_input_mode"})
    handler._process_touch(False, 0)
    handler._process_touch(True, 1)
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "tap_to_talk")
    handler._process_touch(True, 2)
    handler._process_touch(False, 4)
    assert handler._pending.empty()
    handler._process_touch(True, 5)
    handler._process_touch(False, 5.1)
    assert handler._pending.get_nowait()[1].kind == "single"


def test_remapped_swipe_replaces_default_action(allowed):
    handler = handler_for(allowed, gesture_actions={"swipe_left": "toggle_voice_input_mode"})
    with mock.patch.object(actions, "ModeToggleWorker") as worker, mock.patch.object(handler, "_execute_swipe") as swipe:
        handler._execute(_GestureEvent("swipe", 1, direction=1))
    worker.return_value.submit.assert_called_once_with()
    swipe.assert_not_called()


@pytest.mark.parametrize("snapshot", [None, {"enabled": True}, {"enabled": False, "unavailable": True}])
def test_unknown_or_harness_on_suppresses_action(allowed, snapshot):
    with mock.patch.object(actions, "request_toggle") as request:
        actions.toggle_voice_input_mode(lambda: snapshot)
    request.assert_not_called()


@pytest.mark.parametrize("blocked", ["_sleeping", "_enrolling", "_hw_mic_switch_muted"])
def test_ineligible_device_suppresses_action(allowed, monkeypatch, blocked):
    monkeypatch.setattr(state, blocked, True)
    with mock.patch.object(actions, "request_toggle") as request:
        actions.toggle_voice_input_mode(lambda: allowed)
    request.assert_not_called()


def test_send_once_loopback_no_proxy_or_redirects():
    session = mock.MagicMock()
    session.__enter__.return_value = session
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {"status": 1, "data": {"mode": "tap_to_talk"}}
    with mock.patch.object(actions.requests, "Session", return_value=session):
        assert actions.request_toggle() == "tap_to_talk"
    assert session.trust_env is False
    session.post.assert_called_once_with(actions.TOGGLE_URL, timeout=30, allow_redirects=False)


def test_timeout_has_no_success_cue(allowed):
    with (mock.patch.object(actions, "request_toggle", side_effect=requests.Timeout) as request,
          mock.patch("hal.drivers.button_actions.play_ack_chime") as chime,
          mock.patch("hal.drivers.button_actions._speak_gesture_ack") as speak):
        actions.toggle_voice_input_mode(lambda: allowed)
    request.assert_called_once_with()
    chime.assert_not_called()
    speak.assert_not_called()


def test_cue_only_after_applied_response(allowed, monkeypatch):
    order = []
    def request():
        order.append("applied")
        monkeypatch.setattr(config, "VOICE_INPUT_MODE", "tap_to_talk")
        return "tap_to_talk"
    with (mock.patch.object(actions, "request_toggle", side_effect=request),
          mock.patch("hal.drivers.button_actions.play_ack_chime", side_effect=lambda source: order.append("chime")),
          mock.patch("hal.drivers.button_actions._speak_gesture_ack", side_effect=lambda phrase, source: order.append("spoken"))):
        actions.toggle_voice_input_mode(lambda: allowed)
    assert order == ["applied", "chime", "spoken"]


@pytest.mark.parametrize("mask,expected", [(1, []), (7, ["hold"])])
def test_hold_requires_qualified_electrodes(allowed, mask, expected):
    handler = handler_for(allowed, tap_min_electrodes=3, swipe_axis=tuple(range(12)),
                          gesture_actions={"hold": "toggle_voice_input_mode"})
    detector = handler._detector
    events = []
    for tick in range(2500):
        now = tick / 1000
        events += detector.update(mask if .1 <= now < 2.2 else 0, now)
    assert [e.kind for e in events if e.kind in ("single", "hold", "swipe")] == expected


def test_slow_led_does_not_block_poll_feedback(allowed):
    import threading
    started, unblock = threading.Event(), threading.Event()
    def dispatch(*args, **kwargs):
        started.set()
        assert unblock.wait(2)
    feedback = actions.ModeHoldFeedback()
    with mock.patch("hal.drivers.button_actions.dispatch_led", side_effect=dispatch), mock.patch.object(state, "_restore_user_led"):
        try:
            feedback.set_tier(1)
            assert started.wait(1)
            # These must return even while the RGB dependency remains blocked.
            feedback.release()
            feedback.stop()
        finally:
            unblock.set()
            feedback._thread.join(2)
    assert not feedback._thread.is_alive()


def test_mode_change_during_dispatch_suppresses_stale_action(allowed, monkeypatch):
    handler = handler_for(allowed, gesture_actions={"hold": "toggle_voice_input_mode"})
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "tap_to_talk")
    with mock.patch.object(actions, "toggle_voice_input_mode") as toggle:
        handler._execute(_GestureEvent("hold", 1, held_s=2))
    toggle.assert_not_called()


def test_hold_then_travel_cannot_also_toggle(allowed):
    handler = handler_for(allowed, tap_min_electrodes=3, swipe_axis=tuple(range(12)),
                          gesture_actions={"hold": "toggle_voice_input_mode"})
    detector = handler._detector
    samples = [(0, 0), (.1, 7), (2.2, 14), (2.25, 28), (2.3, 56), (2.4, 0)]
    index, mask, events = 0, 0, []
    for tick in range(2700):
        now = tick / 1000
        while index < len(samples) and samples[index][0] <= now:
            mask = samples[index][1]
            index += 1
        events += detector.update(mask, now)
    assert any(e.kind == "hold_tier" for e in events)
    assert not any(e.kind in ("hold", "single") for e in events)


def test_harness_enables_during_toggle_suppresses_late_feedback(allowed):
    snapshot = dict(allowed)
    def request():
        snapshot["enabled"] = True
        return "tap_to_talk"
    with (mock.patch.object(actions, "request_toggle", side_effect=request),
          mock.patch("hal.drivers.button_actions.play_ack_chime") as chime,
          mock.patch("hal.drivers.button_actions._speak_gesture_ack") as speak):
        actions.toggle_voice_input_mode(lambda: snapshot)
    chime.assert_not_called()
    speak.assert_not_called()


def test_authoritative_privacy_gate(allowed):
    with mock.patch.object(actions.privacy, "mic_locked", return_value=True), mock.patch.object(actions, "request_toggle") as request:
        actions.toggle_voice_input_mode(lambda: allowed)
    request.assert_not_called()


def test_slow_toggle_is_bounded_and_keeps_taps_responsive(allowed):
    import threading
    started, unblock = threading.Event(), threading.Event()
    handler = handler_for(allowed, gesture_actions={"hold": "toggle_voice_input_mode"})
    def request():
        started.set()
        assert unblock.wait(2)
        return "tap_to_talk"
    with (mock.patch.object(actions, "request_toggle", side_effect=request) as toggle,
          mock.patch("hal.drivers.mpr121.single_click_action") as tap,
          mock.patch("hal.drivers.button_actions.play_ack_chime") as chime,
          mock.patch("hal.drivers.button_actions._speak_gesture_ack")):
        try:
            handler._execute(_GestureEvent("hold", 1, held_s=2))
            assert started.wait(1)
            handler._execute(_GestureEvent("hold", 2, held_s=2))
            handler._execute(_GestureEvent("single", 3))
            tap.assert_called_once_with(source="MPR121", announce=False)
            toggle.assert_called_once_with()
            handler._stop.set()
        finally:
            unblock.set()
            handler._mode_toggle._thread.join(2)
        # The second hold was dropped, never replayed after completion.
        toggle.assert_called_once_with()
        chime.assert_not_called()
    assert not handler._mode_toggle._thread.is_alive()



def test_newer_mode_update_suppresses_stale_confirmation(allowed):
    # Automatic is the live HAL value; a delayed tap-to-talk response is stale.
    with (mock.patch.object(actions, "request_toggle", return_value="tap_to_talk"),
          mock.patch("hal.drivers.button_actions.play_ack_chime") as chime,
          mock.patch("hal.drivers.button_actions._speak_gesture_ack") as speak):
        actions.toggle_voice_input_mode(lambda: allowed)
    chime.assert_not_called()
    speak.assert_not_called()
