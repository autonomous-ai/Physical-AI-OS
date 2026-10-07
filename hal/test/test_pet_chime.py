"""Head-pet feedback stays soft and follows the shared playback safeguards."""
from unittest.mock import Mock

import numpy as np
import pytest

from hal.drivers.voice.tts.service import TTSService


@pytest.mark.parametrize("rate", [24000, 48000])
def test_pet_cue_is_soft_with_silent_endpoints(rate):
    service = TTSService.__new__(TTSService)
    service._np = np
    service._ack_chime_cache = None
    samples = service._pet_chime_samples(rate)
    assert samples.shape == (int(rate * .18), 1)
    assert samples.dtype == np.float32
    assert np.isfinite(samples).all()
    assert abs(samples[0, 0]) < 1e-6
    assert abs(samples[-1, 0]) < 1e-6
    assert 0.05 < np.max(abs(samples)) < 0.21
    assert np.max(abs(samples)) < np.max(abs(service._ack_chime_samples(rate)))


def test_muted_speaker_does_not_play_pet_cue():
    service = TTSService.__new__(TTSService)
    service._backend = Mock(available=True)
    service._sd = Mock()
    service._speaker_muted = lambda: True
    service._ensure_stream = Mock()
    assert service.play_pet_chime() is False
    service._ensure_stream.assert_not_called()


def test_pet_cue_uses_shared_gesture_playback():
    service = TTSService.__new__(TTSService)
    service._play_gesture_chime = Mock(return_value=True)
    assert service.play_pet_chime() is True
    service._play_gesture_chime.assert_called_once_with(service._pet_chime_samples)
