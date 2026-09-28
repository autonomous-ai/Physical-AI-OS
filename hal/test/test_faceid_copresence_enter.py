"""A stranger joining the user is announced WITH the user in the text (#426)."""

import numpy as np
import pytest

import hal.config as config
from hal.drivers.sensing.perceptions.models import Face, PersonKind
from hal.drivers.sensing.perceptions.processors.faceid import perception as perception_mod
from hal.drivers.sensing.perceptions.processors.faceid.perception import FacePerception
from hal.drivers.sensing.perceptions.processors.faceid.recognizer import FaceRecognizer
from hal.drivers.sensing.perceptions.utils import PerceptionStateObservers


def _face(kind: PersonKind, pid: str) -> Face:
    return Face(bbox=[0, 0, 10, 10], kind=kind, person_id=pid, confidence=0.9)


MOMO = _face(PersonKind.FRIEND, "momo")
STRANGER = _face(PersonKind.STRANGER, "stranger_2")
UNSURE = _face(PersonKind.UNSURE, "?")
FRAME = np.zeros((16, 16, 3), np.uint8)


@pytest.fixture
def perception(monkeypatch, tmp_path):
    """A FacePerception with no models, no watcher thread, no disk, no HTTP."""
    monkeypatch.setattr(FaceRecognizer, "start", lambda self: None)
    monkeypatch.setattr(FacePerception, "_start_watcher", lambda self: None)
    monkeypatch.setattr(FacePerception, "_load_presence_state", lambda self: None)
    monkeypatch.setattr(FacePerception, "_load_stranger_stats", lambda self: {})
    monkeypatch.setattr(FacePerception, "_persist_presence_state", lambda self, *a, **k: None)
    monkeypatch.setattr(FacePerception, "_post_wellbeing", lambda self, *a, **k: None)
    monkeypatch.setattr(FacePerception, "_track_stranger_visits", lambda self, ids: set())
    monkeypatch.setattr(FacePerception, "_annotate_frame", lambda self, frame, faces: frame)
    monkeypatch.setattr(perception_mod, "USERS_DIR", tmp_path / "users")
    monkeypatch.setattr(config, "FACE_COPRESENCE_MIN_TICKS", 2)

    events: list[tuple[str, str]] = []

    def send_event(event_type, message, prefix="", images=None, cooldown=None):
        events.append((event_type, message))

    p = FacePerception(PerceptionStateObservers(), send_event)
    # Ticks are microseconds apart; a zero flush interval reproduces the device's flush-on-mint.
    p._stranger_flush_interval = 0.0
    p.events = events  # type: ignore[attr-defined]
    return p


def _tick(perception, faces, monkeypatch):
    monkeypatch.setattr(perception._face_recognizer, "detect", lambda frame: list(faces))
    perception._check_impl(FRAME)


def _enters(perception) -> list[str]:
    return [m for t, m in perception.events if t == "presence.enter"]


def test_stranger_joining_momo_lists_momo_as_already_present(perception, monkeypatch):
    _tick(perception, [MOMO], monkeypatch)
    _tick(perception, [MOMO, UNSURE], monkeypatch)
    _tick(perception, [MOMO, STRANGER], monkeypatch)

    assert _enters(perception) == [
        "Person detected — new: friend (momo); faces in frame: 1 (momo)",
        "Person detected — new: stranger (stranger_2); "
        "already present: momo (friend); faces in frame: 2 (momo, stranger_2)",
    ]


def test_without_a_corroborating_tick_momo_is_not_listed(perception, monkeypatch):
    """One frame with two boxes is not enough for the "with you" phrasing."""
    _tick(perception, [MOMO], monkeypatch)
    _tick(perception, [MOMO, STRANGER], monkeypatch)

    assert _enters(perception)[-1] == (
        "Person detected — new: stranger (stranger_2); faces in frame: 2 (momo, stranger_2)"
    )


def test_momo_out_of_frame_is_not_already_present_even_inside_her_window(perception, monkeypatch):
    """The false-positive path from the issue: momo left, a lone stranger sat down."""
    _tick(perception, [MOMO], monkeypatch)
    _tick(perception, [], monkeypatch)
    _tick(perception, [UNSURE], monkeypatch)
    _tick(perception, [STRANGER], monkeypatch)

    assert _enters(perception)[-1] == (
        "Person detected — new: stranger (stranger_2); faces in frame: 1 (stranger_2)"
    )
    assert perception.current_user() == "momo"


def test_a_new_friend_lists_a_present_friend_without_the_guard(perception, monkeypatch):
    leo = _face(PersonKind.FRIEND, "leo")
    _tick(perception, [MOMO], monkeypatch)
    _tick(perception, [MOMO, leo], monkeypatch)

    assert _enters(perception)[-1] == (
        "Person detected — new: friend (leo); already present: momo (friend); "
        "faces in frame: 2 (momo, leo)"
    )


def test_copresence_counter_resets_when_the_frame_empties(perception, monkeypatch):
    _tick(perception, [MOMO, UNSURE], monkeypatch)
    assert perception._copresence_ticks == 1
    _tick(perception, [], monkeypatch)
    assert perception._copresence_ticks == 0


def test_delayed_flush_describes_the_buffered_snapshot_not_the_flush_tick(perception, monkeypatch):
    """The enter message reflects the faces seen on the tick the photo was minted."""
    perception._stranger_flush_interval = 10_000.0
    _tick(perception, [MOMO], monkeypatch)
    _tick(perception, [MOMO, UNSURE], monkeypatch)
    _tick(perception, [MOMO, STRANGER], monkeypatch)
    _tick(perception, [STRANGER], monkeypatch)
    perception._stranger_flush_interval = 0.0
    _tick(perception, [UNSURE], monkeypatch)

    assert _enters(perception)[-1] == (
        "Person detected — new: stranger (stranger_2); "
        "already present: momo (friend); faces in frame: 2 (momo, stranger_2)"
    )


def test_immediate_friend_send_still_describes_the_current_frame(perception, monkeypatch):
    """A new friend sends the CURRENT frame at once — its facts, not a stale buffer's."""
    perception._stranger_flush_interval = 10_000.0
    _tick(perception, [MOMO], monkeypatch)
    _tick(perception, [MOMO, UNSURE], monkeypatch)
    _tick(perception, [MOMO, STRANGER], monkeypatch)
    leo = _face(PersonKind.FRIEND, "leo")
    _tick(perception, [MOMO, leo], monkeypatch)

    assert _enters(perception)[-1] == (
        "Person detected — new: friend (leo); already present: momo (friend); "
        "faces in frame: 2 (momo, leo)"
    )
