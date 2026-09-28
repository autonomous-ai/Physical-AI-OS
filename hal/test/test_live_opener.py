"""Confirmed STT openers enter full-duplex LIVE without duplicate dispatch."""

from types import MethodType
from unittest.mock import Mock

import numpy as np
import pytest

from hal import config
from hal.drivers.voice import voice_service as module
from hal.drivers.voice.voice_service import VoiceService
from hal.drivers.voice._internal.wakeword_focus import WakeWordFocus
from hal.realtime.models.output import AudioOutput, TextOutput, UserSpeechOutput, InterruptedOutput
from hal.realtime.models.signal import DelegateSignal, RejectSignal
from hal.telemetry import voice_metrics
from hal.test.test_live_voice_metrics import _pump
from hal.test.test_voice_metrics import kpi  # noqa: F401

NORMAL = {"enabled": False, "generation": 0}


def opener_context(kpi):
    return dict(key="", consumed=False, transcript="Hello Lamp, can you hear me?",
                interaction_id=voice_metrics.speech_end("local_silence", at=kpi.clock()),
                voice_turn_type="voice_command")


@pytest.mark.parametrize("consumed,reopen", [(True, False), (True, True), (False, False)])
def test_promotion_hands_complete_audio_to_live_once(monkeypatch, consumed, reopen):
    monkeypatch.setattr(module.voice_cfg, "LIVE_MODE", True)
    monkeypatch.setattr(config, "REALTIME_ENABLED", True)
    service = object.__new__(VoiceService)
    service._realtime = Mock()
    service._realtime.wait_until_available.return_value = True
    frames = [b"first", b"last", b"silence"]
    mic = object()

    def live(*args, **kwargs):
        assert args == (mic, 320, 16000, frames)
        assert kwargs["harness_voice"] == NORMAL
        assert kwargs["opener"]["interaction_id"] == "original"
        kwargs["opener"]["consumed"] = consumed
        return reopen

    service._live_session = Mock(side_effect=live)
    assert service._try_live_opener(mic, 320, 16000, frames,
        transcript="Hello Lamp", interaction_id="original", harness_voice=NORMAL) == (consumed, reopen)
    service._live_session.assert_called_once()
    service._realtime.commit_audio.assert_not_called()
    service._realtime.append_audio.assert_not_called()


@pytest.mark.parametrize("live,enabled,harness,available", [
    (False, True, NORMAL, True), (True, False, NORMAL, True),
    (True, True, {"enabled": True}, True),
    (True, True, {"enabled": True, "unavailable": True}, True),
    (True, True, NORMAL, False),
])
def test_promotion_unavailable_or_bypassed_keeps_fallback(monkeypatch, live, enabled, harness, available):
    monkeypatch.setattr(module.voice_cfg, "LIVE_MODE", live)
    monkeypatch.setattr(config, "REALTIME_ENABLED", enabled)
    service = object.__new__(VoiceService)
    service._realtime = Mock()
    service._realtime.wait_until_available.return_value = available
    service._live_session = Mock()
    assert service._try_live_opener(object(), 320, 16000, [b"audio"],
        transcript="Hello Lamp", interaction_id="original", harness_voice=harness) == (False, False)
    service._live_session.assert_not_called()
    service._realtime.commit_audio.assert_not_called()


@pytest.mark.parametrize("consumed", [False, True])
def test_error_preserves_whether_opener_already_answered(monkeypatch, consumed):
    monkeypatch.setattr(module.voice_cfg, "LIVE_MODE", True)
    monkeypatch.setattr(config, "REALTIME_ENABLED", True)
    service = object.__new__(VoiceService)
    service._realtime = Mock()
    service._live_stop_output = Mock()

    def live(*args, **kwargs):
        kwargs["opener"]["consumed"] = consumed
        raise RuntimeError("transport failed")

    service._live_session = live
    assert service._try_live_opener(object(), 320, 16000, [b"audio"],
        transcript="Hello Lamp", interaction_id="original", harness_voice=NORMAL) == (consumed, True)


def test_opener_and_followup_have_two_interactions_not_three(monkeypatch, kpi):
    opener = opener_context(kpi)
    original_endpoint = kpi.clock()
    kpi.clock.advance(2000)
    spoken = _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u1", transcript="Hello Lamp", endpoint_at=kpi.clock(), method="server_vad"),
        TextOutput(text="Yes, I hear you.", user_turn_id="u1"),
    ], "u1", True), ([
        UserSpeechOutput(turn_id="u2", transcript="How are you?", endpoint_at=kpi.clock(), method="server_vad"),
        TextOutput(text="Doing well.", user_turn_id="u2"),
    ], "u2", True)], opener=opener)
    assert opener["consumed"]
    assert ("Yes, I hear you.", opener["interaction_id"]) in spoken
    kpi.close_all()
    rows = [e["params"] for e in kpi.of(voice_metrics.EVENT_INTERACTION)]
    assert len(rows) == 2
    first = next(r for r in rows if r["interaction_id"] == opener["interaction_id"])
    assert first["answer_latency_ms"] == 2000
    assert original_endpoint < kpi.clock()
    assert len(kpi.of("voice_metrics_task_execution")) == 2


