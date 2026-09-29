"""Recorded moves are speed-gated before reaching the Pollen daemon (#286)."""
import math
import os
import sys
import types
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _install_sdk_stub():
    """Register a minimal fake `reachy_mini` package before importing the driver."""
    if "reachy_mini" in sys.modules:
        return
    pkg = types.ModuleType("reachy_mini")
    pkg.ReachyMini = object
    utils = types.ModuleType("reachy_mini.utils")
    utils.create_head_pose = lambda **kwargs: None
    motion = types.ModuleType("reachy_mini.motion")
    recorded = types.ModuleType("reachy_mini.motion.recorded_move")
    recorded.RecordedMoves = object
    sys.modules.update({
        "reachy_mini": pkg,
        "reachy_mini.utils": utils,
        "reachy_mini.motion": motion,
        "reachy_mini.motion.recorded_move": recorded,
    })


_install_sdk_stub()

import numpy as np  # noqa: E402

from hal.drivers.motors.reachy_service import (  # noqa: E402
    ReachyMotionService,
    peak_head_dps,
)
from hal.safety.policy import MotionBounds, SafetyPolicy  # noqa: E402


def _yaw_frame(deg):
    """A head pose rotated `deg` about z, in the SDK's 4x4 form."""
    r = math.radians(deg)
    m = np.eye(4)
    m[:3, :3] = [[math.cos(r), -math.sin(r), 0.0],
                 [math.sin(r), math.cos(r), 0.0],
                 [0.0, 0.0, 1.0]]
    return {"head": m.tolist(), "antennas": [0.0, 0.0], "body_yaw": 0.0}


class FakeMove:
    def __init__(self, times, frames):
        self.timestamps = times
        self.trajectory = frames


def _policy(max_speed):
    return SafetyPolicy(schema="v1",
                        motion=MotionBounds(max_speed=max_speed, stop_always=True))


class TestPeakHeadDps(unittest.TestCase):
    def test_measures_rotation_rate(self):
        # 10 deg in 0.1s = 100 deg/s
        peak = peak_head_dps([0.0, 0.1], [_yaw_frame(0), _yaw_frame(10)])
        self.assertAlmostEqual(peak, 100.0, places=3)

    def test_duplicate_timestamps_do_not_divide_by_zero(self):
        # A zero-length window is no evidence of speed.
        self.assertEqual(peak_head_dps([0.0, 0.0], [_yaw_frame(0), _yaw_frame(1)]), 0.0)

    def test_timestamp_jitter_is_not_a_speed_spike(self):
        # wake-mini-up has 1ms gaps; read pairwise it claims 17226 deg/s.
        times = [0.0, 0.020, 0.021, 0.040]
        frames = [_yaw_frame(0), _yaw_frame(2), _yaw_frame(2.1), _yaw_frame(4)]
        self.assertLess(peak_head_dps(times, frames), 150.0)

    def test_gimbal_crossing_is_not_a_speed_spike(self):
        # Euler xyz wraps +179 to -179; the true rotation is 2 deg.
        peak = peak_head_dps([0.0, 1.0], [_yaw_frame(179), _yaw_frame(-179)])
        self.assertAlmostEqual(peak, 2.0, places=3)

    def test_translation_is_ignored(self):
        # head_x/y/z are mm; a pure slide is not a rotation speed.
        a, b = _yaw_frame(0), _yaw_frame(0)
        b["head"][0][3] = 0.5
        self.assertEqual(peak_head_dps([0.0, 0.1], [a, b]), 0.0)


class TestMoveRefused(unittest.TestCase):
    def _svc(self, max_speed):
        svc = ReachyMotionService.__new__(ReachyMotionService)
        svc._safety_policy = _policy(max_speed) if max_speed else None
        svc._move_peak_dps = {}
        return svc

    def test_over_ceiling_is_refused(self):
        move = FakeMove([0.0, 0.1], [_yaw_frame(0), _yaw_frame(60)])
        self.assertTrue(self._svc(450)._move_refused(move, "fast1"))

    def test_within_ceiling_plays(self):
        move = FakeMove([0.0, 0.1], [_yaw_frame(0), _yaw_frame(10)])
        self.assertFalse(self._svc(450)._move_refused(move, "calm1"))

    def test_no_declared_ceiling_never_refuses(self):
        move = FakeMove([0.0, 0.1], [_yaw_frame(0), _yaw_frame(60)])
        self.assertFalse(self._svc(None)._move_refused(move, "fast1"))

    def test_unreadable_move_is_not_refused(self):
        class Opaque:
            pass
        self.assertFalse(self._svc(450)._move_refused(Opaque(), "weird1"))

    def test_scan_is_cached_per_name(self):
        svc = self._svc(450)
        move = FakeMove([0.0, 0.1], [_yaw_frame(0), _yaw_frame(10)])
        svc._move_refused(move, "calm1")
        self.assertIn("calm1", svc._move_peak_dps)
        move.timestamps = None
        self.assertFalse(svc._move_refused(move, "calm1"))


if __name__ == "__main__":
    unittest.main()
