"""Harness direct voice ownership, payload and duplicate-delivery regressions."""

import threading
from unittest.mock import Mock, patch

import pytest
import requests

from hal.drivers.voice._internal import harness_voice, sensing_sender, turn_dispatch
from hal.drivers.voice._internal.realtime_turn import RealtimeTurnResult, ROUTE_NOISE_DROPPED


@pytest.mark.parametrize("enabled", [False, True])
def test_snapshot_is_explicit_even_when_mode_is_disabled(enabled):
    response = Mock()
    response.json.return_value = {"status": 1, "data": {
        "enabled": enabled, "generation": 7, "agentId": "agent-a",
    }}
    with patch.object(harness_voice.requests, "get", return_value=response) as get:
        snapshot = harness_voice.read_voice_mode()
    assert snapshot == {"enabled": enabled, "generation": 7, "agentId": "agent-a"}
    assert harness_voice.bypass_realtime(snapshot) is enabled
    get.assert_called_once_with(harness_voice.VOICE_MODE_URL, timeout=0.5)


@pytest.mark.parametrize("data", [None, {}, {"enabled": "false", "generation": 1},
                                  {"enabled": False, "generation": True}])
def test_invalid_snapshot_never_authorizes_realtime(data):
    response = Mock()
    response.json.return_value = {"status": 1, "data": data}
    with patch.object(harness_voice.requests, "get", return_value=response):
        assert harness_voice.bypass_realtime(harness_voice.read_voice_mode())


def test_os_timeout_never_falls_back_to_realtime():
    with patch.object(harness_voice.requests, "get", side_effect=requests.Timeout):
        assert harness_voice.read_voice_mode()["unavailable"]


def collaborators():
    decorator = Mock()
    decorator.classify_wake_word.return_value = ("fix the tests", "voice")
    decorator.identify_and_decorate.return_value = ("Speaker - Mai: fix the tests", "mai", "Mai")
    sender = Mock()
    sender.is_echo.return_value = False
    sender.send.return_value = sensing_sender.SendResult(run_id="run-1", delivered=True)
    return decorator, sender


def test_direct_stt_has_no_speaker_or_delegation_decoration_and_keeps_ser():
    decorator, sender = collaborators()
    snapshot = {"enabled": True, "generation": 2}
    with patch.object(turn_dispatch, "voice_metrics") as metrics:
        turn_dispatch.dispatch_turn(
            decorator, sender, "hello lamp fix the tests", [], [b"pcm"],
            RealtimeTurnResult(), interaction_id="voice-1", harness_voice=snapshot,
            identity=("Speaker - Mai: fix the tests", "mai", "Mai"),
        )
    sender.send.assert_called_once_with("fix the tests", event_type="voice",
        interaction_id="voice-1", harness_voice=snapshot)
    decorator.identify_and_decorate.assert_not_called()
    decorator.submit_speech_emotion_from_session.assert_called_once_with([b"pcm"], user="mai")
    metrics.bind_run.assert_called_once_with("voice-1", "run-1")


@pytest.mark.parametrize("reason", ["noise", "echo", "unknown"])
def test_direct_route_preserves_filters_and_unknown_mode_fails_closed(reason):
    decorator, sender = collaborators()
    snapshot = {"enabled": True, "generation": 2}
    rt = RealtimeTurnResult()
    if reason == "noise":
        rt = RealtimeTurnResult(route=ROUTE_NOISE_DROPPED)
    elif reason == "echo":
        sender.is_echo.return_value = True
    else:
        snapshot = {"enabled": False, "generation": -1, "unavailable": True}
    with patch.object(turn_dispatch, "voice_metrics"):
        turn_dispatch.dispatch_turn(decorator, sender, "fix the tests", [], [], rt,
                                    harness_voice=snapshot)
    sender.send.assert_not_called()
    decorator.submit_speech_emotion_from_session.assert_called_once()


def test_disabled_snapshot_follows_normal_dispatch():
    decorator, sender = collaborators()
    snapshot = {"enabled": False, "generation": 9}
    with patch.object(turn_dispatch, "voice_metrics"), \
         patch.object(turn_dispatch, "_take_vision_handoff", return_value=("", "")), \
         patch.object(turn_dispatch, "_take_look_snapshot_marker", return_value=""):
        turn_dispatch.dispatch_turn(decorator, sender, "fix the tests", [], [],
                                    RealtimeTurnResult(), harness_voice=snapshot)
    assert sender.send.call_args.kwargs["harness_voice"] == snapshot
    assert sender.send.call_args.args[0] == "Speaker - Mai: fix the tests"


def test_direct_transport_failure_is_never_automatically_retried():
    with patch.object(sensing_sender.requests, "post", side_effect=requests.ConnectionError) as post:
        result = sensing_sender.SensingSender().send(
            "fix tests", harness_voice={"enabled": True, "generation": 2})
    assert not result
    post.assert_called_once()
    assert post.call_args.kwargs["json"]["harness_voice"] == {"enabled": True, "generation": 2}


