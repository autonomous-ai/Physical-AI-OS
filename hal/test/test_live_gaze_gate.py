"""Live entry needs wake focus; closed focus must still allow STT wake detection."""

from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from hal import config
from hal.drivers.voice.voice_service import VoiceService
from hal.drivers.voice._internal.wakeword_focus import WakeWordFocus
from hal.drivers.voice._internal.realtime_turn import RealtimeTurnResult


@pytest.mark.parametrize("wake,gaze,shadow,focus,expected", [
    (True, True, False, False, "turn"),
    (True, True, False, True, "live"),
    (True, True, True, False, "turn"),
    (True, False, False, False, "turn"),
    (True, False, False, True, "live"),
    (True, True, True, True, "live"),
    (False, True, False, False, "live"),
])
def test_live_entry_respects_gaze_focus_and_configuration(monkeypatch, wake, gaze, shadow, focus, expected):
    monkeypatch.setattr(config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", wake)
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", gaze)
    monkeypatch.setattr(config, "GAZE_WAKE_SHADOW", shadow)
    service = object.__new__(VoiceService)
    service._music_is_playing = lambda: False
    service._wakeword_focus = SimpleNamespace(is_active=lambda: focus)
    service._realtime = Mock()
    service._realtime.wait_until_available.return_value = True

    assert service._live_decision([]) == expected
    if expected == "turn":
        service._realtime.prepare_turn.assert_not_called()
        service._realtime.wait_until_available.assert_not_called()
    else:
        service._realtime.prepare_turn.assert_called_once()


@pytest.mark.parametrize("gaze,shadow", [(True, False), (True, True), (False, False)])
def test_wake_focus_opens_live_and_expiry_returns_to_stt(monkeypatch, gaze, shadow):
    monkeypatch.setattr(config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", True)
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", gaze)
    monkeypatch.setattr(config, "GAZE_WAKE_SHADOW", shadow)
    service = object.__new__(VoiceService)
    service._music_is_playing = lambda: False
    now = [0.0]
    service._wakeword_focus = WakeWordFocus(20, clock=lambda: now[0])
    service._realtime = Mock()
    service._realtime.wait_until_available.return_value = True
    assert service._live_decision([]) == "turn"
    service._realtime.prepare_turn.assert_not_called()
    service._wakeword_focus.refresh()
    assert service._live_decision([]) == "live"
    now[0] = 20.0
    assert service._live_decision([]) == "turn"
    service._realtime.prepare_turn.assert_called_once()
    service._realtime.wait_until_available.assert_called_once_with(5.0)


@pytest.mark.parametrize("partial,final,authorized", [
    ("Hello Lamp", "Hello Lamp, tell me a story", True),
    ("Hello Lamp", "Hello Mom, tell me a story", False),
    ("What time is it", "What time is it", False),
])
def test_live_opener_uses_stt_confirmation_before_granting_focus(
    monkeypatch, partial, final, authorized,
):
    from hal.drivers.voice import voice_service

    monkeypatch.setattr(config, "WAKEWORD_ENABLED", True)
    monkeypatch.setattr(config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(voice_service.voice_cfg, "LIVE_MODE", True)
    service = Mock()
    service._running = False
    service._try_live_opener = MethodType(VoiceService._try_live_opener, service)
    service._tts = None
    service._wakeword_focus = WakeWordFocus(20)
    service._realtime.rebuilding = False
    service._realtime.available = True
    service._music_is_playing.return_value = False
    service._decorator.starts_with_wake_word.side_effect = lambda text: text.lower().startswith("hello lamp")
    service._decorator.matches_wake_word_loosely.return_value = False
    service._decorator.classify_wake_word.return_value = (final, "voice_command")
    service._decorator.identify_and_decorate.return_value = (final, None, None)
    stt = Mock()
    stt.is_closed.return_value = False

    def finish_stt():
        stt._on_transcript_cb(partial, False)
        assert not service._wakeword_focus.is_active()
        service._realtime.prepare_turn.assert_not_called()
        service._realtime.append_audio.assert_not_called()
        if partial == "Hello Lamp":
            service._set_emotion_local.assert_called()
        stt._on_transcript_cb(final, True)

    stt.close.side_effect = finish_stt
    monkeypatch.setattr(voice_service, "finalize_session", lambda *args: (final, [], 2.0))
    dispatch = Mock()
    realtime = Mock(return_value=RealtimeTurnResult())
    monkeypatch.setattr(voice_service, "dispatch_turn", dispatch)
    monkeypatch.setattr(voice_service, "run_realtime_turn", realtime)
    monkeypatch.setattr(voice_service, "voice_metrics", Mock())
    monkeypatch.setattr(voice_service.requests, "post", Mock())

    assert VoiceService._live_decision(service, []) == "turn"
    VoiceService._stream_session(
        service, Mock(), 320, 16000, preconnected_session=stt,
        harness_voice={"enabled": False, "generation": 0},
    )
    assert dispatch.called is authorized
    realtime.assert_not_called()
    service._realtime.prepare_turn.assert_not_called()
    service._realtime.append_audio.assert_not_called()
    assert service._wakeword_focus.is_active() is authorized
    assert VoiceService._live_decision(service, []) == ("live" if authorized else "turn")