@pytest.mark.parametrize("outputs,consumed", [
    ([], False),
    ([UserSpeechOutput(turn_id="u1", transcript="Hello Lamp")], False),
    ([UserSpeechOutput(turn_id="u1"), RejectSignal(user_turn_id="u1")], True),
    ([UserSpeechOutput(turn_id="u1"), InterruptedOutput(reason="server_interrupt", user_turn_id="u1")], True),
    ([UserSpeechOutput(turn_id="u1"), UserSpeechOutput(turn_id="u2", transcript="Stop")], True),
])
def test_silent_fallback_but_no_replay_after_reject_or_new_turn(monkeypatch, kpi, outputs, consumed):
    opener = opener_context(kpi)
    _pump(monkeypatch, kpi, [(outputs, "u1", False)], opener=opener)
    assert opener["consumed"] is consumed


@pytest.mark.parametrize("native,outputs,consumed", [
    (True, [TextOutput(text="Transcript without audio", user_turn_id="u1")], False),
    (True, [AudioOutput(audio=np.zeros(32), user_turn_id="u1")], True),
    (False, [TextOutput(text="Yes.", user_turn_id="u1")], True),
])
def test_native_opener_needs_audio_not_just_transcript(monkeypatch, kpi, native, outputs, consumed):
    opener = opener_context(kpi)
    _pump(monkeypatch, kpi, [([UserSpeechOutput(turn_id="u1")] + outputs, "u1", False)],
          native=native, opener=opener)
    assert opener["consumed"] is consumed


def test_delegate_without_input_transcription_uses_original_id_and_text(monkeypatch, kpi):
    opener = opener_context(kpi)
    dispatch = Mock()
    monkeypatch.setattr(module, "dispatch_turn", dispatch)
    _pump(monkeypatch, kpi, [([
        DelegateSignal(user_turn_id="u1", transcript="", message="Check memory"),
    ], "u1", False)], opener=opener)
    assert opener["consumed"]
    dispatch.assert_called_once()
    assert dispatch.call_args.args[2] == opener["transcript"]
    assert dispatch.call_args.kwargs["interaction_id"] == opener["interaction_id"]