@pytest.mark.parametrize("snapshot", [
    {"enabled": True, "generation": 2},
    {"enabled": False, "generation": -1, "unavailable": True},
])
def test_capture_never_opens_or_streams_realtime_for_direct_or_unknown_mode(snapshot, monkeypatch):
    from hal.drivers.voice import voice_service

    service = Mock()
    service._automatic_reply_lock = threading.Lock()
    service._running = False
    service._tts = None
    service._decorator.classify_wake_word.return_value = ("fix the tests", "voice")
    service._decorator.identify_and_decorate.return_value = ("fix the tests", None, None)
    stt = Mock()
    stt.is_closed.return_value = False
    monkeypatch.setattr(voice_service.hal_config, "WAKEWORD_ENABLED", False)
    monkeypatch.setattr(voice_service.hal_config, "REALTIME_ENABLED", True)
    with patch.object(voice_service, "finalize_session", return_value=("fix the tests", [], 2.0)), \
         patch.object(voice_service, "dispatch_turn") as dispatch, \
         patch.object(voice_service, "voice_metrics"), \
         patch.object(voice_service.requests, "post"), \
         patch.object(voice_service, "run_realtime_turn") as realtime:
        voice_service.VoiceService._stream_session(
            service, Mock(), 320, 16000, preconnected_session=stt,
            harness_voice=snapshot,
        )
    service._realtime.prepare_turn.assert_not_called()
    service._realtime.append_audio.assert_not_called()
    service._realtime.send_text.assert_not_called()
    service._realtime.save_main_handoff.assert_not_called()
    realtime.assert_not_called()
    dispatch.assert_not_called()


def test_live_session_refuses_harness_mode_before_touching_realtime():
    from hal.drivers.voice.voice_service import VoiceService

    service = Mock()
    assert VoiceService._live_session(service, Mock(), 320, 16000, [],
        harness_voice={"enabled": True, "generation": 2}) is False
    assert service.mock_calls == []


def test_live_delegation_keeps_disabled_snapshot_during_toggle():
    from hal.drivers.voice import voice_service
    from hal.realtime.models.signal import DelegateSignal

    service = Mock()
    service._live_running = True
    service._live_generation = 3
    service._realtime.stream_output.return_value = iter([
        DelegateSignal(message="fix tests", transcript="fix the tests"),
    ])
    snapshot = {"enabled": False, "generation": 9}
    with patch.object(voice_service, "dispatch_turn") as dispatch:
        voice_service.VoiceService._live_out_pump(service, 3, harness_voice=snapshot)
    assert dispatch.call_args.kwargs["harness_voice"] == snapshot


@pytest.mark.parametrize("snapshot", [
    {"enabled": True, "generation": 2},
    {"enabled": False, "generation": -1, "unavailable": True},
])
def test_direct_or_unknown_mode_discards_prior_realtime_vision_without_loading(snapshot, monkeypatch):
    from hal import app_state

    monkeypatch.setattr(app_state, "realtime_look_frame_path", "/unused/stale.jpg", raising=False)
    monkeypatch.setattr(app_state, "realtime_look_frame_ts", 123.0, raising=False)
    monkeypatch.setattr(app_state, "realtime_look_monitor_path", "/unused/stale.jpg", raising=False)
    decorator, sender = collaborators()
    with patch.object(turn_dispatch, "open", create=True) as image_open, \
         patch.object(turn_dispatch, "voice_metrics"):
        turn_dispatch.dispatch_turn(decorator, sender, "fix the tests", [], [],
                                    RealtimeTurnResult(), harness_voice=snapshot)
    image_open.assert_not_called()
    assert app_state.realtime_look_frame_path is None
    assert app_state.realtime_look_frame_ts == 0.0
    assert app_state.realtime_look_monitor_path is None
    assert turn_dispatch._take_vision_handoff() == ("", "")
    if snapshot["enabled"]:
        assert sender.send.call_args.args == ("fix the tests",)
        assert "image_b64" not in sender.send.call_args.kwargs


@pytest.mark.parametrize("enabled,unavailable,focus,expected", [
    (True, False, False, False),
    (True, False, True, False),
    (False, False, False, False),
    (False, False, True, True),
    (False, True, False, False),
    (True, True, False, False),
])
def test_harness_capture_bypasses_wake_gate_without_extending_focus(
        enabled, unavailable, focus, expected, monkeypatch):
    from hal.drivers.voice import voice_service

    service = Mock()
    service._automatic_reply_lock = threading.Lock()
    service._running = False
    service._tts = None
    service._wakeword_focus.is_active.return_value = focus
    service._decorator.starts_with_wake_word.return_value = False
    service._decorator.matches_wake_word_loosely.return_value = False
    service._decorator.classify_wake_word.return_value = ("fix the tests", "voice")
    service._decorator.identify_and_decorate.return_value = ("fix the tests", None, None)
    stt = Mock()
    stt.is_closed.return_value = False
    snapshot = {"enabled": enabled, "generation": 2, "unavailable": unavailable}
    monkeypatch.setattr(voice_service.hal_config, "WAKEWORD_ENABLED", True)
    monkeypatch.setattr(voice_service.hal_config, "REALTIME_ENABLED", False)
    def finalize(*args, **kwargs):
        stt._on_transcript_cb("fix the tests", False)
        return "fix the tests", [], 2.0

    with patch.object(voice_service, "finalize_session", side_effect=finalize), \
         patch.object(voice_service, "dispatch_turn") as dispatch, \
         patch.object(voice_service, "voice_metrics"), \
         patch.object(voice_service.requests, "post"):
        voice_service.VoiceService._stream_session(
            service, Mock(), 320, 16000, preconnected_session=stt,
            harness_voice=snapshot,
        )
    assert service._backchannel.on_partial.called is expected
    assert dispatch.called is expected
    if expected:
        assert dispatch.call_args.kwargs["harness_voice"] == snapshot
    if enabled or not focus:
        service._wakeword_focus.refresh.assert_not_called()
