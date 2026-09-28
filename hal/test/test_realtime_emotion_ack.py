"""Whether `express_emotion` is acknowledged decides if the turn survives."""

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
    """An idle emotion tool call is acked so the turn completes."""
    orch = _orchestrator_with_agent()
    with mock.patch.object(RealtimeOrchestrator, "_fire_emotion"):
        orch._handle_emotion_call(_call(), spoken=False)
    assert _sent_inputs(orch)[0].trigger_response is True


def test_ack_withheld_once_the_model_has_spoken():
    """No ack mid-reply, to avoid re-speaking the turn."""
    orch = _orchestrator_with_agent()
    with mock.patch.object(RealtimeOrchestrator, "_fire_emotion"):
        orch._handle_emotion_call(_call(), spoken=True)
    assert _sent_inputs(orch)[0].trigger_response is False


class _SyncThread:
    """Run the thread target inline."""

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
    """`spoken` is keyword-only and required."""
    orch = _orchestrator_with_agent()
    with mock.patch.object(RealtimeOrchestrator, "_fire_emotion"):
        try:
            orch._handle_emotion_call(_call())
        except TypeError:
            return
    raise AssertionError("spoken must be required")


def test_capture_settle_scales_with_the_last_move():
    """Settle time scales with the aim's final swing."""
    from hal.realtime.orchestrator import _capture_settle_s

    still = _capture_settle_s(mock.Mock(last_move_deg=0.0))
    swung = _capture_settle_s(mock.Mock(last_move_deg=30.0))
    assert swung > still


def test_capture_settle_is_capped_so_latency_cannot_run_away():
    """The settle delay is capped."""
    from hal.realtime.orchestrator import CAPTURE_SETTLE_MAX_S, _capture_settle_s

    assert _capture_settle_s(mock.Mock(last_move_deg=500.0)) == CAPTURE_SETTLE_MAX_S
    assert CAPTURE_SETTLE_MAX_S <= 0.5


def test_capture_settle_survives_a_missing_aim_result():
    """Aiming can be disabled or raise; the capture must still happen."""
    from hal.realtime.orchestrator import CAPTURE_SETTLE_BASE_S, _capture_settle_s

    assert _capture_settle_s(None) == CAPTURE_SETTLE_BASE_S
