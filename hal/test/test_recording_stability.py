"""Recordings may not reach far enough off the base axis to tip the body (#271)."""
import csv
from pathlib import Path

import pytest

from hal.drivers.motors.recording_stability import (
    check_stable,
    cog_offset_mm,
    load_geometry,
    parse_urdf,
    worst_frame,
)
from hal.safety.policy import MotionBounds, SafetyPolicy, parse_safety

HAL_DIR = Path(__file__).parents[1]
LAMP_DIR = HAL_DIR.parent / "robots" / "lamp"
RECORDINGS_DIR = HAL_DIR / "recordings"

GEOMETRY = load_geometry(str(LAMP_DIR), "urdf/lamp.urdf")
REST = {j: 0.0 for j in GEOMETRY.joints}


def _policy(ceiling):
    return SafetyPolicy(schema="v1", motion=MotionBounds(max_cog_offset_mm=ceiling))


def _frames(path):
    with path.open(newline="") as source:
        return [
            {k: float(v) for k, v in row.items() if k != "timestamp"}
            for row in csv.DictReader(source)
        ]


def test_lamp_declares_both_halves_of_the_gate():
    """Either half alone is inert; the committed profile must carry both."""
    profile = (LAMP_DIR / "ROBOT.md").read_text()
    assert "urdf_ref:" in profile
    policy = parse_safety((LAMP_DIR / "SAFETY.md").read_text())
    assert policy.motion.max_cog_offset_mm == 22


def test_the_committed_urdf_parses_into_the_joints_hal_drives():
    assert GEOMETRY is not None
    assert GEOMETRY.joints == {
        "base_yaw", "base_pitch", "elbow_pitch", "wrist_pitch", "wrist_roll",
    }


def test_a_branching_urdf_is_refused_rather_than_guessed():
    """Two children on one link is not a chain this walk can reduce."""
    forked = """<robot name="forked">
      <link name="base"><inertial><mass value="1"/></inertial></link>
      <link name="a"><inertial><mass value="1"/></inertial></link>
      <link name="b"><inertial><mass value="1"/></inertial></link>
      <joint name="ja" type="revolute">
        <origin xyz="0 0 1"/><parent link="base"/><child link="a"/><axis xyz="0 0 1"/>
      </joint>
      <joint name="jb" type="revolute">
        <origin xyz="0 0 1"/><parent link="base"/><child link="b"/><axis xyz="0 0 1"/>
      </joint>
    </robot>"""
    assert parse_urdf(forked) is None


def test_rest_pose_sits_near_the_axis():
    assert cog_offset_mm(REST, GEOMETRY) < 20.0


def test_extending_the_arm_moves_the_cog_out():
    # -base_pitch and +elbow_pitch both push mass forward and partly cancel, so probe one at a time.
    assert cog_offset_mm({**REST, "base_pitch": -60.0}, GEOMETRY) > cog_offset_mm(REST, GEOMETRY)
    assert cog_offset_mm({**REST, "elbow_pitch": 60.0}, GEOMETRY) > cog_offset_mm(REST, GEOMETRY)


def test_yaw_alone_does_not_move_the_cog():
    """base_yaw spins the arm about the very axis the offset is measured from."""
    assert cog_offset_mm({**REST, "base_yaw": 90.0}, GEOMETRY) == pytest.approx(
        cog_offset_mm(REST, GEOMETRY)
    )


def test_pos_suffix_is_accepted():
    assert cog_offset_mm({"base_pitch.pos": -60.0}, GEOMETRY) == pytest.approx(
        cog_offset_mm({**REST, "base_pitch": -60.0}, GEOMETRY)
    )


def test_every_shipped_recording_passes_the_declared_ceiling():
    """The ceiling must not reject the library it was derived from."""
    policy = parse_safety((LAMP_DIR / "SAFETY.md").read_text())
    for recording in sorted(RECORDINGS_DIR.glob("*.csv")):
        _, worst, _ = worst_frame(_frames(recording), GEOMETRY)
        assert worst <= policy.motion.max_cog_offset_mm, (
            f"{recording.name} peaks at {worst:.1f} mm, over the declared ceiling"
        )


def test_the_pose_that_tipped_the_lamp_is_refused():
    """From the animacy clip that put lamp-0c89 on its side (#271)."""
    tipping = {
        "base_yaw.pos": -7.3,
        "base_pitch.pos": 6.9,
        "elbow_pitch.pos": 53.8,
        "wrist_roll.pos": -0.1,
        "wrist_pitch.pos": -13.9,
    }
    assert cog_offset_mm(tipping, GEOMETRY) == pytest.approx(31.6, abs=0.1)
    with pytest.raises(ValueError, match="tip-over ceiling"):
        check_stable([tipping], "tipping", _policy(22), GEOMETRY)


def test_refusal_is_logged_with_the_offending_pose(caplog):
    """An operator has to be able to explain a refusal from the journal alone."""
    tipping = {**REST, "base_pitch": 6.9, "elbow_pitch": 53.8}
    with caplog.at_level("ERROR", logger="hal.motion.stability"):
        with pytest.raises(ValueError):
            check_stable([REST, tipping], "tipping", _policy(22), GEOMETRY)
    assert "REFUSED" in caplog.text
    assert "frame 1/2" in caplog.text
    assert "elbow_pitch=53.8" in caplog.text


def test_a_clip_near_the_limit_warns_while_still_playing(caplog):
    """Creeping up on the ceiling should show before the day it crosses."""
    near = {**REST, "elbow_pitch": 20.0}
    assert 22 * 0.85 <= cog_offset_mm(near, GEOMETRY) <= 22
    with caplog.at_level("WARNING", logger="hal.motion.stability"):
        check_stable([near], "near", _policy(22), GEOMETRY)
    assert "close to the limit" in caplog.text


def test_no_declared_ceiling_is_pass_through():
    """The engine never invents a limit nobody declared."""
    check_stable([{**REST, "elbow_pitch": 89.0}], "ungated", _policy(None), GEOMETRY)
    check_stable([{**REST, "elbow_pitch": 89.0}], "no_policy", None, GEOMETRY)


def test_a_ceiling_without_geometry_warns_and_passes_through(caplog):
    """Declared but unscoreable must not fail closed — the body still has to move."""
    with caplog.at_level("WARNING", logger="hal.motion.stability"):
        check_stable([{**REST, "elbow_pitch": 89.0}], "no_urdf", _policy(22), None)
    assert "no usable urdf_ref" in caplog.text


def test_another_bodys_joints_are_skipped_not_scored(caplog):
    """A Reachy frame must not be scored against the lamp's chain."""
    reachy = {"head_yaw.pos": 40.0, "head_pitch.pos": -30.0, "antenna_left.pos": 60.0}
    with caplog.at_level("INFO", logger="hal.motion.stability"):
        check_stable([reachy], "reachy_clip", _policy(22), GEOMETRY)
    assert "skipped" in caplog.text
    assert "stable" not in caplog.text
