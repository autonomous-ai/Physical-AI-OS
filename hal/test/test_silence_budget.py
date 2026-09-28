"""End-of-turn close rule: short clock only, and only from the final's arrival."""

import pytest

from hal.drivers.voice._internal import config as voice_cfg
from hal.drivers.voice._internal.vad_filters import turn_should_close

NOW = 1000.0


@pytest.fixture(autouse=True)
def silence_budgets(monkeypatch):
    monkeypatch.setattr(voice_cfg, "ENDPOINT_SILENCE_S", 0.8)
    monkeypatch.setattr(voice_cfg, "SILENCE_TIMEOUT_S", 2.5)


def test_no_final_keeps_the_long_fallback_clock():
    assert not turn_should_close(NOW, NOW - 1.5, 0.0)
    assert turn_should_close(NOW, NOW - (voice_cfg.SILENCE_TIMEOUT_S + 0.1), 0.0)


def test_final_shortens_the_wait():
    quiet = NOW - (voice_cfg.ENDPOINT_SILENCE_S + 0.1)
    assert turn_should_close(NOW, quiet, quiet)


def test_a_final_that_just_arrived_never_closes_on_the_same_frame():
    """Below the fallback clock, a fresh final alone must not end the turn."""
    spoke = NOW - (voice_cfg.SILENCE_TIMEOUT_S - 0.1)
    assert not turn_should_close(NOW, spoke, NOW)


def test_a_mid_utterance_final_does_not_close_the_turn_retroactively():
    """Flux emits EndOfTurn on a breath pause ("Hello." before the question)."""
    spoke = NOW - 0.9  # already quiet for 0.9s when the final lands
    assert not turn_should_close(NOW, spoke, NOW)
    assert turn_should_close(
        NOW + voice_cfg.ENDPOINT_SILENCE_S + 0.01, spoke, NOW
    )


def test_zero_disables_the_short_clock(monkeypatch):
    monkeypatch.setattr(voice_cfg, "ENDPOINT_SILENCE_S", 0.0)
    assert not turn_should_close(NOW, NOW - 1.0, NOW - 1.0)


def test_resumed_speech_supersedes_the_previous_final():
    """A final for 'Hello.' must not shorten pauses in the following question."""
    final = NOW
    resumed_speech = NOW + 2.0
    assert not turn_should_close(resumed_speech + 0.9, resumed_speech, final)


def test_resumed_speech_without_a_new_final_uses_the_fallback():
    final = NOW
    resumed_speech = NOW + 2.0
    assert turn_should_close(resumed_speech + 2.6, resumed_speech, final)


def test_new_final_rearms_the_short_clock_after_resumed_speech():
    resumed_speech = NOW + 2.0
    new_final = NOW + 2.2
    assert not turn_should_close(new_final + 0.7, resumed_speech, new_final)
    assert turn_should_close(new_final + 0.9, resumed_speech, new_final)
