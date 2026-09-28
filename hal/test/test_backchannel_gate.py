"""The addressee rule: who may the lamp answer, and what may it say back."""

import threading

import pytest


def _addressed_to_us(wakeword_enabled, wake_heard, followup_open):
    """Mirror of the predicate in voice_service._stream_session."""
    detected = threading.Event()
    if wake_heard:
        detected.set()
    if not wakeword_enabled:
        return True
    return detected.is_set() or followup_open


@pytest.mark.parametrize("wake_heard,followup", [(True, False), (False, True), (True, True)])
def test_an_authorised_turn_may_be_acknowledged(wake_heard, followup):
    """Wake phrase, or the window a phrase / click / gaze opened."""
    assert _addressed_to_us(True, wake_heard, followup) is True


def test_an_unauthorised_turn_is_not_acknowledged():
    """The colleague-conversation case: overheard, not addressed."""
    assert _addressed_to_us(True, False, False) is False


def test_without_a_wake_word_every_utterance_is_addressed():
    """Nothing to gate on — this is the pre-wake-word behaviour, unchanged."""
    assert _addressed_to_us(False, False, False) is True


def test_the_same_rule_governs_the_cue_and_the_backchannel():
    """Both are claims to be the addressee, so both ask the same question."""
    import inspect

    from hal.drivers.voice.voice_service import VoiceService

    src = inspect.getsource(VoiceService._stream_session)
    assert src.count("def addressed_to_us") == 1, "one definition, not a copy each"
    # The definition line contains the name too, so callers are the rest.
    assert src.count("addressed_to_us()") - 1 == 2, "the cue and the backchannel"
