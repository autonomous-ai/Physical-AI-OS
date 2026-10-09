"""Device manual recording gestures without GPIO, network, or audio hardware."""

from types import SimpleNamespace
from unittest import mock

import pytest

import hal.app_state as state
from hal import config
from hal.board.mpr121 import MPR121Config
from hal.drivers import button_actions, device_tap_actions, gpio_button
from hal.drivers.mpr121 import MPR121Handler
from hal.drivers.voice._internal import harness_voice


@pytest.fixture
def tap(monkeypatch):
    from hal.drivers.voice.tts import turn_supersession
    monkeypatch.setattr(turn_supersession, "suppress_before", mock.Mock())
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "tap_to_talk", raising=False)
    voice = SimpleNamespace(device_input=mock.Mock(
        spec=["active", "enabled", "start", "finish", "cancel"], active=False, enabled=True,
    ))
    monkeypatch.setattr(state, "voice_service", voice)
    monkeypatch.setattr(state, "tts_service", SimpleNamespace(speaking=False))
    for name in ("_sleeping", "_mic_muted", "_speaker_muted", "_enrolling", "_hw_mic_switch_muted"):
        monkeypatch.setattr(state, name, False)
    for name in ("_stop_active_tracking", "_cancel_agent_speech", "_wake_if_sleepy",
                 "_grant_wakeword_focus", "announce_listening_cue", "play_ack_chime"):
        monkeypatch.setattr(button_actions, name, mock.Mock())
    from hal.routes import music, voice as routes
    for module, names in ((music, ("audio_stop", "unmute_speaker")),
                          (routes, ("stop_tts", "unmute_mic"))):
        for name in names:
            monkeypatch.setattr(module, name, mock.Mock())
    monkeypatch.setattr(harness_voice, "read_voice_mode", mock.Mock(return_value={"enabled": False, "generation": 1}))
    return voice, routes


def test_taps_start_then_finish_without_spoken_cue_or_focus(tap):
    voice, _ = tap
    device_tap_actions.physical_short_tap()
    voice.device_input.start.assert_called_once_with(after_ms=mock.ANY)
    voice.device_input.active = True
    device_tap_actions.physical_short_tap()
    voice.device_input.finish.assert_called_once_with()
    button_actions.announce_listening_cue.assert_not_called()
    button_actions._grant_wakeword_focus.assert_not_called()
    button_actions.play_ack_chime.assert_not_called()
    button_actions._cancel_agent_speech.assert_called_once_with("button", before_ms=mock.ANY)


def test_speaking_tap_only_stops_then_next_records(tap):
    voice, routes = tap
    state.tts_service.speaking = True
    device_tap_actions.physical_short_tap()
    voice.device_input.cancel.assert_called_once_with()
    routes.stop_tts.assert_called_once_with()
    button_actions._cancel_agent_speech.assert_called_once_with("button")
    voice.device_input.start.assert_not_called()
    state.tts_service.speaking = False
    device_tap_actions.physical_short_tap()
    voice.device_input.start.assert_called_once_with(after_ms=mock.ANY)
    assert button_actions._cancel_agent_speech.call_count == 2
    button_actions._cancel_agent_speech.assert_called_with("button", before_ms=mock.ANY)


@pytest.mark.parametrize("playing", [False, True])
def test_start_only_resets_music_when_playing(tap, monkeypatch, playing):
    from hal.routes import music
    monkeypatch.setattr(state, "_music_playing", playing)
    monkeypatch.setattr(state, "music_service", SimpleNamespace(playing=playing))
    device_tap_actions.physical_short_tap()
    assert music.audio_stop.call_count == int(playing)


@pytest.mark.parametrize("blocked", ["_hw_mic_switch_muted", "_enrolling", "_sleeping"])
def test_privacy_enrollment_and_sleep_do_not_start_capture(tap, monkeypatch, blocked):
    voice, routes = tap
    monkeypatch.setattr(state, blocked, True)
    device_tap_actions.physical_short_tap()
    voice.device_input.start.assert_not_called()
    voice.device_input.finish.assert_not_called()
    routes.unmute_mic.assert_not_called()
    assert button_actions._wake_if_sleepy.call_count == int(blocked == "_sleeping")
    assert button_actions.play_ack_chime.call_count == int(blocked == "_sleeping")


