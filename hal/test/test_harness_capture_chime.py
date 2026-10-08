"""Harness start/end cues differ in pitch order and respect shared mute policy."""
from unittest.mock import Mock
import numpy as np
import pytest
from hal.drivers.voice.tts.service import TTSService


def test_capture_cues_have_opposite_pitch_order_and_differ_from_ack():
    service = TTSService.__new__(TTSService)
    service._np = np
    service._ack_chime_cache = None
    rate = 24000
    start = service._harness_capture_chime_samples(rate, finished=False)
    end = service._harness_capture_chime_samples(rate, finished=True)
    count = int(rate * .08)

    def pitch(samples):
        return np.fft.rfftfreq(count, 1 / rate)[np.argmax(abs(np.fft.rfft(samples[:, 0])))]

    assert pitch(start[:count]) < pitch(start[-count:])
    assert pitch(end[:count]) > pitch(end[-count:])
    assert start.shape == end.shape
    assert np.max(abs(start)) <= .28
    assert len(start) != len(service._ack_chime_samples(rate))
    assert np.all(start[count:-count] == 0)


@pytest.mark.parametrize('finished', [False, True])
def test_muted_speaker_does_not_play_capture_cue(finished):
    service = TTSService.__new__(TTSService)
    service._sd = Mock()
    service._backend = Mock(available=True)
    service._speaker_muted = lambda: True
    service._ensure_stream = Mock()
    assert service.play_harness_capture_chime(finished=finished) is False
    service._ensure_stream.assert_not_called()


@pytest.mark.parametrize('finished', [False, True])
def test_device_capture_cue_is_short_and_respects_mute(finished):
    service = TTSService.__new__(TTSService)
    service._np = np
    samples = service._device_capture_chime_samples(24000, finished=finished)
    assert samples.shape == (960, 1)
    assert samples.dtype == np.float32
    assert np.isfinite(samples).all()
    assert np.max(abs(samples)) <= .28
    service._sd = Mock()
    service._backend = Mock(available=True)
    service._speaker_muted = lambda: True
    service._ensure_stream = Mock()
    assert service.play_device_capture_chime(finished=finished) is False
    service._ensure_stream.assert_not_called()
