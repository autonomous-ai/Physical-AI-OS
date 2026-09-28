"""A stranger joining the user is announced WITH the user in the text (#426).

Replays the green-lamp log of 2026-09-16 11:23 tick by tick through
`FacePerception._check_impl` with the recognizer stubbed: momo alone (greeted),
momo + an unsure box (the recognizer corroborating), momo + stranger_2 (minted).
The stranger is greeted once they have looked at the lamp on 2 of their last 3
ticks (#531), and the enter lists momo as already present when both are
matched in that frame.
"""

import time

import numpy as np
import pytest

from hal.drivers.sensing.perceptions.models import Face, PersonKind
from hal.drivers.sensing.perceptions.processors.faceid import perception as perception_mod
from hal.drivers.sensing.perceptions.processors.faceid.perception import FacePerception
from hal.drivers.sensing.perceptions.processors.faceid.recognizer import FaceRecognizer
from hal.drivers.sensing.perceptions.utils import PerceptionStateObservers

FRAME = np.zeros((480, 640, 3), np.uint8)
BOX = [270, 160, 370, 280]
FRONTAL = [300.0, 200.0, 340.0, 200.0, 320.0, 225.0, 305.0, 250.0, 335.0, 250.0]
PROFILE = [300.0, 200.0, 340.0, 200.0, 340.0, 225.0, 305.0, 250.0, 335.0, 250.0]


def _face(kind: PersonKind, pid: str, kps: list[float] | None = None) -> Face:
    return Face(bbox=BOX, kind=kind, person_id=pid, confidence=0.9, kps=kps)


MOMO = _face(PersonKind.FRIEND, "momo")
STRANGER = _face(PersonKind.STRANGER, "stranger_2", FRONTAL)        # looking at the lamp
STRANGER_AWAY = _face(PersonKind.STRANGER, "stranger_2", PROFILE)   # turned away
UNSURE = _face(PersonKind.UNSURE, "?")


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
    monkeypatch.setattr(perception_mod, "_STRANGER_SNAPSHOTS_DIR", tmp_path / "strangers")

    events: list[tuple[str, str, list]] = []

    def send_event(event_type, message, prefix="", images=None, cooldown=None):
        events.append((event_type, message, list(images or [])))

    p = FacePerception(PerceptionStateObservers(), send_event)
    p.events = events  # type: ignore[attr-defined]
    return p


def _tick(perception, faces, monkeypatch):
    monkeypatch.setattr(perception._face_recognizer, "detect", lambda frame: list(faces))
    perception._check_impl(FRAME)


def _enters(perception) -> list[str]:
    return [m for t, m, _ in perception.events if t == "presence.enter"]


def _enter_images(perception) -> list[list]:
    return [imgs for t, _, imgs in perception.events if t == "presence.enter"]


def test_stranger_joining_momo_lists_momo_as_already_present(perception, monkeypatch):
    _tick(perception, [MOMO], monkeypatch)             # 11:12 momo arrives
    _tick(perception, [MOMO, UNSURE], monkeypatch)     # recognizer corroborating
    _tick(perception, [MOMO, STRANGER], monkeypatch)   # 11:23 stranger_2 minted, facing 1/1
    _tick(perception, [MOMO, STRANGER], monkeypatch)   # facing 2/2 -> greeted

    assert _enters(perception) == [
        "Person detected — new: friend (momo); faces in frame: 1 (momo)",
        "Person detected — new: stranger (stranger_2); "
        "already present: momo (friend); faces in frame: 2 (momo, stranger_2)",
    ]


def test_known_stranger_next_to_momo_lists_momo(perception, monkeypatch):
    """#531: a stranger re-matched as a known stranger_N on its first tick has
    no `unsure` tick before it. Matched in the same frame as momo is enough."""
    _tick(perception, [MOMO], monkeypatch)
    _tick(perception, [MOMO, STRANGER], monkeypatch)
    _tick(perception, [MOMO, STRANGER], monkeypatch)

    assert _enters(perception)[-1] == (
        "Person detected — new: stranger (stranger_2); "
        "already present: momo (friend); faces in frame: 2 (momo, stranger_2)"
    )


def test_momo_out_of_frame_is_not_already_present_even_inside_her_window(perception, monkeypatch):
    """The false-positive path from #426: momo left, a lone stranger sat down."""
    _tick(perception, [MOMO], monkeypatch)
    _tick(perception, [], monkeypatch)
    _tick(perception, [UNSURE], monkeypatch)
    _tick(perception, [STRANGER], monkeypatch)
    _tick(perception, [STRANGER], monkeypatch)

    assert _enters(perception)[-1] == (
        "Person detected — new: stranger (stranger_2); faces in frame: 1 (stranger_2)"
    )
    assert perception.current_user() == "momo"


def test_a_new_friend_lists_a_present_friend(perception, monkeypatch):
    leo = _face(PersonKind.FRIEND, "leo")
    _tick(perception, [MOMO], monkeypatch)
    _tick(perception, [MOMO, leo], monkeypatch)

    assert _enters(perception)[-1] == (
        "Person detected — new: friend (leo); already present: momo (friend); "
        "faces in frame: 2 (momo, leo)"
    )


