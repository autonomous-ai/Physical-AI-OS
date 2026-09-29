"""Live LED state transitions must not change playback or measurement."""

import numpy as np
import pytest

from hal.drivers.voice._internal.live_cues import LiveVoiceCues
from hal.realtime.models.output import AudioOutput, TextOutput, UserSpeechOutput
from hal.test.test_live_voice_metrics import _pump
from hal.test.test_voice_metrics import kpi  # noqa: F401


def cues_fixture():
    now = [0.0]
    events = []
    cues = LiveVoiceCues(clock=lambda: now[0],
                         show=lambda owner, phase: events.append((owner, phase)),
                         clear=lambda owner: events.append((owner, "clear")))
    return cues, now, events


def test_empty_input_endpoint_and_idle_never_start_emotion():
    cues, now, events = cues_fixture()
    for i in range(20):
        cues.input(str(i), endpoint_at=float(i), transcript="   ")
        now[0] += 1
        cues.tick()
    assert events == []
    cues.close()
    assert events == []


def test_partial_transcript_does_not_infer_thinking_from_silence():
    cues, now, events = cues_fixture()
    cues.input("turn", transcript="hello")
    now[0] = 0.9
    cues.tick()
    assert [e[1] for e in events] == ["listening"]
    now[0] = 8.1
    cues.tick()
    assert [e[1] for e in events] == ["listening", "clear"]
    cues.input("turn", endpoint_at=9)
    assert events[-1][1] == "clear"


@pytest.mark.parametrize("final", [False, True])
def test_recognized_turn_thinks_only_after_provider_end(final):
    cues, _, events = cues_fixture()
    cues.input("turn", transcript="hello")
    cues.input("turn", endpoint_at=None if final else 0.1, transcript_finished=final)
    assert [e[1] for e in events] == ["listening", "thinking"]
    cues.finish("turn")
    cues.input("turn", transcript="late", endpoint_at=0.2)
    assert [e[1] for e in events] == ["listening", "thinking", "clear"]


def test_endpoint_before_transcript_waits_for_words():
    cues, _, events = cues_fixture()
    cues.input("turn", endpoint_at=0.1)
    assert not events
    cues.input("turn", transcript="hello")
    assert [e[1] for e in events] == ["listening", "thinking"]


def test_stale_terminal_does_not_clear_newer_recognized_turn():
    cues, _, events = cues_fixture()
    cues.input("first", transcript="hello", endpoint_at=0.1)
    cues.input("second", transcript="new question")
    latest = events[-1]
    cues.finish("first")
    cues.input("first", transcript="late", endpoint_at=0.1)
    assert events[-1] == latest
    cues.finish("second")
    assert events[-1] == (latest[0], "clear")


def test_timeout_and_session_close_do_not_allow_late_repaint():
    cues, now, events = cues_fixture()
    cues.input("turn", transcript="hello", endpoint_at=0.0)
    now[0] = 26.0
    cues.tick()
    assert events[-1][1] == "clear"
    cues.close()
    count = len(events)
    cues.input("next", transcript="hello", endpoint_at=27)
    cues.tick()
    cues.finish("turn")
    assert len(events) == count


def test_led_failure_does_not_escape_into_voice_pipeline():
    def broken(*args):
        raise RuntimeError("LED unavailable")
    cues = LiveVoiceCues(show=broken, clear=broken)
    cues.input("turn", transcript="hello", endpoint_at=0.0)
    cues.finish("turn")
    cues.close()


@pytest.mark.parametrize("native", [True, False])
def test_live_pump_cues_end_at_reply_without_extra_playback(monkeypatch, kpi, native):
    cues, _, events = cues_fixture()
    output = (AudioOutput(audio=np.zeros(16, dtype=np.float32), user_turn_id="a")
              if native else TextOutput(text="Hello there.", user_turn_id="a"))
    batches = [([UserSpeechOutput(turn_id="a", transcript="hello", endpoint_at=0.2), output], "a", True)]
    spoken = _pump(monkeypatch, kpi, batches, native=native, cues=cues)
    assert [e[1] for e in events] == ["listening", "thinking", "clear"]
    if not native:
        assert len(spoken) == 1


