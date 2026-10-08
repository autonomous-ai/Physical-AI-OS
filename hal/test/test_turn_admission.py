"""Admission evidence outside the transcript: pending questions, echo window, partials."""

import time
from types import SimpleNamespace

import pytest

from hal.drivers.voice._internal import main_followup, turn_admission
from hal.drivers.voice._internal.realtime_turn import is_noise_turn
from hal.drivers.voice._internal.session_finalize import recent_spoken_text
from hal.telemetry import voice_metrics


@pytest.fixture(autouse=True)
def _clean():
    turn_admission.register_tts(None)
    main_followup.reset_main_followup()
    yield
    turn_admission.register_tts(None)
    main_followup.reset_main_followup()


def _tts(text, spoken_at):
    return SimpleNamespace(last_spoken_text=text, last_spoken_time=spoken_at)


def test_bare_yes_is_the_answer_when_the_device_just_asked():
    turn_admission.register_tts(_tts("I can set a timer. Want me to?", spoken_at=time.time() - 4.0))
    assert turn_admission.device_question_pending()
    assert not is_noise_turn("yeah", 0.4, True)


def test_bare_yes_is_noise_when_nothing_was_asked():
    turn_admission.register_tts(_tts("It is three in the afternoon.", spoken_at=time.time() - 4.0))
    assert not turn_admission.device_question_pending()
    assert is_noise_turn("yeah", 0.4, True)


def test_the_question_window_closes():
    turn_admission.register_tts(_tts("Want me to?", spoken_at=1000.0))
    assert not turn_admission.device_question_pending(
        now=1000.0 + turn_admission.DEVICE_QUESTION_WINDOW_S + 1.0)


def test_main_agent_question_also_admits_a_short_answer(monkeypatch):
    monkeypatch.setattr(main_followup.hal_config, "REALTIME_MAIN_FOLLOWUP_S", 60.0)
    main_followup.note_main_reply("What should I call you?", heard=True)
    assert turn_admission.short_answer_expected()
    assert not is_noise_turn("okay", 0.4, True)


def test_a_short_answer_still_needs_real_speech_behind_it():
    turn_admission.register_tts(_tts("Want me to?", spoken_at=1000.0))
    assert is_noise_turn("yeah", 0.1, False, short_answer_ok=True)


def test_echo_prefix_only_applies_right_after_playback():
    tts = _tts("Turn on the desk light please", spoken_at=1000.0)
    assert recent_spoken_text(tts, now=1002.0) == "Turn on the desk light please"
    assert recent_spoken_text(tts, now=1000.0 + 30.0) == ""
    assert recent_spoken_text(None) == ""
    assert recent_spoken_text(_tts("", 1000.0), now=1001.0) == ""


@pytest.mark.parametrize("text, confident", [
    ("what is the weather like today", True),
    ("turn on the desk light", True),
    ("hey there", False),
    ("", False),
    ("... --- ...", False),
])
def test_confident_partial_needs_enough_words(text, confident):
    assert turn_admission.confident_partial(text) is confident


def test_speech_end_at_is_the_endpoint_instant():
    voice_metrics.reset_for_test()
    try:
        iid = voice_metrics.speech_end("silence_clock", at=123.5)
        assert voice_metrics.speech_end_at(iid) == 123.5
        assert voice_metrics.speech_end_at("vi-missing") == 0.0
    finally:
        voice_metrics.reset_for_test()


def test_device_names_strip_the_wake_prefixes():
    assert turn_admission.device_names(["hey lamp", "wake up lamp", "hello autonomous", "hey dee"]) == {
        "lamp", "autonomous", "dee",
    }


@pytest.mark.parametrize("text, heard", [
    ("What time is it, Lamp?", True),
    ("lamp can you hear me", True),
    ("is big lamp awake", True),
    ("turn on the desk light", False),
    ("", False),
])
def test_name_anywhere_counts_as_addressed(text, heard):
    phrases = ["hey lamp", "hey big lamp", "hey autonomous"]
    assert turn_admission.name_mentioned(text, phrases) is heard
