"""A stranger is greeted only once they look at the lamp (#531).

The per-face rule is gaze wake's own (cone, size floor, edge widening,
unmeasurable landmarks never vote); the vote counts face-ID ticks.
"""

import pytest

import hal.config as config
from hal.drivers.sensing.perceptions.models import Face, PersonKind
from hal.drivers.sensing.perceptions.processors.faceid.stranger_gaze import (
    GazeMeasurement,
    gaze_confirmed,
    measure_gaze,
)
from hal.drivers.tracking import gaze

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
    assert measure_gaze(_stranger(), W, H).facing


def test_profile_is_not_facing():
    assert not measure_gaze(_stranger(kps=PROFILE), W, H).facing


def test_no_keypoints_is_not_facing():
    assert not measure_gaze(_stranger(kps=None), W, H).facing


def test_small_face_is_not_facing():
    """Below GAZE_MIN_FACE_PX (48) the yaw is rounding error, not a measurement."""
    assert not measure_gaze(_stranger(bbox=[300, 180, 340, 220]), W, H).facing


def test_face_px_is_measured_at_the_gaze_watcher_resolution():
    """GAZE_MIN_FACE_PX is in 640-wide pixels; 80 px at 1280 wide is 40 px there."""
    kps = [v * 2 for v in FRONTAL]
    face = _stranger(bbox=[560, 360, 640, 440], kps=kps)
    m = measure_gaze(face, 1280, 960)
    assert not m.facing
    assert m.face_px == pytest.approx(40.0)


def test_eyes_outside_the_frame_do_not_count():
    """Landmarks extrapolated past the edge are not an observation."""
    kps = list(FRONTAL)
    kps[1] = kps[3] = -3.0  # both eyes above the frame
    assert not measure_gaze(_stranger(kps=kps), W, H).facing


def test_cone_widens_toward_the_frame_edge():
    """~35 deg: outside the 25 deg centre cone, inside the widened edge cone."""
    centred = [300.0, 200.0, 340.0, 200.0, 331.5, 225.0, 305.0, 250.0, 335.0, 250.0]
    assert not measure_gaze(_stranger(kps=centred), W, H).facing
    at_edge = [30.0, 200.0, 70.0, 200.0, 61.5, 225.0, 35.0, 250.0, 65.0, 250.0]
    assert measure_gaze(_stranger(bbox=[0, 160, 100, 280], kps=at_edge), W, H).facing


# -- the measurement behind the verdict (#537) ------------------------------------


def test_frontal_face_measurement():
    m = measure_gaze(_stranger(), W, H)
    assert m.yaw == pytest.approx(0.0)
    assert m.face_px == pytest.approx(120.0)
    assert m.min_px == config.GAZE_MIN_FACE_PX
    assert m.edge == pytest.approx(0.0)
    assert m.cone == pytest.approx(gaze.cone_for(0.0))
    assert m.reason is None


def test_profile_measurement_reads_ninety():
    m = measure_gaze(_stranger(kps=PROFILE), W, H)
    assert m.yaw == pytest.approx(90.0)
    assert m.reason is None


def test_edge_and_cone_follow_the_box_centre():
    """Box centred at x=50 in a 640-wide frame: |50 - 320| / 320."""
    at_edge = [30.0, 200.0, 70.0, 200.0, 61.5, 225.0, 35.0, 250.0, 65.0, 250.0]
    m = measure_gaze(_stranger(bbox=[0, 160, 100, 280], kps=at_edge), W, H)
    assert m.edge == pytest.approx(270.0 / 320.0)
    assert m.cone == pytest.approx(gaze.cone_for(270.0 / 320.0))


def test_no_keypoints_still_measures_size_and_edge():
    """Size and edge come from the box, so 'too far' and 'clipped' stay distinguishable."""
    m = measure_gaze(_stranger(kps=None), W, H)
    assert m.yaw is None
    assert m.reason == "no keypoints"
    assert m.face_px == pytest.approx(120.0)
    assert m.edge == pytest.approx(0.0)


def test_off_frame_landmarks_give_a_reason():
    kps = list(FRONTAL)
    kps[1] = kps[3] = -3.0
    m = measure_gaze(_stranger(kps=kps), W, H)
    assert m.yaw is None
    assert m.reason == "landmarks off-frame"


def test_coincident_eyes_give_a_reason():
    """head_yaw_deg returns None when the eyes sit on one point."""
    kps = [320.0, 200.0, 320.0, 200.0, 320.0, 225.0, 305.0, 250.0, 335.0, 250.0]
    m = measure_gaze(_stranger(kps=kps), W, H)
    assert not m.facing
    assert m.yaw is None
    assert m.reason == "no yaw"


def test_empty_frame_is_not_measurable():
    m = measure_gaze(_stranger(), 0, 0)
    assert not m.facing
    assert m.yaw is None
    assert m.reason == "no frame"


def _m(**kw) -> GazeMeasurement:
    base = dict(facing=True, yaw=51.6, face_px=117.0, min_px=48.0, edge=0.68,
                cone=92.9, reason=None)
    base.update(kw)
    return GazeMeasurement(**base)


def test_describe_facing_has_no_reason():
    assert _m().describe() == "yaw=51.6<=92.9 face=117px>=48 edge=0.68 -> facing"


def test_describe_yaw_outside_the_cone():
    m = _m(facing=False, yaw=70.2, edge=0.1, cone=60.0)
    assert m.describe() == "yaw=70.2>60.0 face=117px>=48 edge=0.10 -> away (turned too far)"


def test_describe_face_below_the_size_floor():
    m = _m(facing=False, yaw=5.0, face_px=40.0, edge=0.05, cone=60.0)
    assert m.describe() == "yaw=5.0<=60.0 face=40px<48 edge=0.05 -> away (face too small)"


def test_describe_lists_both_failures():
    m = _m(facing=False, yaw=75.0, face_px=40.0, edge=0.05, cone=60.0)
    assert m.describe() == (
        "yaw=75.0>60.0 face=40px<48 edge=0.05 -> away (face too small, turned too far)"
    )


def test_describe_yaw_on_the_cone_passes():
    """facing_lamp passes at yaw == cone, so the symbol must be <=."""
    m = _m(yaw=60.0, edge=0.0, cone=60.0)
    assert m.describe().startswith("yaw=60.0<=60.0 ")


def test_describe_unmeasurable_face():
    m = _m(facing=False, yaw=None, face_px=120.0, edge=0.1, cone=60.0,
           reason="landmarks off-frame")
    assert m.describe() == "yaw=- face=120px>=48 edge=0.10 -> away (landmarks off-frame)"


def test_describe_unmeasurable_small_face_names_only_the_first_failure():
    """Yaw was never measured, so the size and cone checks never ran."""
    m = _m(facing=False, yaw=None, face_px=40.0, edge=0.1, cone=60.0,
           reason="no keypoints")
    assert m.describe() == "yaw=- face=40px<48 edge=0.10 -> away (no keypoints)"


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
