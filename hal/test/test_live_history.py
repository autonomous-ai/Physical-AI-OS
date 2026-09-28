"""Live history reaches OS once per attributed completion, without blocking audio."""

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from hal.drivers.voice._internal.live_history import LiveHistory
from hal.realtime.models.output import AudioOutput, InterruptedOutput, TextOutput, UserSpeechOutput
from hal.realtime.models.signal import DelegateSignal, RejectSignal
from hal.test.test_live_voice_metrics import _pump
from hal.test.test_voice_metrics import kpi  # noqa: F401 -- fake telemetry transport


class Sender:
    def __init__(self):
        self.calls = []
        self.sent = threading.Event()

    def send(self, message, **kwargs):
        self.calls.append((message, kwargs))
        self.sent.set()
        return SimpleNamespace(run_id="device-chat-context-test", speech_suppressed=False)


@pytest.mark.parametrize("native", [False, True])
def test_live_pump_syncs_completed_exchange_across_receive_timeouts(monkeypatch, kpi, native):
    sender = Sender()
    output = (AudioOutput(audio=np.zeros(32, dtype=np.float32), transcript="Hello there.", user_turn_id="u1")
              if native else TextOutput(text="Hello there.", user_turn_id="u1"))
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u1", transcript="Say "),
        UserSpeechOutput(turn_id="u1", transcript="hello"),
        output,
    ], "", False), ([], "u1", True), ([], "u1", True)], native=native,
        sender=sender, harness_voice={"enabled": False, "generation": 1})
    assert sender.sent.wait(2)
    assert len(sender.calls) == 1
    message, args = sender.calls[0]
    assert message == "[skills: input-branching]\n[HANDLED] Say hello\n[REPLY] Hello there."
    assert args["event_type"] == "voice_agent_handled"
    assert args["skip_echo"] is True
    assert args["interaction_id"]
    assert args["harness_voice"]["enabled"] is False


@pytest.mark.parametrize("signal", [
    RejectSignal(user_turn_id="u1"),
    DelegateSignal(user_turn_id="u1", message="Ask main", transcript="Question"),
    InterruptedOutput(reason="server_interrupt", user_turn_id="u1"),
])
def test_live_pump_does_not_sync_rejected_delegated_or_interrupted_turn(monkeypatch, kpi, signal):
    monkeypatch.setattr("hal.drivers.voice.voice_service.dispatch_turn", lambda *args, **kwargs: None)
    sender = Sender()
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u1", transcript="Question"),
        TextOutput(text="Partial.", user_turn_id="u1"), signal,
    ], "u1", True)], sender=sender)
    assert sender.calls == []


def test_history_never_pairs_an_unowned_answer_with_later_input():
    sender = Sender()
    history = LiveHistory(sender)
    history.output("", "Unsolicited answer")
    history.input("u1", "New question", "interaction-1")
    history.complete("", True)
    history.complete("u1", True)
    history.close()
    assert sender.calls == []


def test_history_http_does_not_block_completion_or_close():
    entered, release = threading.Event(), threading.Event()

    class SlowSender(Sender):
        def send(self, message, **kwargs):
            entered.set()
            assert release.wait(3)
            return super().send(message, **kwargs)

    sender = SlowSender()
    history = LiveHistory(sender)
    try:
        history.input("u1", "Question", "interaction-1")
        history.output("u1", "Answer")
        history.complete("u1", True)
        assert entered.wait(1)
        history.close()
        assert not sender.sent.is_set()
    finally:
        release.set()
        history._queue.join()
    assert sender.sent.is_set()


def test_history_reset_and_discard_keep_concurrent_turns_separate():
    sender = Sender()
    history = LiveHistory(sender)
    history.input("old", "Old question", "old-iid")
    history.output("old", "Old answer")
    history.discard("old")
    history.input("new", "New question", "new-iid")
    history.output("new", "Draft")
    history.reset_output("new")
    history.output("new", "Final answer")
    history.complete("old", True)
    history.complete("new", True)
    history.close()
    history._queue.join()
    assert len(sender.calls) == 1
    assert "Old" not in sender.calls[0][0]
    assert "Draft" not in sender.calls[0][0]
    assert sender.calls[0][1]["interaction_id"] == "new-iid"


@pytest.mark.parametrize("chunks", [["{pause}"], ["{", "pa", "use", "}"], ["{ PAUSE }"]])
@pytest.mark.parametrize("answer", ["", "Mình nghe rõ."])
def test_pause_marker_does_not_become_tts_or_main_context(monkeypatch, kpi, chunks, answer):
    from hal.drivers.voice.voice_service import VoiceService

    sender = Sender()
    outputs = [UserSpeechOutput(turn_id="pause-turn", transcript="với")]
    outputs.extend(TextOutput(text=text, user_turn_id="pause-turn") for text in chunks)
    if answer:
        outputs.append(TextOutput(text=answer, user_turn_id="pause-turn"))
    spoken = _pump(monkeypatch, kpi, [(outputs, "pause-turn", True)], sender=sender,
                   strip_markers=VoiceService.strip_rt_markers)
    assert [text for text, _ in spoken] == ([answer] if answer else [])
    if answer:
        assert sender.sent.wait(2)
        assert len(sender.calls) == 1
        assert sender.calls[0][0].endswith("[REPLY] " + answer)
    else:
        assert sender.calls == []


def test_pause_marker_split_across_receive_timeout_stays_silent(monkeypatch, kpi):
    from hal.drivers.voice.voice_service import VoiceService

    sender = Sender()
    spoken = _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u", transcript="với"),
        TextOutput(text="{pau", user_turn_id="u"),
    ], "", False), ([TextOutput(text="se}", user_turn_id="u")], "u", True)],
        sender=sender, strip_markers=VoiceService.strip_rt_markers)
    assert spoken == []
    assert sender.calls == []
