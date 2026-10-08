"""Manual Harness recording never submits on silence or a cancelled owner."""
import threading

from unittest.mock import Mock, patch

import numpy as np
import pytest

from hal.drivers.voice._internal.harness_capture import HarnessCapture


SNAPSHOT = {"enabled": True, "generation": 2, "focusAvailable": True,
            "machineId": "mac", "agentId": "agent", "focusRevision": "rev"}


@pytest.mark.parametrize("change", [
    {"enabled": False}, {"generation": 3}, {"focusAvailable": False},
    {"agentId": "next"}, {"focusRevision": "new"}, {"unavailable": True},
])
def test_owner_change_cancels_pending_capture(change):
    control = HarnessCapture()
    assert control.start(SNAPSHOT)
    assert control.claim(dict(SNAPSHOT, **change)) is None
    assert not control.active
    assert not control.finish()


def test_old_cancelled_capture_cannot_release_new_capture():
    control = HarnessCapture()
    assert control.start(SNAPSHOT)
    old = control.claim(SNAPSHOT)
    control.cancel()
    assert old.cancelled.is_set()
    assert control.start(SNAPSHOT)
    control.release(old)
    assert control.active


@pytest.mark.parametrize("reason", ["finish", "cancel", "disconnect", "provider_close", "timeout"])
def test_real_stream_silence_waits_for_tap_and_cancel_never_dispatches(reason, monkeypatch):
    from hal.drivers.voice import voice_service as module

    control = HarnessCapture()
    assert control.start(SNAPSHOT)
    capture = control.claim(SNAPSHOT)
    service = Mock()
    service._automatic_reply_lock = threading.Lock()
    service._automatic_reply_stop = None
    service._automatic_reply_cancelled_at = None
    service._stt_drain_worker = None
    service._stt_drain_future = None
    service._running = True
    service._tts = Mock(last_spoken_text="")
    service._tts.play_harness_capture_chime.return_value = True
    service._tts_is_speaking.return_value = False
    service._music_is_playing.return_value = False
    service._np = np
    service._decorator.classify_wake_word.return_value = ("fix the tests", "voice")
    service._decorator.identify_and_decorate.return_value = ("fix the tests", None, None)
    stt = Mock()
    stt.is_closed.return_value = False
    service._stt.create_session.return_value = stt
    reads = []
    current_mode = dict(SNAPSHOT)

    def read(_):
        reads.append(1)
        if len(reads) == 3:
            if reason == "finish":
                control.finish()
            elif reason == "cancel":
                control.cancel()
            elif reason == "disconnect":
                current_mode["focusAvailable"] = False
                control.finish()
            elif reason == "provider_close":
                stt.is_closed.return_value = True
            else:
                monkeypatch.setattr(module.voice_cfg, "MAX_SESSION_DURATION_S", -1)
        return np.zeros((320, 1), dtype=np.int16), False

    mic = Mock()
    mic.read.side_effect = read
    monkeypatch.setattr(module.hal_config, "WAKEWORD_ENABLED", False)
    monkeypatch.setattr(module.hal_config, "REALTIME_ENABLED", False)
    monkeypatch.setattr(module.voice_cfg, "SILENCE_VAD_ENABLED", False, raising=False)
    with patch.object(module, "read_voice_mode", side_effect=lambda: dict(current_mode)), \
         patch.object(module, "turn_should_close", return_value=True) as silence, \
         patch.object(module, "finalize_session", return_value=("fix the tests", [], 2.0)), \
         patch.object(module, "dispatch_turn") as dispatch, \
         patch.object(module, "voice_metrics"), \
         patch.object(module.requests, "post"), \
         patch("hal.drivers.harness.led.set_capturing") as capture_led:
        module.VoiceService._stream_session(
            service, mic, 320, 16000, preconnected_session=stt,
            harness_voice=SNAPSHOT, manual_capture=capture,
        )
    from unittest.mock import call
    assert capture_led.call_args_list == [call(True), call(False)]
    assert len(reads) == 3
    from unittest.mock import call
    expected = [call(), call(finished=True)] if reason == "finish" else [call()]
    assert service._tts.play_harness_capture_chime.call_args_list == expected
    silence.assert_not_called()
    assert dispatch.called is (reason == "finish")
    service._realtime.append_audio.assert_not_called()


def test_fast_finish_before_recorder_ready_does_not_send_or_beep(monkeypatch):
    from hal.drivers.voice import voice_service as module
    control = HarnessCapture()
    assert control.start(SNAPSHOT)
    assert control.finish()
    capture = control.claim(SNAPSHOT)
    service = Mock()
    service._automatic_reply_lock = threading.Lock()
    service._automatic_reply_stop = None
    service._automatic_reply_cancelled_at = None
    service._stt_drain_worker = None
    service._stt_drain_future = None
    service._running = True
    stt = Mock()
    stt.is_closed.return_value = False
    monkeypatch.setattr(module.hal_config, "WAKEWORD_ENABLED", False)
    with patch.object(module, "finalize_session", return_value=("", [], 0)), \
         patch.object(module, "dispatch_turn") as dispatch:
        module.VoiceService._stream_session(service, Mock(), 320, 16000,
            preconnected_session=stt, harness_voice=SNAPSHOT, manual_capture=capture)
    dispatch.assert_not_called()
    service._tts.play_harness_capture_chime.assert_not_called()


def test_harness_idle_does_not_open_microphone(monkeypatch):
    from hal.drivers.voice import voice_service as module
    service = Mock()
    service._running = True
    service._alsa_device = "test"
    service._harness_capture = HarnessCapture()
    monkeypatch.setattr(module.hal_config, "REALTIME_ENABLED", False)

    def sleep(delay):
        if delay == 0.1:
            service._running = False

    with patch.object(module, "read_voice_mode", return_value=SNAPSHOT), \
         patch.object(module.time, "sleep", side_effect=sleep), \
         patch.object(module, "ArecordStream") as recorder:
        module.VoiceService._loop(service)
    recorder.assert_not_called()
    service._vad_loop.assert_not_called()
    service._stream_session.assert_not_called()


@pytest.mark.parametrize("flag", ["_mic_muted", "_sleeping", "_hw_mic_switch_muted"])
def test_start_respects_privacy_and_sleep(flag, monkeypatch):
    from hal import app_state
    from hal.drivers.voice.voice_service import VoiceService
    service = Mock()
    service._running = True
    service._harness_capture = HarnessCapture()
    service._tts_is_speaking.return_value = False
    service._music_is_playing.return_value = False
    monkeypatch.setattr(app_state, "_hw_mic_switch_muted", False)
    monkeypatch.setattr(app_state, "_mic_muted", False)
    monkeypatch.setattr(app_state, "_sleeping", False)
    monkeypatch.setattr(app_state, flag, True)
    assert not VoiceService.start_harness_capture(service, SNAPSHOT)
    assert not service._harness_capture.active
