"""If realtime answers the user's reply to main itself, main still gets it live (#564)."""

from unittest.mock import Mock

import pytest

import hal.config as hal_config
from hal.drivers.voice._internal import main_followup as mf
from hal.drivers.voice._internal import realtime_turn as module
from hal.realtime.models import TextOutput

QUESTION = "What name should I save you under?"
SPOKEN = "Look right at my camera, Momo. All done! I've got you remembered."


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(module.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(module.hal_config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(hal_config, "REALTIME_MAIN_FOLLOWUP_S", 60)
    monkeypatch.setattr(module, "harness_followup_active", lambda: False)
    monkeypatch.setattr(module, "_thinking_cue_start", lambda: None)
    monkeypatch.setattr(module, "_thinking_cue_clear", lambda: None)
    monkeypatch.setattr(module, "_WaitFiller", Mock())
    monkeypatch.setattr(module, "_reply_language_name", lambda: "English")


def _run(text="No more.", spoken=SPOKEN, **kw):
    rt = Mock(available=True, execution_completed=False)
    rt.stream_output.side_effect = [iter([TextOutput(text=spoken)])]
    return rt, module.run_realtime_turn(rt, Mock(), lambda t: t, text, [object()], 1.2, **kw)


def test_handled_answer_inside_window_becomes_handoff():
    mf.note_main_reply(QUESTION, heard=True)

    rt, result = _run()

    rt.commit_audio.assert_called()          # realtime still heard the audio
    assert result.delegated and not result.handled
    assert result.route == module.ROUTE_DELEGATED
    assert "Momo" in result.transcript       # realtime's understanding survives
    assert "Momo" in result.handoff_context
    assert not mf.take_main_followup()       # consumed


def test_closed_window_keeps_handled():
    _, result = _run(text="what time is it", spoken="It's three o'clock.")

    assert result.handled and not result.delegated
    assert result.route == module.ROUTE_HANDLED


def test_noise_turn_does_not_consume_window():
    mf.note_main_reply(QUESTION, heard=True)

    # A short STT fabrication over non-speech audio is a noise turn (needs_noise_guard).
    _run(text="Uh.", audio_is_speech=False)

    assert mf.pending_main_question() == QUESTION
