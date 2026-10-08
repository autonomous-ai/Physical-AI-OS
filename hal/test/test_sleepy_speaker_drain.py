"""Sleep closes audio immediately; only scene confirmations retain a drain."""
import threading
from unittest.mock import Mock

import pytest

from hal import app_state as state
from hal.models import EmotionRequest, SpeakRequest
from hal.routes import emotion, voice


@pytest.fixture
def sleepy(monkeypatch):
    for name, value in {
        "_sleeping": True, "_current_emotion": "sleepy",
        "_speaker_muted": False, "_mic_muted": False,
        "_sleepy_auto_muted_speaker": False, "_sleepy_auto_muted_mic": False,
        "_scene_drain_cancel": None, "_active_scene": None,
        "tts_service": Mock(speaking=False), "music_service": None,
        "voice_service": None, "rgb_service": None,
        "animation_service": None, "_sleepy_release_timer": None,
        "_still_idle_timer": None, "_thinking_reset_timer": None,
    }.items():
        monkeypatch.setattr(state, name, value)
    for name in ("_persist_sleep_state", "_persist_speaker_state",
                 "_cancel_pending_restore", "_log_sleep_transition",
                 "clear_listening_pending_cue", "_apply_emotion_led_display"):
        monkeypatch.setattr(state, name, Mock())
    monkeypatch.setattr(state.privacy, "speaker_muted", False)
    return state.tts_service


@pytest.mark.parametrize("speaking", [False, True])
def test_sleep_mutes_and_cancels_tts_without_waiting(sleepy, speaking):
    """stop is required even when synthesis/queued speech has not begun playing."""
    sleepy.speaking = speaking
    sleepy.stop.side_effect = lambda: (
        pytest.fail("stop ran before mute") if not state._speaker_muted else None
    )
    state._finalize_sleepy_peripherals(mute_mic=True, mute_speaker=True)
    assert state._speaker_muted and state._sleepy_auto_muted_speaker
    assert state._mic_muted and state._sleepy_auto_muted_mic
    sleepy.stop.assert_called_once()
    assert state._scene_drain_cancel is None


def test_late_tts_is_suppressed(sleepy):
    state._finalize_sleepy_peripherals(mute_mic=False, mute_speaker=True)
    result = voice.speak_queue_text(SpeakRequest(text="Late old reply"))
    assert result["status"] == "suppressed"
    sleepy.speak_queue.assert_not_called()


@pytest.mark.parametrize("manual_mute", [False, True])
def test_wake_restores_only_sleep_owned_mute(sleepy, manual_mute):
    state._speaker_muted = manual_mute
    state._finalize_sleepy_peripherals(mute_mic=False, mute_speaker=True)
    sleepy.stop.assert_called_once()
    assert state._sleepy_auto_muted_speaker is not manual_mute
    state._sleeping = False
    state._current_emotion = "stretching"
    state._wake_sleepy_peripherals()
    assert state._speaker_muted is manual_mute
    assert not state._sleepy_auto_muted_speaker


def test_repeat_sleep_preserves_mute_ownership(sleepy):
    for _ in range(2):
        state._finalize_sleepy_peripherals(mute_mic=False, mute_speaker=True)
    assert state._speaker_muted and state._sleepy_auto_muted_speaker
    assert sleepy.stop.call_count == 2


def test_sleep_cancels_scene_drain_and_stops_music(sleepy, monkeypatch):
    cancel = threading.Event()
    state._scene_drain_cancel = cancel
    music = Mock(playing=True)
    monkeypatch.setattr(state, "music_service", music)
    state._finalize_sleepy_peripherals(mute_mic=False, mute_speaker=True)
    assert cancel.is_set()
    assert state._scene_drain_cancel is None
    music.stop.assert_called_once()
    state._start_scene_speaker_drain("night")
    assert state._scene_drain_cancel is None


def test_sleep_mutes_before_camera_cleanup(sleepy, monkeypatch):
    state._sleeping = False
    state._current_emotion = "idle"
    monkeypatch.setattr(emotion, "harness_blocks_sleep", lambda: False)
    monkeypatch.setattr(emotion.threading, "Timer", Mock())

    def camera_off(_reason):
        assert state._mic_muted and state._speaker_muted
        sleepy.stop.assert_called_once()

    monkeypatch.setattr(state, "_auto_camera_off", camera_off)
    result = emotion.express_emotion(EmotionRequest(emotion="sleepy"))
    assert result["status"] == "ok"


def test_rejected_sleep_does_not_mute(sleepy, monkeypatch):
    state._sleeping = False
    state._current_emotion = "idle"
    monkeypatch.setattr(emotion, "harness_blocks_sleep", lambda: True)
    result = emotion.express_emotion(EmotionRequest(emotion="sleepy"))
    assert result["status"] == "ignored"
    assert not state._speaker_muted and not state._mic_muted
    sleepy.stop.assert_not_called()


def test_stale_sleep_mute_does_not_stop_awake_audio(sleepy):
    state._sleeping = False
    state._current_emotion = "stretching"
    state._mute_speaker_for_sleep()
    assert not state._speaker_muted
    sleepy.stop.assert_not_called()
