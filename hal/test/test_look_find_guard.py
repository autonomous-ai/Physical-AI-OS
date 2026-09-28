"""#481: a `look` call on a find request delegates without aiming or capturing."""
import json
from unittest.mock import Mock

import hal.app_state as state
from hal.realtime import orchestrator
from hal.realtime.models import FunctionCallOutput, FunctionCallResultInput
from hal.realtime.models.signal import DelegateSignal


def _rt():
    rt = object.__new__(orchestrator.RealtimeOrchestrator)
    rt._agent = Mock()
    rt._capture_frame = Mock()
    return rt


def _look(transcript):
    return FunctionCallOutput(name="look", call_id="look-1", arguments="{}",
                              user_transcript=transcript, user_turn_id="gemini-t1")


def test_find_request_delegates_without_capture(monkeypatch):
    monkeypatch.setattr(state, "realtime_look_frame_path", "/tmp/stale.jpg", raising=False)
    rt = _rt()
    signal = rt._redirect_find_look(_look("Find my mouse. Yeah. Do that one."))
    assert isinstance(signal, DelegateSignal)
    assert signal.message == "Find my mouse. Yeah. Do that one."
    assert signal.transcript == signal.message
    assert signal.user_turn_id == "gemini-t1"
    rt._capture_frame.assert_not_called()
    (ack,) = rt._agent.send.call_args.args[0]
    assert isinstance(ack, FunctionCallResultInput)
    assert ack.call_id == "look-1"
    assert json.loads(ack.output) == {"result": "delegated"}
    rt._agent.end_turn.assert_called_once()
    assert state.realtime_look_frame_path is None


def test_visual_question_is_not_redirected():
    rt = _rt()
    assert rt._redirect_find_look(_look("Look what I am holding!")) is None
    rt._agent.send.assert_not_called()
    rt._agent.end_turn.assert_not_called()


def test_missing_transcript_falls_through():
    rt = _rt()
    assert rt._redirect_find_look(_look("")) is None
    rt._agent.send.assert_not_called()
