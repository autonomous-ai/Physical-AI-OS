"""Device tap capture waits for an explicit finish and preserves route ownership."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, call, patch

import numpy as np
import pytest

from hal.drivers.voice._internal.device_input import DeviceTapInput
from hal.drivers.voice._internal.harness_capture import HarnessCapture
from hal.drivers.voice._internal.input_policy import (
    device_manual_mode, device_snapshot, same_capture_target,
)


LOCAL = {"enabled": False, "generation": 4}


@pytest.fixture(autouse=True)
def manual_mode(monkeypatch):
    from hal import config
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "tap_to_talk")


@pytest.mark.parametrize("change", [
    {"enabled": True}, {"unavailable": True}, {"generation": 5},
    {"generation": None}, {"generation": True},
])
def test_route_change_cancels_local_owner(change):
    control = HarnessCapture(target_matches=same_capture_target)
    assert control.start(device_snapshot(LOCAL))
    assert control.claim(dict(LOCAL, **change)) is None
    assert not control.active
    assert not control.finish()


def test_automatic_mode_rejects_local_owner(monkeypatch):
    from hal import config
    control = HarnessCapture(target_matches=same_capture_target)
    assert control.start(device_snapshot(LOCAL))
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "automatic")
    assert not device_manual_mode(LOCAL)
    assert control.claim(LOCAL) is None
    assert not control.active


@pytest.mark.parametrize("snapshot", [{}, {"enabled": False},
                                        dict(LOCAL, unavailable=True),
                                        dict(LOCAL, enabled=True)])
def test_start_requires_authoritative_harness_off(snapshot):
    assert not device_manual_mode(snapshot)
    assert not HarnessCapture(target_matches=same_capture_target).start(device_snapshot(snapshot))


@pytest.mark.parametrize("live", [False, True])
def test_device_idle_does_not_open_microphone(live, monkeypatch):
    from hal.drivers.voice import voice_service as module
    service = Mock()
    service._running = True
    service._alsa_device = "test"
    service._harness_capture = HarnessCapture(target_matches=same_capture_target)
    monkeypatch.setattr(module.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(module.voice_cfg, "LIVE_MODE", live)

    def sleep(delay):
        if delay == 0.1:
            service._running = False

    with patch.object(module, "read_voice_mode", return_value=LOCAL), \
         patch.object(module.time, "sleep", side_effect=sleep), \
         patch.object(module, "ArecordStream") as recorder:
        module.VoiceService._loop(service)
    recorder.assert_not_called()
    service._vad_loop.assert_not_called()
    service._stream_session.assert_not_called()
    service._realtime.append_audio.assert_not_called()


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("reason", ["finish", "cancel", "route_change", "offline",
                                   "provider_close", "stt_error", "timeout"])
def test_manual_stream_only_finish_dispatches_once(reason, live, monkeypatch):
    from hal.drivers.voice import voice_service as module

    control = HarnessCapture(target_matches=same_capture_target)
    assert control.start(device_snapshot(LOCAL))
    capture = control.claim(LOCAL)
    service = Mock()
    service._running = True
    service._tts = Mock(last_spoken_text="")
    service._tts_is_speaking.return_value = False
    service._music_is_playing.return_value = False
    service._wakeword_focus.is_active.return_value = False
    service._np = np
    service._decorator.starts_with_wake_word.return_value = False
    service._decorator.classify_wake_word.return_value = ("please fix the tests", "voice")
    service._decorator.identify_and_decorate.return_value = ("please fix the tests", None, None)
    stt = Mock()
    stt.is_closed.return_value = False
    service._stt.create_session.return_value = stt
    current_mode = dict(LOCAL)
    reads = []

    def read(_):
        reads.append(1)
        assert len(reads) <= 4, "manual session did not terminate"
        if len(reads) == 2:
            stt._on_transcript_cb("please fix the tests", False)
            stt._on_transcript_cb("please fix the tests", True)
        if len(reads) == 3:
            if reason == "finish":
                control.finish()
                control.finish()
            elif reason == "cancel":
                control.cancel()
            elif reason in ("route_change", "offline"):
                current_mode.update({"generation": 5} if reason == "route_change" else {"unavailable": True})
                control.finish()
            elif reason == "provider_close":
                stt.is_closed.return_value = True
            elif reason == "stt_error":
                stt.send_audio.side_effect = RuntimeError("STT disconnected")
            else:
                monkeypatch.setattr(module.voice_cfg, "MAX_SESSION_DURATION_S", -1)
        return np.zeros((320, 1), dtype=np.int16), False

    mic = Mock()
    mic.read.side_effect = read
    # Explicit capture must work even if a stale wake setting is still true.
    monkeypatch.setattr(module.hal_config, "WAKEWORD_ENABLED", True)
    monkeypatch.setattr(module.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(module.voice_cfg, "LIVE_MODE", live)
    monkeypatch.setattr(module.voice_cfg, "SILENCE_VAD_ENABLED", False, raising=False)
    with patch.object(module, "read_voice_mode", side_effect=lambda: dict(current_mode)), \
         patch.object(module, "turn_should_close", return_value=True) as silence, \
         patch.object(module, "finalize_session", return_value=("please fix the tests", [], 2.0)), \
         patch.object(module, "dispatch_turn", wraps=module.dispatch_turn) as dispatch, \
         patch.object(module, "voice_metrics"), \
         patch.object(module.requests, "post"), \
         patch("hal.drivers.harness.led.set_capturing") as harness_led:
        module.VoiceService._stream_session(
            service, mic, 320, 16000, preconnected_session=stt,
            harness_voice=device_snapshot(LOCAL), manual_capture=capture,
        )
    assert len(reads) == 3
    silence.assert_not_called()
    harness_led.assert_not_called()
    service._set_emotion_local.assert_any_call(module.presets.EMO_LISTENING)
    assert dispatch.call_count == (1 if reason == "finish" else 0)
    if reason == "finish":
        assert dispatch.call_args.args[2] == "please fix the tests"
        assert dispatch.call_args.kwargs["event_type_override"] == "voice_command"
        service._sensing_sender.send.assert_called_once()
        sent = service._sensing_sender.send.call_args
        assert sent.kwargs["event_type"] == "voice_command"
        assert sent.kwargs["voice_turn_type"] == "voice_command"
        assert dispatch.call_args.kwargs["harness_voice"] == device_snapshot(LOCAL)
    expected_chimes = [call(), call(finished=True)] if reason == "finish" else [call()]
    assert service._tts.play_harness_capture_chime.call_args_list == expected_chimes
    service._backchannel.on_partial.assert_not_called()
    service._realtime.append_audio.assert_not_called()
    service._realtime.send_text.assert_not_called()
    service._realtime.bind_audio_turn.assert_not_called()
    service._realtime.reserve_audio_capture.assert_not_called()
    service._try_live_opener.assert_not_called()


@pytest.mark.parametrize("flag", ["_mic_muted", "_sleeping", "_hw_mic_switch_muted"])
def test_device_start_respects_privacy_and_sleep(flag, monkeypatch):
    from hal import app_state
    from hal.drivers.voice import voice_service as module
    service = Mock()
    service._running = True
    service._harness_capture = HarnessCapture(target_matches=same_capture_target)
    service._tts_is_speaking.return_value = False
    service._music_is_playing.return_value = False
    service.start_harness_capture.side_effect = lambda snapshot: module.VoiceService.start_harness_capture(service, snapshot)
    service.device_input = DeviceTapInput(
        service._harness_capture, service.start_harness_capture, read_mode=lambda: module.read_voice_mode(),
    )
    for name in ("_mic_muted", "_sleeping", "_hw_mic_switch_muted"):
        monkeypatch.setattr(app_state, name, name == flag)
    with patch.object(module, "read_voice_mode", return_value=LOCAL):
        assert not service.device_input.start()
    assert not service._harness_capture.active


@pytest.mark.parametrize("mode,expected", [(None, "automatic"), ("automatic", "automatic"),
                                          ("tap_to_talk", "tap_to_talk")])
def test_config_default_and_effective_wake(mode, expected, tmp_path):
    config = {"wakeword": True}
    if mode is not None:
        config["voice_input_mode"] = mode
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    script = "from hal import config; print(config.VOICE_INPUT_MODE, config.WAKEWORD_ENABLED)"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=dict(os.environ, OS_CONFIG_PATH=str(path), PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True, text=True, timeout=20, check=True,
    )
    assert result.stdout.strip().splitlines()[-1] == f"{expected} {expected == 'automatic'}"


def test_device_start_finish_cancel_api(monkeypatch):
    from hal import app_state
    from hal.drivers.voice import voice_service as module
    service = Mock()
    service._running = True
    service._harness_capture = HarnessCapture(target_matches=same_capture_target)
    service._tts_is_speaking.return_value = False
    service._music_is_playing.return_value = False
    service.start_harness_capture.side_effect = lambda snapshot: module.VoiceService.start_harness_capture(service, snapshot)
    service.device_input = DeviceTapInput(
        service._harness_capture, service.start_harness_capture, read_mode=lambda: module.read_voice_mode(),
    )
    for flag in ("_mic_muted", "_sleeping", "_hw_mic_switch_muted"):
        monkeypatch.setattr(app_state, flag, False)
    with patch.object(module, "read_voice_mode", return_value=LOCAL):
        assert service.device_input.start()
        assert service.device_input.enabled
        assert service.device_input.active
        assert not service.device_input.start()
    capture = service._harness_capture.claim(LOCAL)
    assert capture.snapshot == device_snapshot(LOCAL)
    assert service.device_input.finish()
    assert capture.finished.is_set()
    service.device_input.cancel()
    assert capture.cancelled.is_set()
    assert not service.device_input.active


@pytest.mark.parametrize("snapshot", [dict(LOCAL, enabled=True), dict(LOCAL, unavailable=True)])
def test_start_rechecks_route_instead_of_trusting_cached_off(snapshot):
    from hal.drivers.voice import voice_service as module
    service = Mock()
    service.device_input = DeviceTapInput(
        service._harness_capture, service.start_harness_capture, read_mode=lambda: module.read_voice_mode(),
    )
    service.device_input.observe(LOCAL)
    with patch.object(module, "read_voice_mode", return_value=snapshot):
        assert not service.device_input.start()
    service.start_harness_capture.assert_not_called()
    assert not service.device_input.enabled


def test_unowned_stream_is_rejected_before_stt_or_model():
    from hal.drivers.voice import voice_service as module
    service = Mock()
    stt = Mock()
    with patch.object(module, "dispatch_turn") as dispatch:
        module.VoiceService._stream_session(
            service, Mock(), 320, 16000,
            preconnected_session=stt, harness_voice=LOCAL,
        )
    stt.close.assert_called_once()
    service._stt.create_session.assert_not_called()
    service._realtime.append_audio.assert_not_called()
    dispatch.assert_not_called()


def test_finish_before_recorder_ready_discards_without_chime():
    from hal.drivers.voice import voice_service as module
    control = HarnessCapture(target_matches=same_capture_target)
    assert control.start(device_snapshot(LOCAL))
    assert control.finish()
    capture = control.claim(LOCAL)
    service = Mock()
    service._running = True
    service._tts = Mock(last_spoken_text="")
    stt = Mock()
    stt.is_closed.return_value = False
    with patch.object(module, "read_voice_mode", return_value=LOCAL), \
         patch.object(module, "finalize_session", return_value=("", [], 0)), \
         patch.object(module, "dispatch_turn") as dispatch:
        module.VoiceService._stream_session(
            service, Mock(), 320, 16000, preconnected_session=stt,
            harness_voice=device_snapshot(LOCAL), manual_capture=capture,
        )
    dispatch.assert_not_called()
    service._tts.play_harness_capture_chime.assert_not_called()
