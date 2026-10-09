"""Muted startup can warm filters without opening or changing audio devices."""

import threading
from unittest.mock import Mock

import pytest

from hal.drivers.voice import aec
from hal.drivers.voice._internal import config


@pytest.fixture
def preparation(monkeypatch):
    existing = aec._prepare_thread
    if existing is not None:
        existing.join(5)
        assert not existing.is_alive()
    monkeypatch.setattr(aec, "_prepare_thread", None)
    monkeypatch.setattr(aec, "_prepare_pending", None)
    monkeypatch.setattr(aec, "_playback_rate", 44100)
    monkeypatch.setattr(config, "AEC_ENABLED", True)
    yield
    worker = aec._prepare_thread
    if worker is not None:
        worker.join(2)
        assert not worker.is_alive()


def test_filter_only_prepare_keeps_mic_and_reference_untouched(preparation, monkeypatch):
    canceller, reference = object(), object()
    monkeypatch.setattr(aec, "_canceller", canceller)
    monkeypatch.setattr(aec, "_reference", reference)
    configure = Mock(side_effect=AssertionError("must not configure microphone"))
    monkeypatch.setattr(aec, "configure", configure)
    filters = Mock()
    monkeypatch.setattr(aec, "_reference_resample_filter", filters)
    worker = aec.prepare_reference_background(16000)
    worker.join(1)
    assert not worker.is_alive()
    filters.assert_called_once_with(160, 441, "<f4")
    assert aec._canceller is canceller and aec._reference is reference
    configure.assert_not_called()


def test_many_requests_share_one_worker_and_one_pending_ratio(preparation, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def filters(*args):
        calls.append(args)
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(aec, "_reference_resample_filter", filters)
    worker = aec.prepare_reference_background(16000)
    try:
        assert entered.wait(1)
        for _ in range(20):
            assert aec.prepare_reference_background(48000) is worker
        release.set()
        worker.join(1)
        assert not worker.is_alive()
        assert calls == [(160, 441, "<f4"), (160, 147, "<f4")]
    finally:
        release.set()


def test_disabled_preparation_does_not_start_worker(preparation, monkeypatch):
    monkeypatch.setattr(config, "AEC_ENABLED", False)
    assert aec.prepare_reference_background(16000) is None
    assert aec._prepare_thread is None


def test_muted_voice_construction_prepares_filters_without_starting_capture(monkeypatch):
    from hal import app_state
    from hal.drivers.voice import voice_service as module

    for name in ("SileroVADFilter", "WebRTCVADFilter", "Backchannel", "SensingSender",
                 "SpeakerDecorator", "RealtimeOrchestrator"):
        monkeypatch.setattr(module, name, Mock())
    monkeypatch.setattr(module.hal_config, "VOICE_INPUT_MODE", "tap_to_talk")
    monkeypatch.setattr(app_state, "_mic_muted", True)
    monkeypatch.setattr(app_state, "_sleeping", True)
    prepare = Mock()
    monkeypatch.setattr(aec, "prepare_reference_background", prepare)
    capture = Mock(side_effect=AssertionError("must not open microphone"))
    monkeypatch.setattr(module.VoiceService, "_capture", capture)
    service = module.VoiceService(stt_provider=Mock(), alsa_device="test")
    prepare.assert_called_once_with(16000)
    assert not service._running and service._thread is None
    assert app_state._mic_muted and app_state._sleeping
    capture.assert_not_called()