@pytest.mark.parametrize("partial,final,consumed,expected_dispatch", [
    ("Hello Lamp", "Hello Lamp can you hear me", True, False),
    ("Hello Lamp", "Hello Lamp can you hear me", False, True),
    ("Hello Lamp", "Hello Mom can you hear me", True, False),
    ("What time is it", "What time is it", True, False),
])
def test_final_wake_confirmation_controls_promotion_and_fallback(
    monkeypatch, partial, final, consumed, expected_dispatch,
):
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", True)
    monkeypatch.setattr(config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(module.voice_cfg, "LIVE_MODE", True)
    service = Mock()
    service._running = False
    service._tts = None
    service._wakeword_focus = WakeWordFocus(20)
    service._realtime.rebuilding = False
    service._realtime.available = True
    service._music_is_playing.return_value = False
    service._decorator.starts_with_wake_word.side_effect = lambda t: t.lower().startswith("hello lamp")
    service._decorator.matches_wake_word_loosely.return_value = False
    service._decorator.classify_wake_word.return_value = (final, "voice_command")
    service._decorator.identify_and_decorate.return_value = (final, None, None)
    service._try_live_opener = MethodType(VoiceService._try_live_opener, service)
    stt = Mock()
    stt.is_closed.return_value = False

    def finish():
        stt._on_transcript_cb(partial, False)
        service._live_session.assert_not_called()
        stt._on_transcript_cb(final, True)

    stt.close.side_effect = finish
    frames = [b"\x00\x00" * 320]

    def live(*args, **kwargs):
        assert args[3] == frames
        kwargs["opener"]["consumed"] = consumed
        return True

    service._live_session.side_effect = live
    monkeypatch.setattr(module, "finalize_session", lambda *a: (final, frames, 2.0))
    dispatch = Mock()
    manual = Mock()
    monkeypatch.setattr(module, "dispatch_turn", dispatch)
    monkeypatch.setattr(module, "run_realtime_turn", manual)
    monkeypatch.setattr(module, "voice_metrics", Mock())
    monkeypatch.setattr(module.requests, "post", Mock())
    reopened = VoiceService._stream_session(service, Mock(), 320, 16000,
        preconnected_session=stt, speech_pre_buffer=frames, harness_voice=NORMAL)
    authorized = final.startswith("Hello Lamp")
    assert service._live_session.called is authorized
    assert bool(reopened) is authorized
    assert dispatch.called is expected_dispatch
    manual.assert_not_called()


def test_real_live_session_keeps_mic_streaming_during_opener_reply(monkeypatch, kpi):
    import threading
    from types import SimpleNamespace

    monkeypatch.setattr(config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", False)
    monkeypatch.setattr(module.voice_cfg, "LIVE_UPLINK_DUMP_DIR", "")
    monkeypatch.setattr(module, "LiveVoiceCues", Mock())
    monkeypatch.setattr(module, "read_voice_mode", lambda: NORMAL)
    monkeypatch.setattr(module.aec, "active", lambda: False)
    ready, spoke, captured = threading.Event(), threading.Event(), threading.Event()
    service = object.__new__(VoiceService)
    service._running = True
    service._live_generation = 0
    service._silence_vad = None
    service._np = np
    service._wakeword_focus = WakeWordFocus(20)
    service._music_is_playing = lambda: False
    service._tts_is_speaking = spoke.is_set
    service._live_uplink_frame = lambda data: data
    service._to_realtime = lambda frame: frame
    service.strip_rt_markers = lambda text: text
    service._decorator = SimpleNamespace(classify_wake_word=lambda t: (t, "voice_command"))
    service._sensing_sender = Mock()
    calls = []
    frames = [b"speech", b"trailing silence"]

    class Provider:
        execution_completed = False
        execution_turn_id = ""
        output_sample_rate = 24000

        def flush_output(self):
            calls.append("flush")

        def set_live_active(self, value):
            calls.append(("live", value))

        def end_live_audio(self):
            calls.append("audio_end")

        def append_audio(self, frame):
            calls.append(("audio", frame))
            if len([c for c in calls if isinstance(c, tuple) and c[0] == "audio"]) == 2:
                ready.set()

        def stream_output(self, *, stop_event=None):
            assert ready.wait(2), "pre-roll never reached provider"
            yield UserSpeechOutput(turn_id="u1", transcript="Hello Lamp")
            yield TextOutput(text="Yes, I hear you.", user_turn_id="u1")
            assert captured.wait(2), "mic stopped while replying"
            service._running = False

    def queue_reply(*args, **kwargs):
        spoke.set()
        return True

    service._tts = SimpleNamespace(speak=queue_reply, speak_queue=queue_reply,
                                   realtime_speaking=False, speaking=False)
    service._realtime = Provider()

    class Mic:
        def read(self, size):
            assert spoke.wait(2), "opener was not answered"
            captured.set()
            return np.zeros((size, 1), dtype=np.int16), False

    opener = opener_context(kpi)
    service._live_session(Mic(), 320, 16000, frames, harness_voice=NORMAL, opener=opener)
    assert captured.is_set() and opener["consumed"]
    audio = [c[1] for c in calls if isinstance(c, tuple) and c[0] == "audio"]
    assert audio[:2] == frames
    assert len(audio) > 2  # Real-time mic frames followed the captured opener.
    assert calls.index("flush") < calls.index(("audio", frames[0]))


@pytest.mark.parametrize("outputs,native,consumed,completed", [
    ([], False, False, 0),
    ([TextOutput(text="Transcript only", user_turn_id="u1")], True, False, 0),
    ([TextOutput(text="A reply without punctuation", user_turn_id="u1")], False, True, 1),
])
def test_silent_provider_terminal_does_not_complete_main_fallback_task(
    monkeypatch, kpi, outputs, native, consumed, completed,
):
    opener = opener_context(kpi)
    _pump(monkeypatch, kpi, [([UserSpeechOutput(turn_id="u1")] + outputs, "u1", True)],
          native=native, opener=opener)
    assert opener["consumed"] is consumed
    assert len(kpi.of("voice_metrics_task_execution")) == completed


def test_stopped_promoted_session_cannot_enqueue_buffered_tail(monkeypatch, kpi):
    captured = {}
    original = VoiceService._live_out_pump

    def pump(self, *args, **kwargs):
        captured["service"] = self
        return original(self, *args, **kwargs)

    monkeypatch.setattr(VoiceService, "_live_out_pump", pump)
    opener = opener_context(kpi)
    spoken = _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u1"),
        TextOutput(text="Hello", user_turn_id="u1"),
        lambda: setattr(captured["service"], "_live_running", False),
    ], "u1", False)], opener=opener)
    assert spoken == []
    assert not opener["consumed"]
