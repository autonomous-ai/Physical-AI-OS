"""Recover an uncommitted upload once, without replaying executed responses."""

from unittest.mock import Mock, call

import pytest

from hal.drivers.voice._internal import realtime_turn as module
from hal.realtime.models import TextOutput
from hal.realtime.models.signal import LookReplaySignal
from hal.realtime.voice_agent.base import AudioTurnSessionChanged


def test_recovered_binding_survives_camera_replay(monkeypatch):
    old, fresh = object(), object()
    frames = [object(), object()]
    rt = Mock(available=True)
    rt.flush_output.side_effect = [AudioTurnSessionChanged(), None, None]
    rt.bind_audio_turn.return_value = fresh
    rt.recover_session.return_value = True
    rt.stream_output.side_effect = [iter([LookReplaySignal()]), iter([TextOutput(text="I see it.")])]
    monkeypatch.setattr(module.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(module.hal_config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(module, "harness_followup_active", lambda: False)
    monkeypatch.setattr(module, "_thinking_cue_start", lambda: None)
    monkeypatch.setattr(module, "_thinking_cue_clear", lambda: None)
    monkeypatch.setattr(module, "_WaitFiller", Mock())
    monkeypatch.setattr(module, "_reply_language_name", lambda: "English")

    result = module.run_realtime_turn(
        rt, Mock(), lambda text: text, "Please look at this", frames, 2.0, audio_turn=old,
    )

    assert result.handled
    rt.recover_session.assert_called_once_with("audio-upload-session-changed")
    assert rt.append_audio.call_args_list == [call(frame, turn=fresh) for frame in frames] * 2
    assert rt.commit_audio.call_args_list == [call(turn=fresh), call(turn=fresh)]
    assert rt.stream_output.call_args_list == [call(turn=fresh), call(turn=fresh)]


def test_session_loss_after_commit_does_not_replay_tools():
    rt, token = Mock(), object()

    def output():
        yield TextOutput(text="Working.")
        raise AudioTurnSessionChanged()

    rt.stream_output.return_value = output()
    binding, outputs = module._commit_turn_output(rt, [object()], token)
    assert binding is token
    assert next(outputs).text == "Working."
    with pytest.raises(AudioTurnSessionChanged):
        next(outputs)
    rt.commit_audio.assert_called_once_with(turn=token)
    rt.recover_session.assert_not_called()
    rt.append_audio.assert_not_called()
