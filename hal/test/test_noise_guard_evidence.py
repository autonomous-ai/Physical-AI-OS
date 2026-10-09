"""Regression coverage for measured short noise bursts and manual speech."""

from unittest.mock import Mock

import pytest

from hal import config
from hal.drivers.voice._internal.noise_guard import accepts_speech_metrics
from hal.drivers.voice.voice_service import VoiceService


def decision(span_ratio, span_seconds, *, ratio=.45, minimum=160):
    return accepts_speech_metrics(
        (.9, .2, .2, span_ratio, span_seconds),
        min_ratio=ratio, min_voiced_ms=minimum,
    )


@pytest.mark.parametrize('span_ratio,span_seconds', [
    (1, .032), (1, .064), (2/3, .096),  # Isolated lamp background spikes.
    (1, .128), (2/3, .192),             # Servo / speaker echo bursts.
    (18/46, 1.472), (7/31, .992), (4/34, 1.088),  # Sparse longer noise.
])
def test_lamp_noise_is_rejected(span_ratio, span_seconds):
    assert not decision(span_ratio, span_seconds)


def test_original_rejected_short_speech_passes_from_logged_metrics():
    # 09:02:26 waveform was not saved; this reproduces its logged metrics only.
    assert not decision(.480, 1.60, ratio=.55, minimum=0)
    assert decision(.480, 1.60)


@pytest.mark.parametrize('span_ratio,span_seconds', [(.917, .768), (.686, 1.12)])
def test_recorded_human_speech_is_retained(span_ratio, span_seconds):
    assert decision(span_ratio, span_seconds)


def test_duration_counts_voiced_time_not_span_or_total_capture():
    assert not decision(.5, .256)  # 128 ms voiced within a 256 ms span.
    assert decision(.5, .320)      # Inclusive 160 ms boundary.
    assert not decision(1, .128)
    assert decision(1, .160)


def test_zero_floor_preserves_legacy_ratio_decision():
    assert decision(1, .032, ratio=.55, minimum=0)
    assert not decision(.54, 3, ratio=.55, minimum=0)


@pytest.mark.parametrize('realtime_enabled', [False, True])
def test_service_applies_shared_policy_even_without_realtime(monkeypatch, realtime_enabled):
    monkeypatch.setattr(config, 'REALTIME_ENABLED', realtime_enabled)
    monkeypatch.setattr(config, 'REALTIME_NOISE_SPEECH_RATIO', .45)
    monkeypatch.setattr(config, 'VOICE_NOISE_MIN_VOICED_MS', 160)
    service = object.__new__(VoiceService)
    service._rt_noise_vad = Mock()
    service._rt_noise_vad.speech_metrics.return_value = (.9, .2, .01, 1, .032)
    assert not service._rt_noise_is_speech(b'pcm')
    service._rt_noise_vad.reset_state.assert_called_once()
    service._rt_noise_vad.speech_metrics.return_value = (1, .4, .444, .48, 1.6)
    assert service._rt_noise_is_speech(b'pcm')


@pytest.mark.parametrize('metrics', [(1, 1, 1, 1, 0), RuntimeError('inference failed')])
def test_unavailable_or_failed_model_still_fails_open(monkeypatch, metrics):
    monkeypatch.setattr(config, 'REALTIME_NOISE_SPEECH_RATIO', .45)
    monkeypatch.setattr(config, 'VOICE_NOISE_MIN_VOICED_MS', 160)
    service = object.__new__(VoiceService)
    service._rt_noise_vad = Mock()
    if isinstance(metrics, Exception):
        service._rt_noise_vad.speech_metrics.side_effect = metrics
    else:
        service._rt_noise_vad.speech_metrics.return_value = metrics
    assert service._rt_noise_is_speech(b'pcm')


def test_measured_silence_is_not_the_fail_open_sentinel():
    assert not accepts_speech_metrics((0, 0, 0, 0, 0), min_ratio=.45, min_voiced_ms=160)
