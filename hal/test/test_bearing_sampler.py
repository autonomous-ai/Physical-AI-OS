"""Passive bearing sampling."""

from unittest import mock

import numpy as np
import pytest

import hal.app_state as state
import hal.config as cfg
from hal.drivers.tracking import aim, bearing_sampler


def _frame(w=1280, h=720):
    return np.zeros((h, w, 3), dtype=np.uint8)


class _Svc:
    _tracking_active = False

    def __init__(self):
        self.get_positions = mock.Mock(return_value={
            "base_yaw.pos": 10.0, "base_pitch.pos": 5.0, "elbow_pitch.pos": 20.0,
        })


class _Cap:
    def acquire_consumer(self): pass
    def release_consumer(self): pass
    last_frame_ts = 1e9

    def __init__(self, frame):
        self.last_frame = frame


def _run(box, svc=None, tracking=False, disabled=False):
    """Returns (recorded_or_None, svc). `box` is (x, y, w, h) or None."""
    svc = svc or _Svc()
    svc._tracking_active = tracking
    det = mock.Mock()
    det.detect = mock.Mock(
        side_effect=lambda f, t, strict=True, min_conf=None: box if t == "face" else None
    )
    det.last_confidence = 0.9
    recorded = {}

    def _record(bearing, pose=None, now=None):
        recorded["bearing"] = bearing
        recorded["pose"] = pose
        return True

    with (
        mock.patch.object(state, "camera_capture", _Cap(_frame())),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "_camera_disabled", disabled, create=True),
        mock.patch.object(aim, "get_detector", return_value=det),
        mock.patch("hal.drivers.tracking.user_bearing.record_sighting", _record),
    ):
        ok = bearing_sampler._sample_once()
    return (recorded if ok else None), svc


def test_a_close_centred_face_is_recorded_with_posture():
    rec, _ = _run((490, 210, 300, 300))
    assert rec is not None
    assert rec["pose"] is not None, "a centred subject should teach the posture"
    assert rec["pose"]["base_pitch.pos"] == 5.0


def test_a_far_face_is_ignored():
    """A far face (a colleague across the office) is ignored."""
    rec, _ = _run((600, 300, 28, 30))
    assert rec is None


def test_bearing_is_corrected_for_a_horizontal_offset():
    """Off-centre is fine for direction: bearing = yaw + dx x scale."""
    rec, _ = _run((740, 210, 300, 300))
    assert rec is not None
    assert rec["bearing"] > 10.0, rec


def test_a_large_horizontal_offset_is_not_recorded():
    """That correction leans on the FOV constant, so it is only trusted small."""
    rec, _ = _run((1000, 210, 280, 300))
    assert rec is None


def test_a_face_high_in_frame_still_teaches_its_posture():
    """A face high in frame still records its posture."""
    rec, _ = _run((490, 0, 150, 150))
    assert rec is not None, "the bearing is still usable"
    assert rec["pose"] is not None, "and so is the posture that saw the face"


def test_nothing_is_sampled_while_the_body_is_busy():
    """Mid-aim the pose is in flight and the detector is in use."""
    rec, _ = _run((490, 210, 300, 300), tracking=True)
    assert rec is None


def test_nothing_is_sampled_when_the_camera_is_disabled():
    """Privacy: never watch someone who asked us not to."""
    rec, _ = _run((490, 210, 300, 300), disabled=True)
    assert rec is None


def test_a_look_in_progress_is_never_made_to_wait():
    """The sampler takes the detector lock non-blocking so it never delays a user turn."""
    aim._detector_lock_use.acquire()
    try:
        rec, _ = _run((490, 210, 300, 300))
    finally:
        aim._detector_lock_use.release()
    assert rec is None


def _run_with_snapshots(tmp_path, box, monkeypatch):
    monkeypatch.setattr(cfg, "SNAPSHOT_PERSIST_DIR", str(tmp_path), raising=False)
    rec, _ = _run(box)
    d = tmp_path / bearing_sampler.SNAPSHOT_CATEGORY
    return rec, (sorted(p.name for p in d.iterdir()) if d.exists() else [])


def test_a_recorded_sample_is_pictured(tmp_path, monkeypatch):
    rec, files = _run_with_snapshots(tmp_path, (490, 210, 300, 300), monkeypatch)
    assert rec is not None
    assert len(files) == 1, files


def test_a_rejected_far_detection_is_also_pictured(tmp_path, monkeypatch):
    """A rejected far detection is still captured for debugging."""
    rec, files = _run_with_snapshots(tmp_path, (600, 300, 28, 30), monkeypatch)
    assert rec is None
    assert len(files) == 1, "the dismissed detection left no evidence"


def test_nothing_is_written_when_nothing_was_detected(tmp_path, monkeypatch):
    rec, files = _run_with_snapshots(tmp_path, None, monkeypatch)
    assert rec is None
    assert files == []


def test_oldest_snapshots_are_evicted(tmp_path, monkeypatch):
    """One frame per interval accumulates forever otherwise."""
    monkeypatch.setattr(cfg, "SNAPSHOT_PERSIST_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(cfg, "BEARING_SNAPSHOT_KEEP", 3, raising=False)
    d = tmp_path / bearing_sampler.SNAPSHOT_CATEGORY
    d.mkdir(parents=True)
    for i in range(6):
        (d / f"20260819-0000{i}.jpg").write_bytes(b"x")
    bearing_sampler._prune(str(d))
    left = sorted(p.name for p in d.iterdir())
    assert left == ["20260819-00003.jpg", "20260819-00004.jpg", "20260819-00005.jpg"], left


def test_snapshots_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "SNAPSHOT_PERSIST_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(cfg, "BEARING_SNAPSHOT_ENABLED", False, raising=False)
    _run((490, 210, 300, 300))
    assert not (tmp_path / bearing_sampler.SNAPSHOT_CATEGORY).exists()


def test_a_co_worker_sized_face_is_not_recorded():
    """#545: the sampler used look-aim's 8% floor, so a centred co-worker at 8-15% taught the bearing.

    green-lamp 2026-09-30: the neighbouring co-worker measured up to 13.6%.
    """
    h = int(0.136 * 720)
    rec, _ = _run((640 - h // 2, 210, h, h))
    assert rec is None


def test_a_face_at_the_bearing_floor_is_recorded():
    """The same floor every other bearing path uses (GAZE_BEARING_MIN_FACE_HEIGHT_FRAC)."""
    h = int((cfg.GAZE_BEARING_MIN_FACE_HEIGHT_FRAC + 0.01) * 720)
    rec, _ = _run((640 - h // 2, 210, h, h))
    assert rec is not None