def test_greeting_describes_the_frame_where_gaze_was_confirmed(perception, monkeypatch):
    """The text describes the newest frame — the one where gaze was confirmed."""
    _tick(perception, [MOMO], monkeypatch)
    _tick(perception, [MOMO, STRANGER], monkeypatch)   # facing 1/1, momo beside
    _tick(perception, [STRANGER], monkeypatch)         # facing 2/2, momo gone

    assert _enters(perception)[-1] == (
        "Person detected — new: stranger (stranger_2); faces in frame: 1 (stranger_2)"
    )


def test_immediate_friend_send_still_describes_the_current_frame(perception, monkeypatch):
    """A new friend sends the CURRENT frame at once, whoever is waiting on gaze."""
    _tick(perception, [MOMO], monkeypatch)
    _tick(perception, [MOMO, STRANGER_AWAY], monkeypatch)  # stranger_2 waiting
    leo = _face(PersonKind.FRIEND, "leo")
    _tick(perception, [MOMO, leo], monkeypatch)

    assert _enters(perception)[-1] == (
        "Person detected — new: friend (leo); already present: momo (friend); "
        "faces in frame: 2 (momo, leo)"
    )


# -- gaze gate (#531) ------------------------------------------------------------


def test_stranger_looking_away_is_never_greeted(perception, monkeypatch):
    for _ in range(6):
        _tick(perception, [STRANGER_AWAY], monkeypatch)

    assert _enters(perception) == []
    # Still seen by everything else: faces in frame, snapshots, current_user.
    faces = perception._perception_state.detected_faces.data.faces
    assert [f.person_id for f in faces] == ["stranger_2"]
    assert perception.current_user() == "unknown"


def test_stranger_greeted_once_when_they_turn_to_the_lamp_later(perception, monkeypatch):
    for _ in range(4):
        _tick(perception, [STRANGER_AWAY], monkeypatch)
    _tick(perception, [STRANGER], monkeypatch)
    _tick(perception, [STRANGER], monkeypatch)
    for _ in range(3):
        _tick(perception, [STRANGER], monkeypatch)

    assert _enters(perception) == [
        "Person detected — new: stranger (stranger_2); faces in frame: 1 (stranger_2)"
    ]


def test_a_single_glance_does_not_greet(perception, monkeypatch):
    for faces in ([STRANGER_AWAY], [STRANGER], [STRANGER_AWAY], [STRANGER_AWAY]):
        _tick(perception, faces, monkeypatch)

    assert _enters(perception) == []


def test_greeting_carries_the_buffered_frames(perception, monkeypatch):
    """Up to FACE_STRANGER_GAZE_TICKS snapshots, the confirming frame last."""
    _tick(perception, [STRANGER_AWAY], monkeypatch)
    _tick(perception, [STRANGER], monkeypatch)
    _tick(perception, [STRANGER], monkeypatch)

    assert len(_enter_images(perception)[-1]) == 3


def test_only_the_stranger_looking_is_greeted(perception, monkeypatch):
    other_away = _face(PersonKind.STRANGER, "stranger_3", PROFILE)
    _tick(perception, [STRANGER, other_away], monkeypatch)
    _tick(perception, [STRANGER, other_away], monkeypatch)

    assert _enters(perception) == [
        "Person detected — new: stranger (stranger_2); "
        "faces in frame: 2 (stranger_2, stranger_3)"
    ]
    assert "stranger_3" in perception._ungreeted_strangers


def test_floored_greeting_is_dropped_like_today(perception, monkeypatch):
    """FACE_STRANGER_ENTER_FLOOR_S unchanged: a floored enter is not re-sent."""
    perception._last_stranger_enter_ts = time.time()
    _tick(perception, [STRANGER], monkeypatch)
    _tick(perception, [STRANGER], monkeypatch)
    perception._last_stranger_enter_ts = 0.0
    _tick(perception, [STRANGER], monkeypatch)
    _tick(perception, [STRANGER], monkeypatch)

    assert _enters(perception) == []


def test_stranger_new_with_a_friend_rides_the_friend_enter_ungated(perception, monkeypatch):
    _tick(perception, [MOMO, STRANGER_AWAY], monkeypatch)
    _tick(perception, [MOMO, STRANGER], monkeypatch)
    _tick(perception, [MOMO, STRANGER], monkeypatch)

    assert _enters(perception) == [
        "Person detected — new: friend (momo), stranger (stranger_2); "
        "faces in frame: 2 (momo, stranger_2)"
    ]


def test_familiar_hint_rides_the_gaze_greeting(perception, monkeypatch):
    monkeypatch.setattr(
        FacePerception, "_track_stranger_visits", lambda self, ids: set(ids)
    )
    _tick(perception, [STRANGER_AWAY], monkeypatch)   # 3rd visit: snapshot saved
    _tick(perception, [STRANGER], monkeypatch)
    _tick(perception, [STRANGER], monkeypatch)

    assert "(familiar stranger stranger_2" in _enters(perception)[-1]


def test_forgotten_stranger_stops_waiting(perception, monkeypatch):
    _tick(perception, [STRANGER_AWAY], monkeypatch)
    perception._people_data_dict["stranger_2"].last_seen = 0.0
    perception._check_leaves(time.time())

    assert perception._ungreeted_strangers == {}
    assert len(perception._stranger_gaze_ticks) == 0


def test_reset_cooldowns_forgets_waiting_strangers(perception, monkeypatch):
    _tick(perception, [STRANGER_AWAY], monkeypatch)
    perception.reset_cooldowns()

    assert perception._ungreeted_strangers == {}
    assert len(perception._stranger_gaze_ticks) == 0
