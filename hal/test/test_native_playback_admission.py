"""Native realtime audio must take over interruptible fillers before frame one."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

from hal.drivers.voice.tts.service import TTSService


def _service():
    service = object.__new__(TTSService)
    service._backend = SimpleNamespace(available=True)
    service._sd = object()
    service._speaker_muted = lambda: False
    service._owner_suppressed = lambda owner: False
    service._lock = threading.Lock()
    service._lock.acquire()
    service._interruptible = True
    service._native_mode = False
    service._stop_event = threading.Event()
    service._native_direct_cache = {}
    service._device_key = lambda: "test"
    service._device_rate = 24000
    service._stream_lock = threading.Lock()
    service._ensure_stream = Mock()
    service._begin_playback = Mock()
    service.stop = Mock(side_effect=service._lock.release)
    return service


def test_native_answer_preempts_interruptible_filler_before_first_frame():
    service = _service()
    try:
        assert service.native_play_begin(24000, owner="interaction:answer")
        service.stop.assert_called_once_with()
        service._begin_playback.assert_called_once_with("interaction:answer")
        assert service._native_mode
    finally:
        if service._lock.locked():
            service._lock.release()


def test_native_answer_does_not_interrupt_protected_speech():
    service = _service()
    service._interruptible = False
    try:
        assert not service.native_play_begin(24000)
        service.stop.assert_not_called()
        service._begin_playback.assert_not_called()
    finally:
        service._lock.release()


def test_native_begin_preserves_warm_device_stream():
    service = _service()
    service._lock.release()
    service._device_rate = 44100
    service._stream_rate = 44100
    stream = object()
    service._stream = stream
    try:
        assert service.native_play_begin(24000)
        service._ensure_stream.assert_not_called()
        assert service._stream is stream
        assert not service._native_direct
    finally:
        service._lock.release()


def test_native_answer_keeps_leading_frames_when_speaker_still_busy(monkeypatch):
    import numpy as np
    from hal import config
    from hal.drivers.voice._internal import realtime_turn
    from hal.realtime.models import AudioOutput, TextOutput

    monkeypatch.setattr(config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(config, "REALTIME_NATIVE_AUDIO", True)
    monkeypatch.setattr(realtime_turn, "gemini_needs_idle_workaround", lambda: False)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_start", lambda: None)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", lambda: None)
    monkeypatch.setattr(realtime_turn, "_reply_language_name", lambda: "Vietnamese")
    first = np.ones(240, dtype=np.float32)
    second = np.ones(240, dtype=np.float32) * 2
    realtime = Mock(available=True, execution_completed=False, output_sample_rate=24000)
    realtime_turn_outputs = iter([
        AudioOutput(audio=first), TextOutput(text="Hôm nay, "),
        AudioOutput(audio=second), TextOutput(text="giá vàng ..."),
    ])
    monkeypatch.setattr(realtime_turn, "_commit_turn_output", lambda *args: (None, realtime_turn_outputs))
    tts = Mock()
    tts.native_play_begin.side_effect = [False, True]
    result = realtime_turn.run_realtime_turn(
        realtime, tts, lambda text: text, "giá vàng?", [object()], 1.0,
        wait_filler=Mock(), harness_followup=False,
    )
    frames = [call.args[0] for call in tts.native_play_frame.call_args_list]
    assert len(frames) == 2
    np.testing.assert_array_equal(frames[0], first)
    np.testing.assert_array_equal(frames[1], second)
    tts.speak.assert_not_called()
    assert result.handled


def test_stop_during_filler_handoff_does_not_restart_native_answer():
    service = _service()
    suppressed = False

    def stop_filler():
        nonlocal suppressed
        suppressed = True
        service._lock.release()

    service.stop.side_effect = stop_filler
    service._owner_suppressed = lambda owner: suppressed
    assert not service.native_play_begin(24000, owner="interaction:stopped")
    assert not service._lock.locked()
    service._begin_playback.assert_not_called()


def test_native_admission_does_not_stop_another_native_owner():
    service = _service()
    service._native_mode = True
    try:
        assert not service.native_play_begin(24000, owner="interaction:other")
        service.stop.assert_not_called()
    finally:
        service._lock.release()
