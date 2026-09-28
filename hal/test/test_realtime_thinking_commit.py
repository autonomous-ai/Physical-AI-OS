"""Hardware feedback must not postpone the provider's audio commit."""

from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import Mock

import pytest

from hal.drivers.voice._internal import realtime_turn
from hal.realtime.models import TextOutput
from hal.realtime.models.signal import LookReplaySignal


@pytest.fixture
def turn(monkeypatch):
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_PROVIDER", "gemini")
    monkeypatch.setattr(realtime_turn, "gemini_needs_idle_workaround", lambda: False)
    monkeypatch.setattr(realtime_turn, "_reply_language_name", lambda: "English")
    cue, clear, filler = Mock(), Mock(), Mock()
    monkeypatch.setattr(realtime_turn, "_thinking_cue_start", cue)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", clear)
    realtime, tts = Mock(available=True), Mock()
    realtime.stream_output.return_value = iter([TextOutput(text="I'm here.")])

    def run():
        return realtime_turn.run_realtime_turn(
            realtime, tts, lambda text: text, "Hello, are you there?",
            [b"audio"], 1.0, wait_filler=filler, harness_followup=False,
        )

    return run, realtime, tts, cue, clear, filler


def test_provider_committed_while_hardware_cue_is_blocked(turn):
    run, realtime, tts, cue, clear, _ = turn
    entered, release = threading.Event(), threading.Event()

    def blocked_hardware():
        entered.set()
        assert release.wait(2), "test failed to release hardware cue"

    cue.side_effect = blocked_hardware
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(run)
        try:
            assert entered.wait(2)
            realtime.commit_audio.assert_called_once_with()
            # Hardware stays serialized with this turn's reply/clear: no late
            # background thinking can repaint the device after TTS starts.
            tts.speak.assert_not_called()
            clear.assert_not_called()
        finally:
            release.set()
        assert future.result(timeout=2).handled
    cue.assert_called_once_with()
    tts.speak.assert_called_once()
    clear.assert_called_once_with()


@pytest.mark.parametrize("replay", ["look", "retry"])
def test_thinking_is_not_restarted_for_same_turn_replay(turn, monkeypatch, replay):
    run, realtime, _, cue, _, _ = turn
    if replay == "retry":
        monkeypatch.setattr(realtime_turn, "gemini_needs_idle_workaround", lambda: True)
        first = []
    else:
        first = [LookReplaySignal()]
    realtime.stream_output.side_effect = [iter(first), iter([TextOutput(text="I'm here.")])]
    assert run().handled
    assert realtime.commit_audio.call_count == 2
    cue.assert_called_once_with()


def test_failed_commit_does_not_start_thinking_and_cancels_filler(turn):
    run, realtime, tts, cue, clear, filler = turn
    realtime.commit_audio.side_effect = RuntimeError("transport unavailable")
    result = run()
    assert result.route == realtime_turn.ROUTE_ERROR
    assert result.delegated
    cue.assert_not_called()
    clear.assert_called_once_with()
    filler.cancel.assert_called_once_with()
    tts.speak.assert_not_called()
