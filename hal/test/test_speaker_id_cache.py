"""The recognizer verdict is reused instead of being re-derived every turn."""

import time
from unittest import mock

from hal.drivers.voice._internal import speaker_decorate as sd
from hal.drivers.voice._internal.speaker_decorate import UNKNOWN_LABEL, SpeakerDecorator


def _decorator():
    return SpeakerDecorator(wake_words=["lamp"], nudge_cooldown_s=0, enable_people_perception=False)


def test_a_fresh_decorator_has_nothing_to_reuse():
    assert _decorator()._cached_identity(in_followup=False) is None


def test_a_recent_verdict_is_reused():
    d = _decorator()
    d._remember_identity("long", "Long")
    assert d._cached_identity(in_followup=False) == ("long", "Long")


def test_a_stale_verdict_is_not_reused(monkeypatch):
    monkeypatch.setattr(sd, "SPEAKER_ID_CACHE_S", 10.0)
    monkeypatch.setattr(sd, "SPEAKER_ID_CACHE_FOLLOWUP_S", 0.0)
    d = _decorator()
    d._identity_cache = ("long", "Long", time.time() - 30)
    assert d._cached_identity(in_followup=False) is None


# Turns inside a follow-up window are one conversation, so the identity holds
# for the whole window even once the ordinary TTL has passed.
def test_the_follow_up_window_holds_a_verdict_the_normal_ttl_would_drop(monkeypatch):
    monkeypatch.setattr(sd, "SPEAKER_ID_CACHE_S", 10.0)
    monkeypatch.setattr(sd, "SPEAKER_ID_CACHE_FOLLOWUP_S", 300.0)
    d = _decorator()
    d._identity_cache = ("long", "Long", time.time() - 30)
    assert d._cached_identity(in_followup=False) is None
    assert d._cached_identity(in_followup=True) == ("long", "Long")


# The case most likely to repeat: retrying it every turn pays the full latency
# for the same non-answer.
def test_unknown_is_cached_too():
    d = _decorator()
    d._remember_identity(UNKNOWN_LABEL, None)
    assert d._cached_identity(in_followup=False) == (UNKNOWN_LABEL, None)


def test_zero_ttl_disables_reuse(monkeypatch):
    monkeypatch.setattr(sd, "SPEAKER_ID_CACHE_S", 0.0)
    monkeypatch.setattr(sd, "SPEAKER_ID_CACHE_FOLLOWUP_S", 0.0)
    d = _decorator()
    d._remember_identity("long", "Long")
    assert d._cached_identity(in_followup=False) is None
    assert d._cached_identity(in_followup=True) is None


def test_forget_identity_clears_it():
    d = _decorator()
    d._remember_identity("long", "Long")
    d.forget_identity()
    assert d._cached_identity(in_followup=False) is None


# A cache hit must not call the recognizer, and must still decorate THIS
# transcript with the remembered name.
def test_a_cache_hit_decorates_without_running_recognition():
    d = _decorator()
    d._speaker = mock.Mock()
    d._remember_identity("long", "Long")
    msg, se_user, display = d.identify_and_decorate("what time is it", [b"\x00" * 32000])
    assert msg == "Speaker - Long: what time is it"
    assert (se_user, display) == ("long", "Long")
    d._speaker.recognize.assert_not_called()


# A cached unknown has no WAV path to offer, so it returns the plain transcript
# rather than the enrolment-nudge message.
def test_a_cached_unknown_returns_the_plain_transcript():
    d = _decorator()
    d._speaker = mock.Mock()
    d._remember_identity(UNKNOWN_LABEL, None)
    msg, se_user, display = d.identify_and_decorate("hello", [b"\x00" * 32000])
    assert msg == "hello"
    assert (se_user, display) == (UNKNOWN_LABEL, None)
    d._speaker.recognize.assert_not_called()
