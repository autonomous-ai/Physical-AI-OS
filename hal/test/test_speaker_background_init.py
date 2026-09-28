"""Optional voice perception must not gate startup or outlive its owner."""

import threading
from unittest import mock

import pytest

from hal.drivers.voice._internal import speaker_decorate as module
from hal.drivers.voice._internal.speaker_decorate import SpeakerDecorator


@pytest.fixture
def factories(monkeypatch):
    monkeypatch.setattr(module, 'SPEAKER_RECOGNITION_ENABLED', True)
    monkeypatch.setattr(module, 'SPEECH_EMOTION_ENABLED', True)
    speaker = mock.Mock()
    emotion = mock.Mock()
    monkeypatch.setattr(SpeakerDecorator, '_init_speaker', staticmethod(speaker))
    monkeypatch.setattr(SpeakerDecorator, '_init_speech_emotion', staticmethod(emotion))
    return speaker, emotion


def finish(decorator):
    decorator.close()
    for thread in decorator._init_threads:
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_slow_key_fetch_does_not_block_transcripts_or_other_service(factories):
    speaker, emotion = factories
    entered, release = threading.Event(), threading.Event()

    def slow_speaker():
        entered.set()
        assert release.wait(3)
        return speaker.return_value

    speaker.side_effect = slow_speaker
    d = SpeakerDecorator(['lamp'], 0)
    try:
        assert entered.wait(1)
        # SER can finish independently while Speaker-ID's network call is stuck.
        d._init_threads[1].join(timeout=1)
        assert d._speech_emotion is emotion.return_value
        assert d.identify_and_decorate('hello lamp', [b'audio']) == ('hello lamp', None, None)
        assert d._identity_cache is None
        release.set()
        d._init_threads[0].join(timeout=1)
        assert d._speaker is speaker.return_value
    finally:
        release.set()
        finish(d)
    speaker.return_value.stop.assert_not_called()  # Process-wide singleton.
    emotion.return_value.stop.assert_called_once()


def test_close_during_ser_init_disposes_late_service(factories):
    _, emotion = factories
    entered, release = threading.Event(), threading.Event()

    def slow_emotion():
        entered.set()
        assert release.wait(3)
        return emotion.return_value

    emotion.side_effect = slow_emotion
    d = SpeakerDecorator(['lamp'], 0)
    try:
        assert entered.wait(1)
        d.submit_speech_emotion_from_session([b'audio'])
        emotion.return_value.submit.assert_not_called()
        d.close()
        assert d._speech_emotion is None
    finally:
        release.set()
        finish(d)
    emotion.return_value.stop.assert_called_once()
    assert d._speech_emotion is None


@pytest.mark.parametrize('capability,enabled', [(False, True), (True, False)])
def test_disabled_services_do_not_start_or_fetch(factories, monkeypatch, capability, enabled):
    monkeypatch.setattr(module, 'SPEAKER_RECOGNITION_ENABLED', enabled)
    monkeypatch.setattr(module, 'SPEECH_EMOTION_ENABLED', enabled)
    d = SpeakerDecorator(['lamp'], 0, enable_people_perception=capability)
    finish(d)
    assert d._init_threads == []
    for factory in factories:
        factory.assert_not_called()


def test_init_failure_does_not_break_voice_and_close_cancels_retry(factories):
    speaker, emotion = factories
    entered = threading.Event()

    def failure():
        entered.set()
        raise TimeoutError('mock key fetch timeout')

    speaker.side_effect = failure
    d = SpeakerDecorator(['lamp'], 0)
    try:
        assert entered.wait(1)
        d._init_threads[1].join(timeout=1)
        assert d._speech_emotion is emotion.return_value
        assert d.identify_and_decorate('hello', []) == ('hello', None, None)
    finally:
        finish(d)
    speaker.assert_called_once()


def test_failed_construction_retries_without_blocking_caller(factories):
    speaker, _ = factories
    d = SpeakerDecorator(['lamp'], 0, enable_people_perception=False)
    speaker.side_effect = [None, speaker.return_value]
    # Advance the retry clock without a real 30-second sleep.
    with mock.patch.object(d._init_stop, 'wait', return_value=False) as wait:
        d._init_optional_service('_speaker', speaker)
    wait.assert_called_once_with(30.0)
    assert speaker.call_count == 2
    assert d._speaker is speaker.return_value
    finish(d)


def test_thread_start_failure_cancels_already_started_worker(factories):
    entered, release = threading.Event(), threading.Event()
    speaker, _ = factories

    def blocked():
        entered.set()
        assert release.wait(3)
        return speaker.return_value

    speaker.side_effect = blocked
    original_start = threading.Thread.start
    threads = []

    def start(thread):
        if threads:
            raise RuntimeError('mock thread start failure')
        threads.append(thread)
        original_start(thread)
        assert entered.wait(1)

    try:
        with mock.patch.object(threading.Thread, 'start', start):
            with pytest.raises(RuntimeError, match='mock thread'):
                SpeakerDecorator(['lamp'], 0)
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
    speaker.return_value.stop.assert_not_called()
