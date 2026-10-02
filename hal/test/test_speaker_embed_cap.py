"""Speaker-ID clips are capped before the embed call (#555)."""

import base64

import numpy as np
import pytest

from hal.drivers.voice.speaker_recognizer import speaker_recognizer as sr

SR = 16_000


class _Passthrough:
    def process(self, audio):
        return audio


@pytest.fixture
def recognizer(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_get_audio_processor", lambda: _Passthrough())
    return sr.SpeakerRecognizer(
        api_url="http://unused", api_key="k", users_dir=tmp_path, match_threshold=0.5,
    )


def _wav(seconds: float) -> bytes:
    rng = np.random.default_rng(0)
    w = (rng.standard_normal(int(seconds * SR)) * 0.1).astype(np.float32)
    return sr._float32_waveform_to_wav_bytes(w)


def _payload_seconds(payload: list[str]) -> float:
    w = sr._wav_bytes_to_float32_16k_mono(base64.b64decode(payload[0]))
    return w.shape[0] / SR


def test_default_cap_is_20s():
    assert sr.config.SPEAKER_MAX_EMBED_AUDIO_S == 20.0


def test_long_clip_is_capped(recognizer):
    payload, duration_s = recognizer._prepare_wav_for_embedding(_wav(30.0))
    assert duration_s == pytest.approx(20.0)
    assert _payload_seconds(payload) == pytest.approx(20.0)


@pytest.mark.parametrize("seconds", [10.0, 15.0, 20.0])
def test_clip_within_cap_is_unchanged(recognizer, seconds):
    payload, duration_s = recognizer._prepare_wav_for_embedding(_wav(seconds))
    assert duration_s == pytest.approx(seconds)
    assert _payload_seconds(payload) == pytest.approx(seconds)
