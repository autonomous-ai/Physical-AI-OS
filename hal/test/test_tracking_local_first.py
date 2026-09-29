"""Keeping a local-model target on the local model."""

import numpy as np

from hal.drivers.tracking import constants as C
from hal.drivers.tracking import detection
from hal.drivers.tracking.tracker_service import trust_window_s


class _EmptyYolo:
    """A local model that runs fine and simply finds nothing this frame."""

    names: dict[int, str] = {}

    def __call__(self, *args, **kwargs):
        return []


def _detector(monkeypatch, remote_calls: list):
    monkeypatch.setattr(detection, "_get_local_yolo", lambda: _EmptyYolo())
    monkeypatch.setattr(detection, "_DETECT_LOCAL_ENABLED", True)
    det = detection.ObjectDetector.__new__(detection.ObjectDetector)
    det._on_confidence = None
    det.last_confidence = None
    det._last_remote_attempt_t = 0.0
    det._crypto = None
    monkeypatch.setattr(
        det, "_detect_remote",
        lambda frame, target, up: remote_calls.append(target) or None,
    )
    return det


def _frame():
    return np.zeros((480, 640, 3), dtype=np.uint8)


def test_coco_miss_does_not_reach_remote_mid_session(monkeypatch):
    """A local miss on a routine redetect must not spend a network round-trip on remote."""
    calls: list = []
    det = _detector(monkeypatch, calls)
    assert det.detect(_frame(), "cup", strict=False,
                      allow_remote_fallback=False) is None
    assert calls == []


def test_coco_miss_still_reaches_remote_when_seeding(monkeypatch):
    """With no lock to protect, the slow detector is worth waiting for."""
    calls: list = []
    det = _detector(monkeypatch, calls)
    assert det.detect(_frame(), "cup") is None
    assert calls == ["cup"]


def test_open_vocab_target_always_reaches_remote(monkeypatch):
    """The flag must not strand a target that has no local path at all."""
    calls: list = []
    det = _detector(monkeypatch, calls)
    assert det.detect(_frame(), "rubik cube", strict=False,
                      allow_remote_fallback=False) is None
    assert calls == ["rubik cube"]


def test_trust_window_floors_at_the_constant_for_a_fast_detector():
    """YuNet at ~30ms keeps exactly the window face tracking always had."""
    assert trust_window_s(0.03) == C.TRUST_TRACKER_S
    assert trust_window_s(0.0) == C.TRUST_TRACKER_S


def test_trust_window_grows_with_a_slower_detector():
    """A cup confirmed on a ~0.6s detector gets room for one missed confirm."""
    window = trust_window_s(0.6)
    assert window > C.TRUST_TRACKER_S
    assert window == C.YOLO_REDETECT_S + 1.2 + C.TRUST_MARGIN_S
    assert window > C.YOLO_REDETECT_S + 0.6
