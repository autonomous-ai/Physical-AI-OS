"""An explicit stop refuses late audio of the stopped turn at TTS admission.

Wires the REAL voice_metrics tracker to a TTSService shell: the click stamps
the boundary, then a Harness-style reply, a realtime wait filler and native
audio for the stopped turn are all refused, while a newer turn still gets
through to the speaker lock. Device-observed 2026-09-17: replies played
12-28 s after the click because these paths bypassed os-server's watermark.
"""

import threading
from types import SimpleNamespace

import pytest

from hal.drivers.voice.tts.service import TTSService
from hal.telemetry import client, voice_metrics


@pytest.fixture
def tts(monkeypatch):
    monkeypatch.setattr(voice_metrics.client, "report", lambda *a, **k: None)
    monkeypatch.setattr(client, "report", lambda *a, **k: None)
    monkeypatch.setattr(voice_metrics.threading, "Timer",
                        lambda *a, **k: SimpleNamespace(start=lambda: None, cancel=lambda: None,
                                                        daemon=False))
    voice_metrics.reset_for_test()
    service = object.__new__(TTSService)
    service._backend = SimpleNamespace(available=True)
    service._sd = object()
    service._provider = "test"
    service._voice = ""
    service._model = ""
    service._speed = 1.0
    service._speaker_muted = lambda: False
    service._on_playback_muted = None
    service._on_unspoken_reply = None
    # Held lock + non-interruptible: an admitted request stops at "busy",
    # which is how the test tells "admitted" from "refused" without audio.
    service._lock = threading.Lock()
    service._lock.acquire()
    service._interruptible = False
    service._pending_queue = []
    service._pending_queue_lock = threading.Lock()
    service._queue_request_lock = threading.Lock()
    yield service
    voice_metrics.reset_for_test()


def _stopped_turn():
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "device-chat-1-1789600000000")
    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)
    return old


def test_stopped_turn_is_refused_on_every_admission_path(tts, caplog):
    old = _stopped_turn()
    caplog.set_level("INFO")
    # Harness reply (turn-owned speak) and realtime wait filler (turn_id is the
    # interaction id itself, as os-server forwards it) both refused before the lock.
    assert tts.speak("late Harness recap", turn_id="device-chat-1-1789600000000") is False
    assert tts.speak_cached("one moment", interruptible=True, turn_id=old) is False
    assert tts.native_play_begin(24000, owner=f"interaction:{old}") is False
    refused = [r for r in caplog.records if "turn stopped by user" in r.getMessage()]
    assert len(refused) == 3
    assert not any("busy" in r.getMessage() for r in caplog.records)


def test_newer_turn_and_unowned_audio_still_reach_the_speaker(tts, caplog):
    _stopped_turn()
    new = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(new, "device-chat-2-1789600001000")
    caplog.set_level("INFO")
    assert tts.speak("fresh answer", turn_id="device-chat-2-1789600001000") is False
    assert tts.speak("unowned notice") is False
    assert not any("turn stopped by user" in r.getMessage() for r in caplog.records)
    assert sum("busy" in r.getMessage() for r in caplog.records) == 2
