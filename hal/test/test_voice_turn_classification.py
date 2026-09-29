"""Wake classification travels separately from the routing event."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from hal import config
from hal.drivers.voice._internal.sensing_sender import SensingSender, SendResult
from hal.drivers.voice._internal.turn_dispatch import dispatch_turn
from hal.drivers.voice._internal.realtime_turn import RealtimeTurnResult, ROUTE_HANDLED, ROUTE_DELEGATED
from hal.drivers.voice._internal.wakeword_focus import WakeWordFocus
from hal.realtime.models.output import UserSpeechOutput, TextOutput
from hal.test.test_live_history import Sender
from hal.test.test_live_voice_metrics import _pump
from hal.test.test_voice_metrics import kpi  # noqa: F401


@pytest.mark.parametrize("wake,active,expected", [(True, True, "voice_followup"), (True, False, "voice"), (False, True, "voice")])
def test_live_history_preserves_handled_route_with_wake_label(monkeypatch, kpi, wake, active, expected):
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", wake)
    focus = WakeWordFocus(60)
    if active:
        focus.refresh()
    sender = Sender()
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u", transcript="How are you?"),
        TextOutput(user_turn_id="u", text="Fine."),
    ], "u", True)], focus=focus, sender=sender)
    assert sender.sent.wait(2)
    _, kwargs = sender.calls[0]
    assert kwargs["event_type"] == "voice_agent_handled"
    assert kwargs["voice_turn_type"] == expected


@pytest.mark.parametrize("handled", [True, False])
def test_dispatch_debug_label_does_not_change_routing(monkeypatch, handled):
    decorator = SimpleNamespace(classify_wake_word=lambda text: (text, "voice"),
        identify_and_decorate=lambda text, audio: (text, "leo", "Leo"),
        submit_speech_emotion_from_session=lambda *a, **kw: None)
    sender = SimpleNamespace(send=Mock(return_value=SendResult(run_id="r", delivered=True)))
    rt = RealtimeTurnResult(handled=handled, delegated=not handled,
        transcript="Reply", delegate_msg="Check memory", route=ROUTE_HANDLED if handled else ROUTE_DELEGATED)
    dispatch_turn(decorator, sender, "Check memory", [], [], rt, voice_turn_type="voice_followup")
    kwargs = sender.send.call_args.kwargs
    assert kwargs["voice_turn_type"] == "voice_followup"
    assert kwargs["event_type"] == ("voice_agent_handled" if handled else "voice")


def test_sender_sends_metadata_without_rewriting_type(monkeypatch):
    from hal.drivers.voice._internal import sensing_sender as module
    response = SimpleNamespace(status_code=200, json=lambda: {"status": 1, "data": {"runId": "r"}})
    post = Mock(return_value=response)
    monkeypatch.setattr(module.requests, "post", post)
    result = SensingSender().send("hello", event_type="voice_agent_handled", skip_echo=True,
                                  voice_turn_type="voice_followup")
    assert result
    payload = post.call_args.kwargs["json"]
    assert payload["type"] == "voice_agent_handled"
    assert payload["voice_turn_type"] == "voice_followup"


def test_first_live_input_does_not_classify_its_own_focus_as_followup(monkeypatch, kpi):
    import threading

    monkeypatch.setattr(config, 'WAKEWORD_ENABLED', True)
    focus = WakeWordFocus(60)
    complete = threading.Event()

    class TwoTurnSender(Sender):
        def send(self, *args, **kwargs):
            result = super().send(*args, **kwargs)
            if len(self.calls) == 2:
                complete.set()
            return result

    sender = TwoTurnSender()
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id='first', transcript='How '),
        UserSpeechOutput(turn_id='first', transcript='are you?'),
        TextOutput(user_turn_id='first', text='Fine.'),
    ], 'first', True), ([
        UserSpeechOutput(turn_id='next', transcript='And tomorrow?'),
        TextOutput(user_turn_id='next', text='Also fine.'),
    ], 'next', True)], focus=focus, sender=sender)
    assert complete.wait(2)
    assert [kwargs['voice_turn_type'] for _, kwargs in sender.calls] == ['voice', 'voice_followup']
    assert all(kwargs['event_type'] == 'voice_agent_handled' for _, kwargs in sender.calls)
