"""With the wake word off, the strict gate keeps unaddressed speech away from every model."""

import pytest

from hal import config as hal_config
from hal.drivers.voice._internal import turn_admission
from hal.drivers.voice._internal.realtime_turn import ROUTE_NOT_ADDRESSED, RealtimeTurnResult, should_drop_downstream_turn
from hal.test.test_turn_endpoint_capture import capture

FRAMES = [(1, True, "Please check my memory"), (4, False, None)]


@pytest.fixture(autouse=True)
def _no_evidence(monkeypatch):
    monkeypatch.setattr(turn_admission, "register_tts", lambda tts: None)
    turn_admission.register_tts(None)
    monkeypatch.setattr(turn_admission, "facing_evidence", lambda: None)
    import hal.drivers.voice.voice_service as module

    monkeypatch.setattr(module, "facing_evidence", lambda: None)
    monkeypatch.setattr(module, "short_answer_expected", lambda: False)
    yield


def test_hint_mode_still_sends_everything_to_the_model(monkeypatch):
    monkeypatch.setattr(hal_config, "ADDRESSED_GATE", "hint")
    with capture(monkeypatch, FRAMES, realtime=True) as result:
        result.realtime.assert_called_once()
        result.dispatch.assert_called_once()
        context = result.service._realtime.send_text.call_args.args[0]
        assert context == "test context"


def test_strict_mode_drops_speech_with_no_evidence(monkeypatch):
    monkeypatch.setattr(hal_config, "ADDRESSED_GATE", "strict")
    with capture(monkeypatch, FRAMES, realtime=True) as result:
        result.realtime.assert_not_called()
        # Audio already streamed during capture must not become the next turn.
        result.service._realtime.discard_open_activity.assert_called_once_with("not-addressed")
        result.dispatch.assert_called_once()
        rt = result.dispatch.call_args.args[5]
        assert rt.route == ROUTE_NOT_ADDRESSED


def test_strict_mode_admits_a_user_facing_the_lamp(monkeypatch):
    import hal.drivers.voice.voice_service as module

    monkeypatch.setattr(hal_config, "ADDRESSED_GATE", "strict")
    monkeypatch.setattr(module, "facing_evidence", lambda: True)
    with capture(monkeypatch, FRAMES, realtime=True) as result:
        result.realtime.assert_called_once()
        result.service._realtime.discard_open_activity.assert_not_called()


def test_strict_mode_admits_an_answer_to_the_devices_question(monkeypatch):
    import hal.drivers.voice.voice_service as module

    monkeypatch.setattr(hal_config, "ADDRESSED_GATE", "strict")
    monkeypatch.setattr(module, "short_answer_expected", lambda: True)
    with capture(monkeypatch, FRAMES, realtime=True) as result:
        result.realtime.assert_called_once()


def test_strict_mode_defers_to_the_wake_gate_when_the_wake_word_is_on(monkeypatch):
    monkeypatch.setattr(hal_config, "ADDRESSED_GATE", "strict")
    with capture(monkeypatch, FRAMES, realtime=True, wake_enabled=True, focus=lambda: True) as result:
        result.realtime.assert_called_once()


def test_not_addressed_is_a_terminal_route():
    assert should_drop_downstream_turn(RealtimeTurnResult(route=ROUTE_NOT_ADDRESSED))
