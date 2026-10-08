"""A confident STT partial commits the turn before the final drains; a short one waits."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from hal import config as hal_config
from hal.drivers.voice import voice_service
from hal.drivers.voice._internal import config as voice_cfg
from hal.drivers.voice._internal.realtime_turn import ROUTE_HANDLED, RealtimeTurnResult


class FakeSTT:
    """Emits one partial on the first frame and the final only when closed."""

    def __init__(self, partial, final, final_delay_s=0.3):
        self._on_transcript_cb = None
        self.partial, self.final, self.final_delay_s = partial, final, final_delay_s
        self.partial_sent = False
        self.closed = False
        self.final_at = None

    def start(self, cb):
        self._on_transcript_cb = cb
        return True

    def is_closed(self):
        return self.closed

    def send_audio(self, data):
        if not self.partial_sent:
            self.partial_sent = True
            self._on_transcript_cb(self.partial, False)

    def close(self):
        if self.closed:
            return
        time.sleep(self.final_delay_s)
        self.final_at = time.monotonic()
        self.closed = True
        self._on_transcript_cb(self.final, True)


class SilentMic:
    def read(self, frame_size):
        return np.zeros(frame_size, dtype=np.int16), False


def _service():
    service = Mock()
    service._running = True
    service._np = np
    service._tts = SimpleNamespace(last_spoken_text="", last_spoken_time=0.0)
    service._tts_is_speaking = Mock(return_value=False)
    service._music_is_playing = Mock(return_value=False)
    service._automatic_reply_lock = threading.Lock()
    service._stt_drain_worker = None
    service._stt_drain_future = None
    service._rt_noise_is_speech = Mock(return_value=True)
    service._wakeword_focus.is_active.return_value = False
    service._realtime.rebuilding = False
    service._realtime.available = True
    service._realtime.sample_rate = 16000
    return service


def _run(partial, final, monkeypatch):
    monkeypatch.setattr(hal_config, "WAKEWORD_ENABLED", False)
    monkeypatch.setattr(hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(voice_cfg, "LIVE_MODE", False)
    monkeypatch.setattr(voice_cfg, "TURN_END_ENABLED", False)
    service = _service()
    service._decorator.classify_wake_word.return_value = (final, "voice")
    service._decorator.identify_and_decorate.return_value = (final, None, None)
    stt = FakeSTT(partial, final)
    calls = []

    def run_turn(self, reply_stop, realtime, tts, strip, words, *args, **kwargs):
        calls.append((time.monotonic(), words))
        return RealtimeTurnResult(route=ROUTE_HANDLED, handled=True, transcript="ok")

    metrics = Mock()
    metrics.speech_end.return_value = "vi-test"
    with patch.object(voice_service, "turn_should_close", return_value=True), \
         patch.object(voice_service, "read_voice_mode", return_value={"enabled": False, "generation": 1}), \
         patch.object(voice_service, "harness_followup_active", return_value=False), \
         patch.object(voice_service, "dispatch_turn"), \
         patch.object(voice_service, "voice_metrics", metrics), \
         patch.object(voice_service.requests, "post"), \
         patch.object(voice_service.threading, "Timer", return_value=Mock()), \
         patch.object(voice_service.VoiceService, "_run_automatic_realtime_turn", run_turn):
        voice_service.VoiceService._stream_session(
            service, SilentMic(), 320, 16000, preconnected_session=stt,
            harness_voice={"enabled": False, "generation": 1},
        )
    assert stt.closed and stt.final_at is not None
    assert len(calls) == 1, calls
    return calls[0], stt.final_at


def test_long_partial_commits_before_the_final_arrives(monkeypatch):
    (run_at, words), final_at = _run(
        "what is the weather like today", "What is the weather like today?", monkeypatch,
    )
    assert words == "what is the weather like today"
    assert run_at < final_at


def test_short_partial_waits_for_the_final(monkeypatch):
    (run_at, words), final_at = _run("hey there", "Hey there, lamp.", monkeypatch)
    assert words == "Hey there, lamp."
    assert run_at >= final_at
