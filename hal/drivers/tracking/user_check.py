"""One answer to "is this my user?", shared by every path that picks the user (#545).

Two strengths. CONFIRMING a bearing the lamp already holds needs only a face near enough
to be at the desk: the user often talks while looking at their own monitor. ADOPTING a
new bearing also needs that face recognised or facing the lamp: a co-worker's back, or a
side-on face across the office, must never become the user.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import hal.config as config

logger = logging.getLogger(__name__)

STRANGER_PREFIX: str = "stranger_"


@dataclass(frozen=True)
class FaceEvidence:
    """What was seen of one face."""
    # Box height as a fraction of frame height; the largest seen for this face.
    face_h_frac: float
    facing_ratio: float = 0.0
    # Samples that measured a head at all. Only these may vote on facing.
    facing_samples: int = 0
    # Fresh face-ID friend label, or "".
    identity: str = ""


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str


def is_recognised(label: str) -> bool:
    return bool(label) and label != "unknown" and not label.startswith(STRANGER_PREFIX)


def fresh_identity() -> str:
    """The friend face-ID currently places in front of the lamp, or "".

    Face-ID answers "who was here recently", not "whose box is this", so a label only
    counts while it is fresh.
    """
    try:
        import hal.app_state as state

        label, age = state.face_user()
    except Exception as e:
        logger.debug("[user-check] identity lookup skipped: %s", e)
        return ""
    label = label or ""
    if not is_recognised(label):
        return ""
    if float(age or 0.0) > config.GAZE_BEARING_IDENTITY_FRESH_S:
        return ""
    return label


def near_enough(face_h_frac: float) -> bool:
    """The same floor bearing_sampler and look-aim apply to a face."""
    return face_h_frac >= config.LOOK_AIM_MIN_FACE_HEIGHT_FRAC


def confirms_bearing(ev: Optional[FaceEvidence]) -> Verdict:
    """Loose: is somebody at the desk where the bearing points?"""
    if ev is None:
        return Verdict(False, "no face")
    floor = config.LOOK_AIM_MIN_FACE_HEIGHT_FRAC
    if not near_enough(ev.face_h_frac):
        return Verdict(
            False,
            f"face too far (h={ev.face_h_frac * 100:.0f}% < {floor * 100:.0f}%)",
        )
    return Verdict(True, f"near face (h={ev.face_h_frac * 100:.0f}%)")


def adopts_bearing(ev: Optional[FaceEvidence]) -> Verdict:
    """Strict: may this face become the user's bearing?"""
    base = confirms_bearing(ev)
    if not base.ok or ev is None:
        return base
    if is_recognised(ev.identity):
        return Verdict(True, f"recognised {ev.identity}")
    bar = config.GAZE_BEARING_MIN_FACING_RATIO
    if ev.facing_samples < config.GAZE_MIN_SAMPLES:
        return Verdict(
            False,
            f"not recognised, facing unmeasured "
            f"({ev.facing_samples} of {config.GAZE_MIN_SAMPLES} samples)",
        )
    if ev.facing_ratio >= bar:
        return Verdict(True, f"facing {ev.facing_ratio * 100:.0f}%")
    return Verdict(
        False,
        f"not recognised, facing {ev.facing_ratio * 100:.0f}% < {bar * 100:.0f}%",
    )


# One observation spans about GAZE_WINDOW_S (1.5 s): enough samples for a facing vote.
OBSERVE_FRAMES: int = 6
OBSERVE_INTERVAL_S: float = 0.25
# Two boxes in consecutive frames are the same face if their centres are this close
# (fraction of frame width). The camera is still while observing.
TRACK_MATCH_FRAC: float = 0.15


@dataclass
class FaceTrack:
    # Latest box, in the coordinates of the frame `grab` returned.
    box: Tuple[int, int, int, int]
    evidence: FaceEvidence


def _downscale(frame: Any):
    from hal.drivers.tracking import frame_utils

    return frame_utils.downscale(frame)


def _detect_faces(small: Any) -> list:
    from hal.drivers.tracking import aim, detection

    with aim._detector_lock_use:
        return detection.detect_faces_with_landmarks(small)


def observe_faces(grab: Callable[[], Optional[Any]],
                  frames: int = OBSERVE_FRAMES,
                  interval_s: float = OBSERVE_INTERVAL_S,
                  sleep: Callable[[float], None] = time.sleep) -> List[FaceTrack]:
    """Watch the faces in view for a moment. Ranked best first: facing, then size.

    Identity is not per box (face-ID says who was here, not which box), so a fresh
    label is attached to every track. The rules still demand a near face.
    """
    from hal.drivers.tracking import gaze

    tracks: List[Dict[str, Any]] = []
    for i in range(max(1, frames)):
        if i:
            sleep(interval_s)
        frame = grab()
        if frame is None:
            continue
        small, scale = _downscale(frame)
        sh, sw = float(small.shape[0]) or 1.0, float(small.shape[1]) or 1.0
        faces = _detect_faces(small)
        if i == 0 and not faces:
            return []
        for (x, y, w, h), lm in faces:
            cx = x + w / 2.0
            edge = min(1.0, abs(cx - sw / 2.0) / (sw / 2.0))
            yaw = gaze.head_yaw_deg(lm) if gaze.landmarks_in_frame(lm, sw, sh) else None
            measured = yaw is not None and float(h) >= config.GAZE_MIN_FACE_PX
            s = 1.0 / scale if scale else 1.0
            box = (int(x * s), int(y * s), int(w * s), int(h * s))
            match = next((t for t in tracks
                          if abs(t["cx"] - cx) <= TRACK_MATCH_FRAC * sw), None)
            if match is None:
                match = {"cx": cx, "h_frac": 0.0, "n": 0, "facing": 0, "box": box}
                tracks.append(match)
            match["cx"], match["box"] = cx, box
            match["h_frac"] = max(match["h_frac"], float(h) / sh)
            if measured:
                match["n"] += 1
                if gaze.facing_lamp(yaw, float(h), edge):
                    match["facing"] += 1
    identity = fresh_identity()
    out = [
        FaceTrack(t["box"], FaceEvidence(
            face_h_frac=t["h_frac"],
            facing_ratio=(t["facing"] / t["n"]) if t["n"] else 0.0,
            facing_samples=t["n"],
            identity=identity,
        ))
        for t in tracks
    ]
    out.sort(key=lambda t: (t.evidence.facing_ratio, t.evidence.face_h_frac), reverse=True)
    return out


def best_user_face(tracks: List[FaceTrack], log_prefix: str) -> Optional[FaceTrack]:
    """The best-ranked track that may become the user's bearing, or None."""
    for t in tracks:
        v = adopts_bearing(t.evidence)
        if v.ok:
            logger.info("%s user check passed: %s", log_prefix, v.reason)
            return t
        logger.info("%s user check rejected a face: %s", log_prefix, v.reason)
    return None