def test_live_reuses_regular_hw_emotion_calls(monkeypatch):
    from hal import app_state
    from hal.drivers.voice.voice_service import VoiceService
    from hal.drivers.voice._internal import realtime_turn

    calls = []
    monkeypatch.setattr(VoiceService, "_set_emotion_local", lambda emotion: calls.append(emotion))
    monkeypatch.setattr(realtime_turn, "_thinking_cue_start", lambda: calls.append("thinking"))
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", lambda: calls.append("clear_thinking"))
    monkeypatch.setattr(app_state, "clear_listening_cue", lambda: calls.append("clear_listening"))
    cues = LiveVoiceCues()
    try:
        cues.input("turn", transcript="hello", endpoint_at=0.1)
        cues.finish("turn")
        assert calls == ["listening", "thinking", "clear_thinking", "clear_listening"]
    finally:
        cues.close()


def test_hw_effect_wait_does_not_block_mic_feedback(monkeypatch):
    import threading

    entered = threading.Event()
    release = threading.Event()

    def slow_emotion(*args):
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(LiveVoiceCues, "_show_emotion", staticmethod(slow_emotion))
    monkeypatch.setattr(LiveVoiceCues, "_clear_emotion", staticmethod(lambda owner: None))
    cues = LiveVoiceCues()
    try:
        cues.input("turn", transcript="hello")
        assert entered.wait(1)
        cues.input("turn", transcript="hello")
        cues.tick()
        assert not release.is_set()
    finally:
        release.set()
        cues.close()


@pytest.mark.parametrize("wake,focus,text,expected", [
    (False, False, "hello", True),
    (True, False, "hello", False),
    (True, False, "hey lamp hello", True),
    (True, True, "hello", True),
])
def test_live_emotion_uses_regular_addressing_gate(monkeypatch, wake, focus, text, expected):
    from types import SimpleNamespace
    from hal import config
    from hal.drivers.voice.voice_service import VoiceService

    monkeypatch.setattr(config, "WAKEWORD_ENABLED", wake)
    service = object.__new__(VoiceService)
    service._decorator = SimpleNamespace(starts_with_wake_word=lambda text: text.startswith("hey lamp"))
    service._wakeword_focus = SimpleNamespace(is_active=lambda: focus)
    assert service._live_emotion_addressed(text) is expected


def test_focus_latches_for_one_turn_and_can_arrive_after_transcript():
    focus = [False]
    events = []
    cues = LiveVoiceCues(addressed=lambda text: focus[0] or text.startswith("hey lamp"),
                         show=lambda owner, phase: events.append(phase),
                         clear=lambda owner: events.append("clear"))
    cues.input("a", transcript="hello")
    assert not events
    focus[0] = True
    cues.tick()
    assert events == ["listening"]
    focus[0] = False
    cues.input("a", endpoint_at=1)
    assert events[-1] == "thinking"
    cues.finish("a")
    count = len(events)
    cues.input("b", transcript="other conversation", endpoint_at=2)
    assert len(events) == count
    cues.input("c", transcript="hey")
    assert len(events) == count
    cues.input("c", transcript="lamp hello")
    assert events[-1] == "listening"


@pytest.mark.parametrize("native", [False, True])
def test_noise_metadata_does_not_change_playback_or_metrics(monkeypatch, kpi, native):
    cues, _, events = cues_fixture()
    output = (AudioOutput(audio=np.zeros(16, dtype=np.float32), user_turn_id="noise")
              if native else TextOutput(text="Hello there.", user_turn_id="noise"))
    batches = [([UserSpeechOutput(turn_id="noise", endpoint_at=0.2), output], "noise", True)]
    spoken = _pump(monkeypatch, kpi, batches, native=native, cues=cues)
    assert events == []
    assert len(spoken) == 1


def test_finished_only_metadata_is_not_a_metric_observation(monkeypatch, kpi):
    from unittest.mock import patch
    from hal.telemetry.live_voice import LiveVoiceMetrics

    cues, _, events = cues_fixture()
    with patch.object(LiveVoiceMetrics, "speech", return_value="") as observe:
        _pump(monkeypatch, kpi, [([
            UserSpeechOutput(turn_id="a", transcript="hello"),
            UserSpeechOutput(turn_id="a", transcript_finished=True),
            TextOutput(text="Hello there.", user_turn_id="a"),
        ], "a", True)], cues=cues)
    assert observe.call_count == 1
    assert [e[1] for e in events] == ["listening", "thinking", "clear"]
