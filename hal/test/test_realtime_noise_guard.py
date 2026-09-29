"""Noise guard for turns whose STT invented a short word out of room noise."""

import pytest

from hal import config as hal_config
from hal.drivers.voice._internal.realtime_turn import is_noise_turn, needs_noise_guard
from hal.drivers.voice._internal.session_finalize import finalize_session


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_REQUIRE_TRANSCRIPT", True)
    monkeypatch.setattr(hal_config, "REALTIME_NOISE_GUARD_MAX_WORDS", 3)


def test_guard_runs_for_empty_and_short_transcripts():
    assert needs_noise_guard("")
    assert needs_noise_guard("Okay")
    assert needs_noise_guard("Thank you very")


def test_guard_skipped_for_a_real_sentence():
    assert not needs_noise_guard("bật đèn lên giúp anh")


def test_guard_disabled_leaves_transcript_turns_alone(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_NOISE_GUARD_MAX_WORDS", 0)
    assert not needs_noise_guard("louder")
    assert not is_noise_turn("louder", 2.0, audio_is_speech=False)


def test_short_transcript_over_noise_is_dropped():
    assert is_noise_turn("Okay", 2.0, audio_is_speech=False)


def test_short_transcript_over_real_speech_commits():
    assert not is_noise_turn("bật đèn", 2.0, audio_is_speech=True)


def test_long_transcript_commits_even_if_silero_disagrees():
    assert not is_noise_turn("bật đèn lên giúp anh", 2.0, audio_is_speech=False)


def test_missing_guard_result_never_drops_a_turn():
    assert not is_noise_turn("louder", 2.0)


def test_backchannel_only_turn_dropped_regardless_of_guard():
    assert is_noise_turn("Okay", 2.0, audio_is_speech=True)
    assert is_noise_turn("yeah", 2.0, audio_is_speech=True)


def test_empty_transcript_still_dropped_by_require_transcript():
    assert is_noise_turn("", 2.0, audio_is_speech=True)


@pytest.mark.parametrize("raw", [".", "…", "?!", "——"])
def test_punctuation_only_stt_final_is_normalized_to_empty(raw):
    combined, _ser_audio, _duration = finalize_session([], [""], [raw], -1)

    assert combined == ""


@pytest.mark.parametrize("raw", ["okay", "hôm nay", "你好", "123"])
def test_spoken_unicode_stt_final_is_preserved(raw):
    combined, _ser_audio, _duration = finalize_session([], [""], [raw], -1)

    assert combined == raw
