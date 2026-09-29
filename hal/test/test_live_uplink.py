"""Keep playback and room echo out of provider VAD without losing audio time."""

from types import SimpleNamespace

import numpy as np
import pytest

from hal.drivers.voice import voice_service as module


@pytest.fixture
def uplink(monkeypatch):
    service = object.__new__(module.VoiceService)
    service._np = np
    service._tts = SimpleNamespace(speaking=False)
    service._live_frames_during_playback = 0
    service._live_frames_substituted = 0
    service._live_playback_was_speaking = False
    service._live_playback_tail_until = 0.0
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(module.voice_cfg, "LIVE_PLAYBACK_TAIL_S", 0.35)
    monkeypatch.setattr(module.voice_cfg, "LIVE_UPLINK_DURING_PLAYBACK", "mute")
    monkeypatch.setattr(module.aec, "reference_idle_for", lambda: float("inf"))
    monkeypatch.setattr(module.aec, "uncancelled", lambda: True)
    return service, clock


@pytest.mark.parametrize("mode", ["mute", "cancelled"])
def test_no_aec_blocks_echo_through_tail_then_passes_user(uplink, monkeypatch, mode):
    service, clock = uplink
    monkeypatch.setattr(module.voice_cfg, "LIVE_UPLINK_DURING_PLAYBACK", mode)
    frame = np.full((320, 1), 12000, dtype=np.int16)
    assert service._live_uplink_frame(frame) is frame
    service._tts.speaking = True
    muted = service._live_uplink_frame(frame)
    assert muted.shape == frame.shape and muted.dtype == frame.dtype
    assert not muted.any()
    service._tts.speaking = False
    clock.now = 11.0
    assert not service._live_uplink_frame(frame).any()
    clock.now = 11.34
    assert not service._live_uplink_frame(frame).any()
    clock.now = 11.36
    assert service._live_uplink_frame(frame) is frame
    assert frame.all()


def test_reference_tail_also_protects_playback_between_capture_reads(uplink, monkeypatch):
    service, _ = uplink
    monkeypatch.setattr(module.aec, "reference_idle_for", lambda: 0.1)
    assert not service._live_uplink_frame(np.ones(320, dtype=np.int16)).any()


def test_queued_segment_restarts_tail(uplink):
    service, clock = uplink
    frame = np.ones(320, dtype=np.int16)
    for at, speaking in [(10.0, True), (10.1, False), (10.2, True), (11.0, False), (11.3, False)]:
        clock.now = at
        service._tts.speaking = speaking
        assert not service._live_uplink_frame(frame).any()
    clock.now = 11.4
    assert service._live_uplink_frame(frame) is frame


@pytest.mark.parametrize("mode,cancelled", [("always", False), ("cancelled", True)])
def test_explicit_duplex_modes_preserve_barge_in(uplink, monkeypatch, mode, cancelled):
    service, clock = uplink
    monkeypatch.setattr(module.voice_cfg, "LIVE_UPLINK_DURING_PLAYBACK", mode)
    monkeypatch.setattr(module.aec, "uncancelled", lambda: not cancelled)
    service._tts.speaking = True
    frame = np.ones(320, dtype=np.int16)
    assert service._live_uplink_frame(frame) is frame
    service._tts.speaking = False
    clock.now += 1
    assert service._live_uplink_frame(frame) is frame
