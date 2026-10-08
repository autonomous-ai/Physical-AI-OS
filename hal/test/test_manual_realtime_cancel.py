"""A stop tap cancels provider output without another reply or main handoff."""

import threading
from unittest.mock import Mock

import numpy as np
import pytest

from hal.drivers.voice._internal import realtime_turn as module
from hal.realtime.models import AudioOutput, TextOutput
from hal.realtime.models.signal import LookReplaySignal
from hal.realtime.orchestrator import RealtimeOrchestrator


@pytest.fixture
def turn(monkeypatch):
    monkeypatch.setattr(module.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(module.hal_config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(module.hal_config, "REALTIME_PROVIDER", "gemini")
    monkeypatch.setattr(module.hal_config, "REALTIME_GEMINI_TURN_RETRIES", 2)
    monkeypatch.setattr(module, "gemini_needs_idle_workaround", lambda: True)
    monkeypatch.setattr(module, "native_voice", lambda tts: None)
    monkeypatch.setattr(module, "_thinking_cue_start", Mock())
    monkeypatch.setattr(module, "_thinking_cue_clear", Mock())
    monkeypatch.setattr(module, "pending_main_question", lambda: "What is your name?")
    monkeypatch.setattr(module, "take_main_followup", Mock())
    monkeypatch.setattr(module, "_reply_language_name", lambda: "English")
    realtime = Mock(available=True, execution_completed=False, output_sample_rate=24000)
    tts = Mock(_provider="test")
    tts.speak.return_value = True
    stop = threading.Event()
    filler = Mock()

    def run(**kwargs):
        return module.run_realtime_turn(
            realtime, tts, lambda text: text, "Please explain the result.",
            [np.zeros(320, dtype=np.float32)], 2.0,
            interaction_id="tap-1", stop_event=stop, wait_filler=filler,
            harness_followup=False, **kwargs,
        )

    return realtime, tts, stop, filler, run


def assert_cancelled(result, realtime):
    assert result.route == module.ROUTE_CANCELLED
    assert module.should_drop_downstream_turn(result)
    assert not result.handled
    assert not result.delegated
    realtime.save_turn.assert_not_called()
    realtime.append_audio.assert_not_called()
    module.take_main_followup.assert_not_called()


@pytest.mark.parametrize("cancel", [False, True])
def test_device_finalizer_does_not_overwrite_new_capture_visuals(turn, monkeypatch, cancel):
    realtime, _, stop, _, run = turn
    restore = Mock()
    monkeypatch.setattr("hal.routes.led.restore_led", restore)
    realtime.stream_output.return_value = iter([])
    if cancel:
        stop.set()
    run(suppress_visual_feedback=True)
    module._thinking_cue_start.assert_not_called()
    module._thinking_cue_clear.assert_not_called()
    restore.assert_not_called()


@pytest.mark.parametrize("bound", [False, True])
def test_stop_after_first_sentence_does_not_play_later_output(turn, bound):
    realtime, tts, stop, filler, run = turn
    tts.speak.side_effect = lambda *args, **kwargs: (stop.set() or True)
    consumed_later = []

    def outputs(**kwargs):
        assert kwargs["stop_event"] is stop
        yield TextOutput(text="This is the first complete sentence.")
        consumed_later.append(True)
        yield TextOutput(text="This must never be spoken.")

    realtime.stream_output.side_effect = outputs
    result = run(audio_turn=object() if bound else None)
    assert_cancelled(result, realtime)
    assert not consumed_later
    tts.speak.assert_called_once()
    tts.speak_queue.assert_not_called()
    tts.stop_realtime_reply.assert_called_once_with(turn_id="tap-1")
    realtime.recover_session.assert_called_once_with(
        "manual-capture-cancelled", discard_old_on_failure=True,
    )
    filler.cancel.assert_called()


@pytest.mark.parametrize("failure", ["end", "exception", "look"])
def test_cancelled_stream_never_retries_or_replays(turn, failure):
    realtime, tts, stop, _, run = turn

    def outputs(**kwargs):
        stop.set()
        if failure == "exception":
            raise RuntimeError("connection closed during stop")
        if failure == "look":
            yield LookReplaySignal()

    realtime.stream_output.side_effect = outputs
    assert_cancelled(run(), realtime)
    realtime.commit_audio.assert_called_once()
    realtime.recover_session.assert_called_once_with(
        "manual-capture-cancelled", discard_old_on_failure=True,
    )
    tts.speak.assert_not_called()
    tts.speak_queue.assert_not_called()


@pytest.mark.parametrize("when", ["before", "flush"])
def test_cancel_before_commit_replaces_session_with_buffered_audio(turn, when):
    realtime, tts, stop, _, run = turn
    if when == "before":
        stop.set()
    else:
        realtime.flush_output.side_effect = stop.set
    assert_cancelled(run(), realtime)
    realtime.commit_audio.assert_not_called()
    realtime.stream_output.assert_not_called()
    realtime.recover_session.assert_called_once_with(
        "manual-capture-cancelled", discard_old_on_failure=True,
    )
    realtime.discard_open_activity.assert_not_called()
    tts.speak.assert_not_called()


def test_cancel_native_playback_releases_speaker_without_history(turn, monkeypatch):
    realtime, tts, stop, _, run = turn
    monkeypatch.setattr(module.hal_config, "REALTIME_NATIVE_AUDIO", True)
    tts.native_play_begin.return_value = True
    tts.native_play_frame.side_effect = lambda frame: (stop.set() or True)

    def outputs(**kwargs):
        yield AudioOutput(audio=np.zeros(100, dtype=np.float32), transcript="Hello")
        pytest.fail("read past native playback cancellation")

    realtime.stream_output.side_effect = outputs
    assert_cancelled(run(), realtime)
    tts.native_play_frame.assert_called_once()
    tts.stop_realtime_reply.assert_called_once_with(turn_id="tap-1")
    tts.native_play_end.assert_called_once_with()


@pytest.mark.parametrize("discard", [False, True])
def test_failed_cancel_recovery_cannot_reuse_old_provider(discard):
    orchestrator = object.__new__(RealtimeOrchestrator)
    old = Mock()
    replacement = Mock()
    replacement.connect.side_effect = RuntimeError("provider unavailable")
    orchestrator._agent = old
    orchestrator._context = Mock()
    orchestrator._make_agent = Mock(return_value=replacement)
    orchestrator._rebuild_lock = threading.Lock()
    orchestrator._rebuild_done = threading.Event()
    # Keep the test deterministic while executing the real disconnect action.
    orchestrator._disconnect_in_background = lambda agent, reason: agent.disconnect()

    assert not orchestrator.recover_session(
        "manual-capture-cancelled", discard_old_on_failure=discard,
    )
    replacement.disconnect.assert_called_once()
    assert orchestrator._rebuild_done.is_set()
    if discard:
        assert orchestrator._agent is None
        old.disconnect.assert_called_once()
    else:
        assert orchestrator._agent is old
        old.disconnect.assert_not_called()
