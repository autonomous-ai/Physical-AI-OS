"""Whether a stranger is looking at the lamp — the gate on their greeting (#531).

The per-face rule is gaze wake's (hal/drivers/tracking/gaze.py): yaw from the
five keypoints, the GAZE_MAX_YAW_DEG cone widened toward the frame edge, the
GAZE_MIN_FACE_PX floor, and landmarks outside the frame never vote. Only the
vote differs: face ID ticks every 2 s, so it counts ticks, and only ticks
younger than FACE_STRANGER_GAZE_WINDOW_S.
"""

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


def face_facing_lamp(face: Face, frame_w: int, frame_h: int) -> bool:
    """Whether this face, on this tick, counts as looking at the lamp."""
    if face.kps is None or frame_w <= 0 or frame_h <= 0:
        return False
    if not gaze.landmarks_in_frame(face.kps, frame_w, frame_h):
        return False
    # GAZE_MIN_FACE_PX is in the gaze watcher's downscaled pixels
    # (VISION_MAX_WIDTH wide); face ID sees the full frame.
    max_w = tracking_constants.VISION_MAX_WIDTH
    scale = min(1.0, max_w / frame_w) if max_w else 1.0
    x1, y1, x2, y2 = face.bbox
    face_px = max(0, y2 - y1) * scale
    edge = min(1.0, abs((x1 + x2) / 2.0 - frame_w / 2.0) / (frame_w / 2.0))
    return gaze.facing_lamp(gaze.head_yaw_deg(face.kps), face_px, edge)


def gaze_confirmed(ticks: Iterable[frozenset[str]], stranger_id: str) -> bool:
    """True once the stranger faced the lamp on enough of the buffered ticks."""
    facing = sum(1 for t in ticks if stranger_id in t)
    return facing >= config.FACE_STRANGER_GAZE_MIN_FACING
