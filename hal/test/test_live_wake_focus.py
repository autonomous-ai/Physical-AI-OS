"""LIVE follow-up focus follows accepted turns, not raw VAD or model output."""
import pytest
from hal import config
from hal.drivers.voice._internal.wakeword_focus import WakeWordFocus
from hal.realtime.models.output import UserSpeechOutput, TextOutput, ExecutionOutput
from hal.realtime.models.signal import RejectSignal, DelegateSignal
from hal.test.test_live_voice_metrics import _pump
from hal.test.test_voice_metrics import kpi  # noqa: F401


def focus_window():
    now = [0.0]
    focus = WakeWordFocus(60, clock=lambda: now[0])
    focus.refresh()
    now[0] = 45
    return focus, now


def test_accepted_live_followup_extends_window_past_original_deadline(monkeypatch, kpi):
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", True)
    focus, now = focus_window()
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u", transcript="How are you today?"),
        TextOutput(text="Doing well.", user_turn_id="u"),
    ], "u", True)], focus=focus)
    now[0] = 65
    assert focus.is_active()
    now[0] = 106
    assert not focus.is_active()


@pytest.mark.parametrize("outputs,completed", [
    ([UserSpeechOutput(turn_id="u", transcript="hello"), RejectSignal(user_turn_id="u")], True),
    ([TextOutput(text="Unprompted.", user_turn_id="u")], True),
    ([UserSpeechOutput(turn_id="u", transcript="")], True),
    ([UserSpeechOutput(turn_id="u", transcript="hello")], False),
])
def test_reject_noise_output_and_timeout_do_not_extend_focus(monkeypatch, kpi, outputs, completed):
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", True)
    focus, now = focus_window()
    _pump(monkeypatch, kpi, [(outputs, "u", completed)], focus=focus)
    now[0] = 65
    assert not focus.is_active()


def test_duplicate_terminal_does_not_extend_focus_twice(monkeypatch, kpi):
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", True)
    focus, now = focus_window()
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u", transcript="hello"),
        ExecutionOutput(user_turn_id="u", execution_completed=True),
    ], "u", True), ([
        lambda: now.__setitem__(0, 55),
        ExecutionOutput(user_turn_id="u", execution_completed=True),
    ], "u", True)], focus=focus)
    now[0] = 106
    assert not focus.is_active()


def test_delegate_refreshes_focus_even_if_transcript_arrives_with_tool(monkeypatch, kpi):
    import hal.drivers.voice.voice_service as module
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", True)
    monkeypatch.setattr(module, "dispatch_turn", lambda *a, **kw: None)
    focus, now = focus_window()
    _pump(monkeypatch, kpi, [([
        DelegateSignal(user_turn_id="u", transcript="Check my memory", message="Check my memory"),
    ], "u", False)], focus=focus)
    now[0] = 65
    assert focus.is_active()


def test_harness_does_not_open_normal_followup_window(monkeypatch, kpi):
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", True)
    focus, now = focus_window()
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u", transcript="hello"),
    ], "u", True)], focus=focus, harness_voice={"enabled": True})
    now[0] = 65
    assert not focus.is_active()


def test_unaddressed_turn_cannot_reopen_expired_focus(monkeypatch, kpi):
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", True)
    focus, now = focus_window()
    now[0] = 65
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u", transcript="background conversation"),
    ], "u", True)], focus=focus, addressed=False)
    assert not focus.is_active()


def test_focus_at_live_entry_survives_expiry_during_accepted_turn(monkeypatch, kpi):
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", True)
    focus, now = focus_window()
    _pump(monkeypatch, kpi, [([
        lambda: now.__setitem__(0, 65),
        UserSpeechOutput(turn_id="u", transcript="a long question"),
    ], "u", True)], focus=focus, addressed=False)
    now[0] = 70
    assert focus.is_active()
