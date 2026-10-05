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


def test_rejected_answer_inside_window_still_reaches_main():
    # A one-word name is a typical reject_turn candidate; main asked, so main decides.
    from hal.realtime.models.signal import RejectSignal

    mf.note_main_reply(QUESTION, heard=True)
    rt = Mock(available=True, execution_completed=False)
    rt.stream_output.side_effect = [iter([RejectSignal()])]

    result = module.run_realtime_turn(rt, Mock(), lambda t: t, "Momo", [object()], 1.2)

    assert result.delegated and not result.rejected
    assert not module.should_drop_downstream_turn(result)
    assert not mf.take_main_followup()


def test_recovered_session_still_gets_the_delegate_note(monkeypatch):
    from hal.realtime.voice_agent.base import AudioTurnSessionChanged

    monkeypatch.setattr(module, "device_city", lambda: "")
    mf.note_main_reply(QUESTION, heard=True)
    old, fresh = object(), object()
    rt = Mock(available=True, execution_completed=False)
    rt.flush_output.side_effect = [AudioTurnSessionChanged(), None]
    rt.bind_audio_turn.return_value = fresh
    rt.recover_session.return_value = True
    rt.stream_output.side_effect = [iter([TextOutput(text=SPOKEN)])]

    module.run_realtime_turn(rt, Mock(), lambda t: t, "Momo", [object()], 1.2, audio_turn=old)

    sent = [c.args[0] for c in rt.send_text.call_args_list]
    assert any("Main agent is waiting for this answer" in s for s in sent)


def test_backstop_handoff_lets_main_ignore_an_unrelated_turn():
    from hal.drivers.voice._internal import turn_dispatch

    mf.note_main_reply("Anything else?", heard=True)
    _, result = _run(text="what time is it", spoken="It's three o'clock.")
    assert result.answered_for_main

    decorator = Mock()
    decorator.classify_wake_word.return_value = ("what time is it", "voice")
    decorator.identify_and_decorate.return_value = ("what time is it", "", "")
    sender = Mock()
    sender.send.return_value = None
    turn_dispatch.dispatch_turn(decorator, sender, "what time is it", [], [], result)

    message = sender.send.call_args.args[0]
    assert "[realtime-handoff] Realtime answered this aloud while you were waiting" in message
    assert "NO_REPLY" in message
    assert "do not choose NO_REPLY merely because realtime already spoke" not in message
    assert "[HANDLED]" not in message