def test_software_muted_mic_can_start(tap, monkeypatch):
    voice, routes = tap
    monkeypatch.setattr(state, "_mic_muted", True)
    device_tap_actions.physical_short_tap()
    routes.unmute_mic.assert_called_once_with()
    voice.device_input.start.assert_called_once_with(after_ms=mock.ANY)


def test_unknown_harness_mode_fails_closed(tap):
    voice, _ = tap
    harness_voice.read_voice_mode.return_value = {"enabled": False, "unavailable": True}
    device_tap_actions.physical_short_tap()
    voice.device_input.start.assert_not_called()


def test_automatic_action_is_unchanged(tap, monkeypatch):
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "automatic")
    with mock.patch.object(button_actions, "single_click_action") as normal:
        device_tap_actions.physical_short_tap("test")
    normal.assert_called_once_with(source="test", announce=False)
    harness_voice.read_voice_mode.assert_not_called()


def test_mpr121_rapid_taps_stay_queued_individually(tap):
    handler = MPR121Handler(MPR121Config(bus=1, debounce_ms=0))
    snapshot = {"enabled": False, "generation": 1}
    handler._harness_gestures = SimpleNamespace(snapshot=snapshot, mode_key=lambda: (False, 1))
    handler._mode_key = (False, 1)
    handler._detector = handler._new_detector()
    for touched, now in [(False, 0), (True, 1), (False, 1.1), (True, 1.2), (False, 1.3), (False, 2)]:
        handler._process_touch(touched, now)
    events = [handler._pending.get_nowait()[1] for _ in range(handler._pending.qsize())]
    assert [event.kind for event in events] == ["single", "single"]
    assert events[0].gesture_id != events[1].gesture_id
    handler._invalidate_pending("harness_mode_changed")
    assert handler._single_generation == handler._generation


def test_gpio_rapid_taps_do_not_arm_triple_click(tap):
    from hal.board.gpio_button import ButtonConfig
    from hal.test.test_gpio_button import ImmediateThread, edge
    with (mock.patch.object(gpio_button, "HoldLEDFeedback"),
          mock.patch.object(gpio_button.threading, "Thread", ImmediateThread),
          mock.patch.object(gpio_button, "physical_short_tap") as action,
          mock.patch.object(gpio_button, "triple_click_action") as triple,
          mock.patch.object(gpio_button, "announce_listening_cue") as cue):
        handler = gpio_button.GPIOButtonHandler(ButtonConfig(chip=0, line=99, debounce_ns=0))
        for start in (1, 1.2, 1.4):
            edge(handler, 0, start)
            edge(handler, 1, start + .1)
        handler._on_click_timeout()
    assert action.call_count == 3
    assert handler._click_timer is None
    triple.assert_not_called()
    cue.assert_not_called()


def spatial_trace(monkeypatch, samples, *, mode="tap_to_talk", harness=False):
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", mode)
    handler = MPR121Handler(MPR121Config(
        bus=0, swipe_axis=tuple(range(12)), tap_min_electrodes=3,
    ))
    handler._harness_gestures = SimpleNamespace(snapshot={"enabled": harness, "generation": 1})
    detector = handler._new_detector()
    detector.update(0, 0)
    actions = []
    index, mask = 0, 0
    for tick in range(1, int((samples[-1][0] + .2) * 1000)):
        now = tick / 1000
        while index < len(samples) and samples[index][0] <= now:
            mask = samples[index][1]
            index += 1
        actions.extend((now, event.kind) for event in detector.update(mask, now)
                       if event.kind in {"single", "swipe", "hold"})
    return actions


@pytest.mark.parametrize("mode,harness,delay", [
    ("tap_to_talk", False, .030), ("automatic", False, .120), ("tap_to_talk", True, .120),
])
def test_stationary_chord_release_is_fast_only_for_device(monkeypatch, mode, harness, delay):
    actions = spatial_trace(monkeypatch, [(.1, 7), (.2, 0)], mode=mode, harness=harness)
    assert len(actions) == 1 and actions[0][1] == "single"
    assert .2 + delay <= actions[0][0] <= .2 + delay + .002


