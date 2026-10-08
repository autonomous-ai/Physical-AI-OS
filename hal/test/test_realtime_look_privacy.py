"""A realtime `look` never wakes a camera the user switched off."""
import json
from unittest.mock import Mock

import pytest

import hal.app_state as state
from hal import privacy
from hal.realtime import orchestrator
from hal.realtime.models import FunctionCallOutput, FunctionCallResultInput


def _rt():
    rt = object.__new__(orchestrator.RealtimeOrchestrator)
    rt._agent = Mock()
    rt._capture_frame = Mock()
    return rt


def _look():
    return FunctionCallOutput(name="look", call_id="look-1", arguments="{}",
                              user_transcript="What am I holding?", user_turn_id="gemini-t1")


@pytest.mark.parametrize("muted, disabled, manual, off", [
    (True, False, False, True),    # hardware privacy switch
    (False, True, True, True),     # user turned the camera off in the app
    (False, True, False, False),   # OS idled the camera; a look may wake it
    (False, False, False, False),  # camera on
])
def test_camera_off_by_user(muted, disabled, manual, off, monkeypatch):
    monkeypatch.setattr(privacy, "camera_muted", muted)
    monkeypatch.setattr(state, "_camera_disabled", disabled)
    monkeypatch.setattr(state, "_camera_manual_override", manual)
    assert orchestrator._camera_off_by_user() is off


def test_look_refuses_without_aiming_or_capturing(monkeypatch):
    monkeypatch.setattr(privacy, "camera_muted", False)
    monkeypatch.setattr(state, "_camera_disabled", True)
    monkeypatch.setattr(state, "_camera_manual_override", True)
    rt = _rt()
    assert rt._handle_look_call(_look()) is False
    rt._capture_frame.assert_not_called()
    (ack,) = rt._agent.send.call_args.args[0]
    assert isinstance(ack, FunctionCallResultInput)
    assert ack.call_id == "look-1"
    assert "turned off by the user" in json.loads(ack.output)["error"]


def test_capture_frame_refuses_a_user_disabled_camera(monkeypatch):
    cap = Mock()
    monkeypatch.setattr(state, "camera_capture", cap, raising=False)
    monkeypatch.setattr(privacy, "camera_muted", False)
    monkeypatch.setattr(state, "_camera_disabled", True)
    monkeypatch.setattr(state, "_camera_manual_override", True)
    assert orchestrator.RealtimeOrchestrator._capture_frame() is None
    cap.start.assert_not_called()
