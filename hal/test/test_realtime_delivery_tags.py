"""Delivery cues retain their sentence and incomplete cues never reach speech."""
from types import SimpleNamespace
import pytest
from hal.drivers.voice._internal.realtime_turn import split_delivery_sentence, realtime_speech_text
from hal.drivers.voice.voice_service import VoiceService


@pytest.mark.parametrize('text,head,tail', [
    ('Hello. [whispers] Keep this secret.', 'Hello. ', '[whispers] Keep this secret.'),
    ('Hello. [excited, happy] You did it!', 'Hello. ', '[excited, happy] You did it!'),
    ('A joke! [laughs] [calm] Goodnight.', 'A joke! [laughs] ', '[calm] Goodnight.'),
    ('A joke! [laughs]', '', 'A joke! [laughs]'),
    ('A joke! [lau', '', 'A joke! [lau'),
])
def test_delivery_sentence_ownership(text, head, tail):
    assert split_delivery_sentence(text) == (head, tail)


@pytest.mark.parametrize('text,expected', [
    ('Hello. [laugh', 'Hello.'),
    ('[HW:/led/off:{}][laughs] Hi.', '[laughs] Hi.'),
    ('[thinking] [voice trembling] Hello.', '[voice trembling] Hello.'),
])
def test_tts_delivery_cleaner(text, expected):
    assert realtime_speech_text(text, SimpleNamespace(_provider='elevenlabs'), VoiceService.strip_rt_markers) == expected


def test_nested_control_marker_is_not_split():
    tts = SimpleNamespace(_provider='elevenlabs')
    head, tail = split_delivery_sentence('Hello. [HW:/led/off:{"color":[255,0,0]}] Next.')
    assert realtime_speech_text(head, tts, VoiceService.strip_rt_markers) == 'Hello.'
    assert realtime_speech_text(tail, tts, VoiceService.strip_rt_markers) == 'Next.'


def test_long_opening_clause_still_streams(monkeypatch):
    from hal.drivers.voice._internal.realtime_turn import split_realtime_first_chunk, hal_config
    monkeypatch.setattr(hal_config, 'REALTIME_FIRST_CHUNK_MAX_CHARS', 60)
    tts = SimpleNamespace(_provider='elevenlabs')
    head, tail = split_realtime_first_chunk('[calm] Take a slow breath, we can work through this together', tts, VoiceService.strip_rt_markers)
    assert head == '[calm] Take a slow breath,'
    assert tail.strip() == 'we can work through this together'
    assert split_realtime_first_chunk('Take a slow breath. [laughs]', tts, VoiceService.strip_rt_markers)[0] == ''


@pytest.mark.parametrize('width', [1, 2, 7, 24, 1000])
def test_network_chunk_boundaries_preserve_every_word_and_tag(width):
    text = '[calm] Hello. [whispers] Keep this secret. [laughs] Goodnight! [sighs]'
    buf = ''
    sent = []
    for offset in range(0, len(text), width):
        buf += text[offset:offset + width]
        head, tail = split_delivery_sentence(buf)
        if head:
            sent.append(head)
            buf = tail
    sent.append(buf)
    assert ''.join(sent) == text
    assert any('[whispers] Keep this secret.' in part for part in sent)
    assert sent[-1].endswith('Goodnight! [sighs]')
