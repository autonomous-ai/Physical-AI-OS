"""Local intent completion ends retained realtime thinking without needing TTS."""
from unittest.mock import Mock
import pytest
from hal import app_state as state
from hal.presets import EMO_THINKING
from hal.drivers.voice._internal import turn_dispatch, realtime_turn
from hal.drivers.voice._internal.sensing_sender import SendResult
from hal.routes import led

@pytest.mark.parametrize("emotion,active,clear_count,restore_count", [
    (EMO_THINKING, True, 1, 1),
    ("happy", True, 1, 0),
    ("sleepy", False, 0, 0),
    (EMO_THINKING, False, 0, 0),
])
def test_local_completion_only_releases_owned_cue(monkeypatch, emotion, active, clear_count, restore_count):
    monkeypatch.setattr(state, "_current_emotion", emotion)
    monkeypatch.setattr(state, "_thinking_cue_active", active)
    monkeypatch.setattr(state, "_tts_speaking", False)
    monkeypatch.setattr(state, "_music_playing", False)
    clear, restore = Mock(), Mock()
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", clear)
    monkeypatch.setattr(led, "restore_led", restore)
    # No playback/TTS callback is required: muted and silent replies finish here.
    turn_dispatch._note_dispatch_outcome("local-turn", SendResult(delivered=True, handled_locally=True))
    assert clear.call_count == clear_count
    assert restore.call_count == restore_count


def test_main_agent_dispatch_keeps_thinking(monkeypatch):
    finish = Mock()
    monkeypatch.setattr(turn_dispatch, "_finish_local_intent_cue", finish)
    turn_dispatch._note_dispatch_outcome("agent-turn", SendResult(delivered=True, run_id="run"))
    finish.assert_not_called()

@pytest.mark.parametrize("color", [[0, 0, 0], [24, 19, 15]])
def test_cleanup_restores_command_color_not_thinking(monkeypatch, color):
    from hal.drivers.harness import led as harness_led
    from hal.presets import LST_SOLID, RGB_CMD_SOLID
    monkeypatch.setattr(state, "_current_emotion", EMO_THINKING)
    monkeypatch.setattr(state, "_thinking_cue_active", True)
    monkeypatch.setattr(state, "_sleeping", False)
    monkeypatch.setattr(state, "_tts_speaking", False)
    monkeypatch.setattr(state, "_music_playing", False)
    monkeypatch.setattr(state, "_user_led_state", {"type": LST_SOLID, "color": color})
    rgb = Mock()
    monkeypatch.setattr(state, "rgb_service", rgb)
    monkeypatch.setattr(state, "_stop_current_effect", Mock())
    monkeypatch.setattr(state, "_mic_muted_led_owns_strip", lambda: False)
    monkeypatch.setattr(harness_led, "enabled", lambda: False)
    def clear():
        state._thinking_cue_active = False
        state._current_emotion = "idle"
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", clear)
    turn_dispatch._finish_local_intent_cue()
    rgb.dispatch.assert_called_once_with(RGB_CMD_SOLID, tuple(color))
    assert not state._thinking_cue_active

@pytest.mark.parametrize("flag", ["_tts_speaking", "_music_playing"])
def test_cleanup_leaves_active_playback_overlay(monkeypatch, flag):
    monkeypatch.setattr(state, "_current_emotion", EMO_THINKING)
    monkeypatch.setattr(state, "_thinking_cue_active", True)
    monkeypatch.setattr(state, "_tts_speaking", False)
    monkeypatch.setattr(state, "_music_playing", False)
    monkeypatch.setattr(state, flag, True)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", Mock())
    restore = Mock()
    monkeypatch.setattr(led, "restore_led", restore)
    turn_dispatch._finish_local_intent_cue()
    restore.assert_not_called()
