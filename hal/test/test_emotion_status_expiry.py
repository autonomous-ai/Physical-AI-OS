"""Transient status expires independently of TTS's LED restore timer."""
from unittest.mock import Mock

import pytest
import hal.app_state as state
from hal.routes.emotion import express_emotion, emotion_status
from hal.models import EmotionRequest


class Timer:
    def __init__(self, delay, callback):
        self.delay, self.callback = delay, callback
        self.cancelled = False
    def start(self):
        pass
    def cancel(self):
        self.cancelled = True
    def is_alive(self):
        return not self.cancelled


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(state.threading, 'Timer', Timer)
    for key, value in {
        '_sleeping': False, '_current_emotion': None, '_emotion_generation': 0,
        '_emotion_idle_timer': None, '_still_idle_timer': None,
        '_thinking_reset_timer': None, '_sleepy_release_timer': None,
        '_restore_timer': None, 'animation_service': None,
        '_user_led_state': None, '_camera_disabled': False,
    }.items():
        monkeypatch.setattr(state, key, value)
    monkeypatch.setattr(state, 'clear_listening_pending_cue', Mock())
    monkeypatch.setattr(state, '_apply_emotion_led_display', Mock(return_value=None))
    monkeypatch.setattr(state, '_get_recording_duration', lambda name: 4.0)


def test_laugh_expires_even_when_tts_cancels_led_restore(setup):
    express_emotion(EmotionRequest(emotion='laugh'))
    assert emotion_status()['current_emotion'] == 'laugh'
    timer = state._emotion_idle_timer
    assert timer.delay == 4.5
    state._restore_timer.cancel()
    state._restore_timer = None
    timer.callback()
    assert emotion_status()['current_emotion'] == 'idle'


@pytest.mark.parametrize('next_emotion', ['laugh', 'thinking', 'listening', 'idle'])
def test_old_callback_cannot_clear_new_expression(setup, next_emotion):
    express_emotion(EmotionRequest(emotion='laugh'))
    old = state._emotion_idle_timer
    express_emotion(EmotionRequest(emotion=next_emotion))
    assert old.cancelled
    old.callback()  # A cancelled timer may already have entered its callback.
    assert emotion_status()['current_emotion'] == next_emotion


def test_sleep_is_not_cleared_by_stale_timer(setup):
    generation = state._begin_emotion('laugh')
    state._schedule_emotion_idle(1, generation)
    old = state._emotion_idle_timer
    state._begin_emotion('sleepy')
    state._sleeping = True
    old.callback()
    assert state._current_emotion == 'sleepy'
