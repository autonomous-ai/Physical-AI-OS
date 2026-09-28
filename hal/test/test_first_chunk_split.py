"""Time-to-first-audio: the opening of a turn is cut at a clause boundary."""

import pytest

from hal import config as hal_config
from hal.drivers.voice._internal.realtime_turn import split_first_chunk


@pytest.fixture(autouse=True)
def enable_early_cut(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_FIRST_CHUNK_MAX_CHARS", 90)


def test_waits_while_the_opening_is_still_a_runt():
    assert split_first_chunk("Sure,") == ("", "Sure,")


def test_speaks_the_head_at_the_last_clause_boundary():
    head, rest = split_first_chunk("Well, that depends on where you are, but I can check")
    assert head == "Well, that depends on where you are,"
    assert rest.strip() == "but I can check"


def test_falls_back_to_a_word_break_past_the_cap():
    buf = " ".join(["word"] * 30)
    head, rest = split_first_chunk(buf)
    assert head and rest
    assert len(head) <= hal_config.REALTIME_FIRST_CHUNK_MAX_CHARS
    # Nothing is lost or duplicated; the two halves are spoken in order.
    assert (head + " " + rest).split() == buf.split()


def test_short_comma_less_opening_keeps_buffering():
    assert split_first_chunk("Hello there") == ("", "Hello there")


def test_zero_disables_the_early_cut(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_FIRST_CHUNK_MAX_CHARS", 0)
    text = "Well, that depends on where you are, but I can check"
    assert split_first_chunk(text) == ("", text)


@pytest.mark.parametrize("text", [
    "Chào bạn, hôm nay bạn thế nào?",
    "The time is 10:30.",
    "The price is 1,000 dollars.",
])
def test_complete_sentence_is_never_split(text):
    assert split_first_chunk(text) == ("", text)


@pytest.mark.parametrize("text", [
    "The time is 10:", "The time is 10:30",
    "The price is 1,", "The price is 1,000 dollars",
    "[softly, warmly] Hello there", "[softly,", "[laugh] Hi,",
    "Read https://example.com for details",
])
def test_non_clause_punctuation_does_not_start_speech(text):
    assert split_first_chunk(text) == ("", text)


def test_tag_is_preserved_with_a_real_clause():
    text = "[softly, warmly] Hello there, welcome"
    assert split_first_chunk(text) == ("[softly, warmly] Hello there,", " welcome")


def test_word_cap_never_splits_inside_a_tag(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_FIRST_CHUNK_MAX_CHARS", 20)
    text = "[speaking softly and very warmly] Hello there"
    assert split_first_chunk(text) == ("", text)


@pytest.mark.parametrize('text,head,tail', [
    ('I am here. Let me', 'I am here.', ' Let me'),
    ('First! Second? Third', 'First! Second?', ' Third'),
    ('[warmly] I am here. Let me', '[warmly] I am here.', ' Let me'),
    ('Xin chào。 Tôi đang', 'Xin chào。', ' Tôi đang'),
    ('Ask Dr. Smith', '', 'Ask Dr. Smith'),
    ('The amount is 3. 14 dollars', '', 'The amount is 3. 14 dollars'),
    ('I am here. [warm', '', 'I am here. [warm'),
    ('[tag. inside] Not finished', '', '[tag. inside] Not finished'),
    ('Check example.com now', '', 'Check example.com now'),
])
def test_completed_prefix_preserves_pending_text(text, head, tail):
    from hal.drivers.voice._internal.realtime_turn import split_completed_prefix
    assert split_completed_prefix(text) == (head, tail)
    assert head + tail == text
