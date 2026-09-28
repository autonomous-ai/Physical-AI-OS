"""Provider rejection cancels actual queued speech independent of its wording."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hal.drivers.voice.tts.service import TTSService, _PendingSpeech
from hal.drivers.voice.voice_service import VoiceService
from hal.realtime.models.output import TextOutput, UserSpeechOutput, ExecutionOutput
from hal.realtime.models.signal import RejectSignal
from hal.test.test_live_history import Sender
from hal.test.test_live_voice_metrics import _pump
from hal.test.test_voice_metrics import kpi  # noqa: F401
from hal.telemetry import voice_metrics


class DelayedSpeaker(TTSService):
    """Use the real cancellation code; delay PCM readiness until test flush."""
    def __init__(self):
        self._pending_queue_lock = threading.Lock()
        self._pending_queue = []
        self._stop_event = threading.Event()
        self._wake_drain_queues = Mock()
        self._speaking = False
        self._native_mode = False
        self._realtime_reply = False
        self._realtime_feedback = False
        self._playback_owner = ""
        self._active_pending_speech = None
        self.played = []
        self.items = []

    def speak(self, text, *, turn_id="", realtime_reply=False):
        item = _PendingSpeech(text, False, owner="run:" + turn_id,
                              realtime_reply=realtime_reply)
        self.items.append(item)
        self._pending_queue.append(item)
        return True

    speak_queue = speak

    def flush(self):
        for item in self._pending_queue:
            if not item.cancelled.is_set():
                self.played.append(item.text)
                voice_metrics.playback_audio(item.owner, SimpleNamespace(realtime_reply=True))
        self._pending_queue.clear()


@pytest.mark.parametrize("reply", [
    "Rất tiếc, đã xảy ra một lỗi hệ thống.",
    "Máy chủ tạm thời gặp trục trặc, bạn quay lại sau nhé.",
    "Something broke internally. Please try again later.",
    "Something broke internally. Please try",
    "申し訳ありません。内部エラーが発生しました。",
])
def test_late_reject_cancels_synthesis_before_audio_and_never_syncs(monkeypatch, kpi, reply):
    tts, sender = DelayedSpeaker(), Sender()
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="rejected", transcript="Thôi."),
        TextOutput(text=reply, user_turn_id="rejected"),
        RejectSignal(user_turn_id="rejected"),
        TextOutput(text="Unwanted ACK followup.", user_turn_id="rejected"),
        ExecutionOutput(user_turn_id="rejected", execution_completed=True),
        tts.flush,
    ], "rejected", True)], tts=tts, sender=sender,
        strip_markers=VoiceService.strip_rt_markers)
    if "." in reply:
        assert tts.items  # ASCII sentences entered synthesis before rejection.
    assert all(item.cancelled.is_set() for item in tts.items)
    assert tts.played == []
    assert sender.calls == []
    kpi.close_all()
    row = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert row["ack_kind"] == ""
    assert row["task_exclusion_reason"] == "rejected_non_user"
    assert kpi.of("voice_metrics_task_execution") == []


def test_late_old_reject_preserves_new_reply_and_history(monkeypatch, kpi):
    tts, sender = DelayedSpeaker(), Sender()
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="old", transcript="Thôi."),
        TextOutput(text="Old reply.", user_turn_id="old"),
        UserSpeechOutput(turn_id="new", transcript="Xin chào."),
        TextOutput(text="Xin chào.", user_turn_id="new"),
        RejectSignal(user_turn_id="old"),
        tts.flush,
    ], "new", True)], tts=tts, sender=sender)
    assert tts.played == ["Xin chào."]
    assert sender.sent.wait(2)
    assert len(sender.calls) == 1
    assert sender.calls[0][0].endswith("[REPLY] Xin chào.")


def test_rejected_partial_sentence_is_not_flushed_at_terminal(monkeypatch, kpi):
    tts, sender = DelayedSpeaker(), Sender()
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u", transcript="Thôi."),
        TextOutput(text="unfinished", user_turn_id="u"),
        RejectSignal(user_turn_id="u"),
    ], "u", True)], tts=tts, sender=sender)
    assert tts.items == []
    assert sender.calls == []
