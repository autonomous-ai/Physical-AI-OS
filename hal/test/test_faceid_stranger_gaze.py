"""A stranger is greeted only once they look at the lamp (#531).

The per-face rule is gaze wake's own (cone, size floor, edge widening,
unmeasurable landmarks never vote); the vote counts face-ID ticks.
"""

from hal.drivers.sensing.perceptions.models import Face, PersonKind
from hal.drivers.sensing.perceptions.processors.faceid.stranger_gaze import (
    face_facing_lamp,
    gaze_confirmed,
)

W, H = 640, 480
BOX = [270, 160, 370, 280]  # 120 px tall, centred
# Eyes 40 px apart at y=200, nose centred between them: yaw 0.
FRONTAL = [300.0, 200.0, 340.0, 200.0, 320.0, 225.0, 305.0, 250.0, 335.0, 250.0]
# Nose on the left eye's x: sin(yaw) = 1 -> 90 deg, a profile.
PROFILE = [300.0, 200.0, 340.0, 200.0, 340.0, 225.0, 305.0, 250.0, 335.0, 250.0]


def _stranger(bbox=BOX, kps=FRONTAL) -> Face:
    return Face(bbox=bbox, kind=PersonKind.STRANGER, person_id="stranger_2",
                confidence=0.9, kps=kps)


def test_frontal_face_is_facing():
    assert face_facing_lamp(_stranger(), W, H)


def test_profile_is_not_facing():
    assert not face_facing_lamp(_stranger(kps=PROFILE), W, H)


def test_no_keypoints_is_not_facing():
    assert not face_facing_lamp(_stranger(kps=None), W, H)


def test_small_face_is_not_facing():
    """Below GAZE_MIN_FACE_PX (48) the yaw is rounding error, not a measurement."""
    assert not face_facing_lamp(_stranger(bbox=[300, 180, 340, 220]), W, H)


def test_face_px_is_measured_at_the_gaze_watcher_resolution():
    """GAZE_MIN_FACE_PX is in 640-wide pixels; 80 px at 1280 wide is 40 px there."""
    kps = [v * 2 for v in FRONTAL]
    face = _stranger(bbox=[560, 360, 640, 440], kps=kps)
    assert not face_facing_lamp(face, 1280, 960)


def test_eyes_outside_the_frame_do_not_count():
    """Landmarks extrapolated past the edge are not an observation."""
    kps = list(FRONTAL)
    kps[1] = kps[3] = -3.0  # both eyes above the frame
    assert not face_facing_lamp(_stranger(kps=kps), W, H)


def test_cone_widens_toward_the_frame_edge():
    """~35 deg: outside the 25 deg centre cone, inside the widened edge cone."""
    centred = [300.0, 200.0, 340.0, 200.0, 331.5, 225.0, 305.0, 250.0, 335.0, 250.0]
    assert not face_facing_lamp(_stranger(kps=centred), W, H)
    at_edge = [30.0, 200.0, 70.0, 200.0, 61.5, 225.0, 35.0, 250.0, 65.0, 250.0]
    assert face_facing_lamp(_stranger(bbox=[0, 160, 100, 280], kps=at_edge), W, H)


def test_two_of_three_ticks_confirm():
    s = frozenset({"stranger_2"})
    none = frozenset()
    assert gaze_confirmed([s, none, s], "stranger_2")
    assert not gaze_confirmed([s, none, none], "stranger_2")
    assert not gaze_confirmed([s], "stranger_2")


def test_votes_are_per_stranger():
    both = frozenset({"stranger_2", "stranger_3"})
    only_2 = frozenset({"stranger_2"})
    assert gaze_confirmed([both, only_2], "stranger_2")
    assert not gaze_confirmed([both, only_2], "stranger_3")
