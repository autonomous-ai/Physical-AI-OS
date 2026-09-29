"""Whether a stranger is looking at the lamp — the gate on their greeting (#531).

The per-face rule is gaze wake's (hal/drivers/tracking/gaze.py): yaw from the
five keypoints, the GAZE_MAX_YAW_DEG cone widened toward the frame edge, the
GAZE_MIN_FACE_PX floor, and landmarks outside the frame never vote. Only the
vote differs: face ID ticks every 2 s, so it counts ticks, and only ticks
younger than FACE_STRANGER_GAZE_WINDOW_S.
"""

import math
from collections.abc import Iterable
from typing import NamedTuple

import cv2

import hal.config as config
from hal.drivers.sensing.perceptions.models import Face
from hal.drivers.tracking import constants as tracking_constants
from hal.drivers.tracking import gaze


class StrangerGazeTick(NamedTuple):
    """One face-ID tick with an ungreeted stranger in frame."""

    frame: cv2.typing.MatLike  # annotated snapshot
    facing: frozenset[str]  # ungreeted stranger ids facing the lamp on it
    ts: float  # when the tick was seen; older than FACE_STRANGER_GAZE_WINDOW_S no longer votes


class GazeMeasurement(NamedTuple):
    """One face on one tick: the verdict and the numbers behind it (#537)."""

    facing: bool
    yaw: float | None  # head yaw, degrees; None when it cannot be measured
    face_px: float  # face height in the gaze watcher's pixels (GAZE_MIN_FACE_PX's unit)
    min_px: float  # the size floor face_px was held to: GAZE_MIN_FACE_PX
    edge: float  # 0 = frame centre, 1 = frame edge
    cone: float  # the yaw this face is allowed: gaze.cone_for(edge)
    reason: str | None  # why yaw is None, so unmeasurable never reads as "turned away"

    def describe(self) -> str:
        """The per-face part of the [face] stranger gaze log line.

        Each tested number sits against its limit with the comparison that
        gaze.facing_lamp applies, and an away vote names the failed checks in
        words, so the log shows which test decided the vote.
        """
        small = self.face_px < self.min_px
        # Rounded down: 47.6 px must not print as 48 next to a floor of 48.
        size = f"face={math.floor(self.face_px)}px{'<' if small else '>='}{self.min_px:.0f}"
        if self.yaw is None:
            # The size and cone checks never ran; only the first failure applies.
            return f"yaw=- {size} edge={self.edge:.2f} -> away ({self.reason})"
        wide = self.yaw > self.cone
        # Rounding keeps order, so only a failing yaw can print equal to its
        # cone; add decimals until the printed pair shows the gap.
        for digits in range(1, 7):
            yaw_txt, cone_txt = f"{self.yaw:.{digits}f}", f"{self.cone:.{digits}f}"
            if not wide or float(yaw_txt) > float(cone_txt):
                break
        yaw = f"yaw={yaw_txt}{'>' if wide else '<='}{cone_txt}"
        line = f"{yaw} {size} edge={self.edge:.2f} -> {'facing' if self.facing else 'away'}"
        failed = [r for r, hit in (("face too small", small), ("turned too far", wide)) if hit]
        return f"{line} ({', '.join(failed)})" if failed else line


def measure_gaze(face: Face, frame_w: int, frame_h: int) -> GazeMeasurement:
    """Whether this face, on this tick, counts as looking at the lamp, and why."""
    min_px = float(config.GAZE_MIN_FACE_PX)
    if frame_w <= 0 or frame_h <= 0:
        return GazeMeasurement(False, None, 0.0, min_px, 0.0, gaze.cone_for(0.0), "no frame")
    # GAZE_MIN_FACE_PX is in the gaze watcher's downscaled pixels
    # (VISION_MAX_WIDTH wide); face ID sees the full frame.
    max_w = tracking_constants.VISION_MAX_WIDTH
    scale = min(1.0, max_w / frame_w) if max_w else 1.0
    x1, y1, x2, y2 = face.bbox
    face_px = max(0, y2 - y1) * scale
    edge = min(1.0, abs((x1 + x2) / 2.0 - frame_w / 2.0) / (frame_w / 2.0))
    cone = gaze.cone_for(edge)
    if face.kps is None:
        return GazeMeasurement(False, None, face_px, min_px, edge, cone, "no keypoints")
    if not gaze.landmarks_in_frame(face.kps, frame_w, frame_h):
        return GazeMeasurement(False, None, face_px, min_px, edge, cone, "landmarks off-frame")
    yaw = gaze.head_yaw_deg(face.kps)
    if yaw is None:
        return GazeMeasurement(False, None, face_px, min_px, edge, cone, "no yaw")
    facing = gaze.facing_lamp(yaw, face_px, edge)
    return GazeMeasurement(facing, yaw, face_px, min_px, edge, cone, None)


def gaze_confirmed(ticks: Iterable[frozenset[str]], stranger_id: str) -> bool:
    """True once the stranger faced the lamp on enough of the buffered ticks."""
    facing = sum(1 for t in ticks if stranger_id in t)
    return facing >= config.FACE_STRANGER_GAZE_MIN_FACING
