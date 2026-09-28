"""Async look continues its interaction; legacy look still requests replay."""
from contextlib import nullcontext
from unittest.mock import Mock

import numpy as np
import pytest

from hal.realtime import orchestrator
from hal.realtime.models import FunctionCallOutput, FunctionCallResultInput, ImageInput


@pytest.mark.parametrize("async_status", [False, True])
def test_fresh_look_replays_only_legacy_sessions(monkeypatch, async_status):
    from hal.drivers.tracking import aim, look_debug
    monkeypatch.setattr(aim, "servo_ownership", nullcontext)
    monkeypatch.setattr(orchestrator.config, "LOOK_AIM_ENABLED", False)
    monkeypatch.setattr(orchestrator.config, "LOOK_MONITOR_ENABLED", False)
    for name in ("start", "note_capture"):
        monkeypatch.setattr(look_debug, name, Mock())
    monkeypatch.setattr(look_debug, "stage", lambda *args: nullcontext())
    rt = object.__new__(orchestrator.RealtimeOrchestrator)
    rt._agent = Mock(supports_look_continuation=async_status)
    rt._last_look_sent_monotonic = 0
    rt._looked_this_turn = False
    rt._capture_frame = Mock(return_value=np.zeros((4, 4, 3), dtype=np.uint8))
    rt._persist_look_frame = Mock(return_value="/tmp/look.jpg")
    replay = rt._handle_look_call(FunctionCallOutput(
        name="look", call_id="look-1", arguments="{}",
    ))
    assert replay is not async_status
    sent = [call.args[0][0] for call in rt._agent.send.call_args_list]
    ack = sent[0]
    assert isinstance(ack, FunctionCallResultInput)
    assert ack.call_id == "look-1"
    if async_status:
        assert len(sent) == 1
        assert ack.image is rt._capture_frame.return_value
    else:
        assert len(sent) == 2
        assert ack.image is None
        assert isinstance(sent[1], ImageInput)
    assert rt._looked_this_turn