def test_stationary_chord_release_bounce_does_not_duplicate_tap(monkeypatch):
    actions = spatial_trace(monkeypatch, [(.1, 7), (.2, 0), (.22, 7), (.3, 0)])
    assert len(actions) == 1 and actions[0][1] == "single"
    assert actions[0][0] >= .33


def test_swipe_handoff_gap_keeps_full_window(monkeypatch):
    actions = spatial_trace(monkeypatch, [
        (.1, 1), (.15, 0), (.21, 2), (.25, 4), (.29, 8), (.33, 16), (.38, 0),
    ])
    assert len(actions) == 1 and actions[0][1] == "swipe"
    assert actions[0][0] >= .50


def test_qualified_chord_motion_retains_swipe_handoff_window(monkeypatch):
    actions = spatial_trace(monkeypatch, [
        (.1, 7), (.2, 14), (.25, 0), (.31, 28), (.36, 56), (.42, 0),
    ])
    assert len(actions) == 1 and actions[0][1] == "swipe"
    assert actions[0][0] >= .54


def test_unqualified_short_third_electrode_does_not_commit_fast_tap(monkeypatch):
    assert spatial_trace(monkeypatch, [(.1, 3), (.16, 7), (.17, 3), (.22, 0)]) == []


def test_qualified_chord_handoff_within_debounce_stays_one_swipe(monkeypatch):
    actions = spatial_trace(monkeypatch, [
        (.1, 7), (.2, 0), (.22, 14), (.26, 28), (.3, 56), (.35, 0),
    ])
    assert len(actions) == 1 and actions[0][1] == "swipe"


def test_touch_after_stationary_chord_commit_starts_new_tap(monkeypatch):
    actions = spatial_trace(monkeypatch, [(.1, 7), (.2, 0), (.25, 7), (.35, 0)])
    assert [kind for _, kind in actions] == ["single", "single"]


@pytest.mark.parametrize("action", ["wake", "interrupt"])
def test_ack_follows_wake_or_stop_without_starting_capture(tap, monkeypatch, action):
    voice, routes = tap
    events = []
    if action == "wake":
        monkeypatch.setattr(state, "_sleeping", True)
        monkeypatch.setattr(state, "_speaker_muted", True)

        def wake(source):
            events.append("wake")
            state._sleeping = False
            state._speaker_muted = False

        button_actions._wake_if_sleepy.side_effect = wake
    else:
        state.tts_service.speaking = True
        routes.stop_tts.side_effect = lambda: events.append("stop")

    def ack(source):
        assert source == "MPR121"
        assert not state._speaker_muted
        events.append("ack")

    button_actions.play_ack_chime.side_effect = ack
    device_tap_actions.physical_short_tap("MPR121")
    assert events == ["wake" if action == "wake" else "stop", "ack"]
    voice.device_input.start.assert_not_called()
    voice.device_input.finish.assert_not_called()
    # The next tap starts capture, whose own ready cue must not get an extra ping.
    state.tts_service.speaking = False
    device_tap_actions.physical_short_tap("MPR121")
    voice.device_input.start.assert_called_once_with(after_ms=mock.ANY)
    button_actions.play_ack_chime.assert_called_once_with("MPR121")


def test_next_recording_cancels_pending_turn_before_admission(tap):
    voice, routes = tap
    sequence = mock.Mock()
    sequence.attach_mock(voice.device_input.cancel, "cancel")
    sequence.attach_mock(routes.stop_tts, "stop")
    sequence.attach_mock(voice.device_input.start, "start")
    device_tap_actions.physical_short_tap()
    assert sequence.mock_calls == [mock.call.cancel(), mock.call.stop(),
                                   mock.call.start(after_ms=mock.ANY)]
    cutoff = voice.device_input.start.call_args.kwargs["after_ms"]
    button_actions._cancel_agent_speech.assert_called_once_with("button", before_ms=cutoff)
