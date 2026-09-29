"""Emotion tool results must acknowledge pending calls without recycling Gemini.

The live-session fix acknowledges even after speech: withholding a tool result
leaves Gemini pending and forces session recreation on the following input.
"""

from unittest import mock

from hal.realtime.models.output import FunctionCallOutput
from hal.realtime.orchestrator import RealtimeOrchestrator


def _orchestrator_with_agent():
    """Bare instance — __init__ wires up a whole session we do not need."""
    orch = object.__new__(RealtimeOrchestrator)
    orch._agent = mock.Mock()
    return orch


def _call():
    return FunctionCallOutput(
        name="express_emotion",
        arguments='{"emotion": "curious", "intensity": 0.8}',
        call_id="call-1",
    )


def _sent_inputs(orch):
    assert orch._agent.send.call_count == 1
    (payload,), _ = orch._agent.send.call_args
    return payload


def test_ack_sent_when_the_model_has_not_spoken_yet():
    """The tool call is the model's whole generation — without the ack it waits
    forever, the watchdog fires and the turn falls back to the main agent
    (device-observed 2026-08-19)."""
    orch = _orchestrator_with_agent()
    with mock.patch.object(RealtimeOrchestrator, "_fire_emotion"):
        orch._handle_emotion_call(_call(), spoken=False)
    assert _sent_inputs(orch)[0].trigger_response is True


def test_ack_still_resolves_tool_once_the_model_has_spoken():
    """A spoken reply must not leave the emotion call pending in Gemini."""
    orch = _orchestrator_with_agent()
    with mock.patch.object(RealtimeOrchestrator, "_fire_emotion"):
        orch._handle_emotion_call(_call(), spoken=True)
    result = _sent_inputs(orch)[0]
    assert result.trigger_response is True
    assert result.call_id == "call-1"
    assert "do not react to this ack" in result.output


class _SyncThread:
    """Runs the target inline. The real one is a daemon thread, so asserting on
    it directly would race the assertion."""

    def __init__(self, target=None, args=(), daemon=None, **kwargs):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def test_emotion_still_fires_in_both_cases():
    """The face must move regardless — the ack only governs the conversation."""
    for spoken in (True, False):
        orch = _orchestrator_with_agent()
        with (
            mock.patch.object(RealtimeOrchestrator, "_fire_emotion") as fire,
            mock.patch("hal.realtime.orchestrator.threading.Thread", _SyncThread),
        ):
            orch._handle_emotion_call(_call(), spoken=spoken)
        assert fire.called, f"emotion did not fire (spoken={spoken})"
        assert fire.call_args[0] == ("curious", 0.8)


def test_spoken_is_keyword_only_and_required():
    """A positional/defaulted flag would let a new call site silently pick the
    deadlocking branch."""
    orch = _orchestrator_with_agent()
    with mock.patch.object(RealtimeOrchestrator, "_fire_emotion"):
        try:
            orch._handle_emotion_call(_call())
        except TypeError:
            return
    raise AssertionError("spoken must be required")


# --- Capture settle scaling --------------------------------------------------

def test_capture_settle_scales_with_the_last_move():
    """A timed-out aim exits right after a big swing and the arm is still
    ringing; a flat 0.3s photographs that ring as blur."""
    from hal.realtime.orchestrator import _capture_settle_s

    still = _capture_settle_s(mock.Mock(last_move_deg=0.0))
    swung = _capture_settle_s(mock.Mock(last_move_deg=30.0))
    assert swung > still


def test_capture_settle_is_capped_so_latency_cannot_run_away():
    """This delay is paid before the user hears an answer — a sharper frame is
    not worth unbounded waiting."""
    from hal.realtime.orchestrator import CAPTURE_SETTLE_MAX_S, _capture_settle_s

    assert _capture_settle_s(mock.Mock(last_move_deg=500.0)) == CAPTURE_SETTLE_MAX_S
    assert CAPTURE_SETTLE_MAX_S <= 0.5


def test_capture_settle_survives_a_missing_aim_result():
    """Aiming can be disabled or raise; the capture must still happen."""
    from hal.realtime.orchestrator import CAPTURE_SETTLE_BASE_S, _capture_settle_s

    assert _capture_settle_s(None) == CAPTURE_SETTLE_BASE_S


def test_spoken_emotion_ack_clears_gemini_pending_call_without_recycle():
    import asyncio
    from types import SimpleNamespace
    from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent

    orch = _orchestrator_with_agent()
    with mock.patch.object(RealtimeOrchestrator, '_fire_emotion'):
        orch._handle_emotion_call(_call(), spoken=True)
    result = _sent_inputs(orch)[0]
    agent = object.__new__(GeminiLiveAgent)
    agent._session = SimpleNamespace(send_tool_response=mock.AsyncMock())
    agent._pending_tool_calls = {'call-1'}
    agent._pending_tool_names = {'call-1': 'express_emotion'}
    agent._pending_image = None
    agent._gated_audio_frames = 0
    agent._requires_fresh_session = False
    asyncio.run(agent._async_send_input(result))
    agent._session.send_tool_response.assert_awaited_once()
    response = agent._session.send_tool_response.call_args.kwargs['function_responses'][0]
    assert response.id == 'call-1'
    assert response.name == 'express_emotion'
    assert not agent._pending_tool_calls
    assert not agent._requires_fresh_session
