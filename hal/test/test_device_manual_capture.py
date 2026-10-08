"""Device tap capture waits for an explicit finish and preserves route ownership."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import Mock, patch

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
    service.device_input = DeviceTapInput(service._harness_capture, Mock())
    service.device_input.wait_for_capture = lambda: setattr(service, "_running", False)
    monkeypatch.setattr(module.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(module.voice_cfg, "LIVE_MODE", live)

    def sleep(delay):
        if delay == 0.1:
            service._running = False

    def claim(_):
        prepare_aec.assert_called_once_with(module.voice_cfg.STT_RATE)
        return None

    with patch.object(module, "read_voice_mode", return_value=LOCAL), \
         patch.object(module.time, "sleep", side_effect=sleep), \
         patch.object(module.aec, "configure") as prepare_aec, \
         patch.object(service._harness_capture, "claim", side_effect=claim), \
         patch.object(module, "ArecordStream") as recorder:
        module.VoiceService._loop(service)
    prepare_aec.assert_called_once_with(module.voice_cfg.STT_RATE)
    recorder.assert_not_called()
    service._sd.InputStream.assert_not_called()
    service._vad_loop.assert_not_called()
    service._stream_session.assert_not_called()
    service._realtime.append_audio.assert_not_called()


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("reason", ["finish", "cancel", "route_change", "offline",
                                   "provider_close", "stt_error", "timeout"])
def test_manual_stream_only_finish_dispatches_once(reason, live, monkeypatch):
    from hal.drivers.voice._internal import device_voice_pipeline as module
    from hal.drivers.voice._internal.device_turn_queue import DeviceTurnQueue

    queue = DeviceTurnQueue()
    ticket = queue.reserve(device_snapshot(LOCAL))
    control = HarnessCapture(target_matches=same_capture_target)
    assert control.start(device_snapshot(LOCAL), reservation=ticket)
    capture = control.claim(LOCAL)
    tts = Mock(last_spoken_text="")
    decorator = Mock()
    decorator.classify_wake_word.return_value = ("please fix the tests", "voice")
    decorator.identify_and_decorate.return_value = ("please fix the tests", None, None)
    sender = Mock()
    stt = Mock()
    stt.is_closed.return_value = False
    connected = threading.Event()

    def start(callback):
        stt.callback = callback
        connected.set()
        return True

    stt.start.side_effect = start
    current_mode = dict(LOCAL)
    reads = []
    pipeline = module.DeviceVoicePipeline(
        queue, create_session=lambda: stt, convert=lambda data, rate: data.tobytes(),
        valid=lambda snapshot: same_capture_target(snapshot, current_mode),
        set_capturing=Mock(), tts=tts, decorator=decorator,
        sensing_sender=sender, noise_is_speech=lambda pcm: True,
    )

    def read(_):
        reads.append(1)
        assert len(reads) <= 3, "manual session did not terminate"
        assert connected.wait(1)
        if len(reads) == 2:
            stt.callback("please fix the tests", False)
            stt.callback("please fix the tests", True)
        if len(reads) == 3:
            if reason == "cancel":
                control.cancel()
            elif reason == "timeout":
                pipeline.max_duration = -1
            else:
                if reason in ("route_change", "offline"):
                    current_mode.update({"generation": 5} if reason == "route_change"
                                        else {"unavailable": True})
                elif reason == "provider_close":
                    stt.is_closed.return_value = True
                elif reason == "stt_error":
                    stt.send_audio.side_effect = RuntimeError("STT disconnected")
                control.finish()
                control.finish()
        return np.zeros((320, 1), dtype=np.int16), False

    # Explicit capture remains independent of stale wake/live configuration.
    monkeypatch.setattr(module.hal_config, "WAKEWORD_ENABLED", True)
    monkeypatch.setattr(module.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(module.voice_cfg, "LIVE_MODE", live)
    try:
        with patch.object(module, "dispatch_turn", wraps=module.dispatch_turn) as dispatch, \
             patch.object(module, "voice_metrics"), \
             patch("hal.drivers.harness.led.set_capturing") as harness_led:
            pipeline.run_capture(Mock(read=read), 320, 16000, capture, ticket)
            assert ticket.done.wait(1)
        assert len(reads) == 3
        harness_led.assert_not_called()
        assert dispatch.call_count == (1 if reason == "finish" else 0)
        if reason == "finish":
            assert dispatch.call_args.args[2] == "please fix the tests"
            assert dispatch.call_args.kwargs["event_type_override"] == "voice_command"
            sender.send.assert_called_once()
            assert sender.send.call_args.kwargs["event_type"] == "voice_command"
            assert sender.send.call_args.kwargs["voice_turn_type"] == "voice_command"
            assert dispatch.call_args.kwargs["harness_voice"] == device_snapshot(LOCAL)
        assert tts.play_device_capture_chime.call_args_list[0].kwargs == {"finished": False}
    finally:
        assert queue.shutdown(2)


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


@pytest.mark.parametrize("timing", ["before_wait", "during_wait"])
def test_device_capture_start_wakes_idle_worker_without_losing_signal(timing):
    import threading
    control = HarnessCapture(target_matches=same_capture_target)
    device = DeviceTapInput(control, control.start, read_mode=lambda: LOCAL)
    reached_wait = threading.Event()
    original_wait = device._wake.wait
    def wait(timeout):
        reached_wait.set()
        return original_wait(timeout)
    device._wake.wait = wait
    results = []
    if timing == "before_wait":
        assert device.start()
    worker = threading.Thread(target=lambda: results.append(device.wait_for_capture(timeout=1)))
    worker.start()
    try:
        if timing == "during_wait":
            assert reached_wait.wait(1)
            assert device.start()
        worker.join(1)
        assert not worker.is_alive()
        assert results == [True]
        assert control.claim(LOCAL) is not None
    finally:
        device._wake.set()
        worker.join(1)


def test_rejected_device_capture_does_not_wake_idle_worker():
    control = HarnessCapture(target_matches=same_capture_target)
    device = DeviceTapInput(control, lambda _: False, read_mode=lambda: LOCAL)
    assert not device.start()
    assert not device.wait_for_capture(timeout=0)
