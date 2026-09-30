"""One answer to "is this my user?", shared by every path that picks the user (#545).

Two strengths. CONFIRMING a bearing the lamp already holds needs only a face near enough
to be at the desk: the user often talks while looking at their own monitor. ADOPTING a
new bearing also needs that face near the frame centre and facing the lamp: a
co-worker's back, or a side-on face across the office, must never become the user.

Face-ID is deliberately not a way in. It names who was seen recently, not whose box
this is, so a fresh label would let every near face through while the user is in view.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import hal.config as config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FaceEvidence:
    """What was seen of one face."""
    # Box height as a fraction of frame height; the largest seen for this face.
    face_h_frac: float
    facing_ratio: float = 0.0
    # Samples that measured a head at all. Only these may vote on facing.
    facing_samples: int = 0
    # Latest horizontal offset of the face centre, as a fraction of frame width (0 = centre).
    dx_frac: float = 0.0
    # Per-frame head yaw (None = not measured) and face height fraction, for the log.
    yaws: Tuple[Optional[float], ...] = ()
    heights: Tuple[float, ...] = ()


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str


def near_enough(face_h_frac: float) -> bool:
    return face_h_frac >= config.GAZE_BEARING_MIN_FACE_HEIGHT_FRAC


def faces_lamp(yaw_deg: Optional[float], face_px: float) -> bool:
    """One sample facing the lamp, by the bearing's own limit (no edge widening)."""
    return (
        yaw_deg is not None
        and face_px >= config.GAZE_MIN_FACE_PX
        and yaw_deg <= config.GAZE_BEARING_MAX_YAW_DEG
    )


def _size_verdict(ev: Optional[FaceEvidence]) -> Verdict:
    if ev is None:
        return Verdict(False, "no face")
    floor = config.GAZE_BEARING_MIN_FACE_HEIGHT_FRAC
    if not near_enough(ev.face_h_frac):
        return Verdict(
            False,
            f"face too far (h={ev.face_h_frac * 100:.0f}% < {floor * 100:.0f}%)",
        )
    return Verdict(True, f"near face (h={ev.face_h_frac * 100:.0f}%)")


def confirms_bearing(ev: Optional[FaceEvidence]) -> Verdict:
    """Loose: is somebody at the desk where the bearing points? Size alone decides.

    Neither facing nor position: the user talks while looking at their monitor, and a
    bearing a few degrees off their seat puts them at the frame side (device-observed
    2026-09-30: a +/-15% centre gate ruled the user's 35% face "far").
    """
    return _size_verdict(ev)


def adopts_bearing(ev: Optional[FaceEvidence]) -> Verdict:
    """Strict: may this face become the user's bearing?"""
    base = _size_verdict(ev)
    if not base.ok or ev is None:
        return base
    # Wider than the repoint's gate: the sweep's looks overlap only at +/-25%.
    max_dx = config.BEARING_SAMPLE_MAX_DX_FRAC
    if abs(ev.dx_frac) > max_dx:
        return Verdict(
            False,
            f"too far off centre (dx={ev.dx_frac * 100:+.0f}%, max {max_dx * 100:.0f}%)",
        )
    bar = config.GAZE_BEARING_MIN_FACING_RATIO
    if ev.facing_samples < config.GAZE_MIN_SAMPLES:
        return Verdict(
            False,
            f"facing unmeasured ({ev.facing_samples} of {config.GAZE_MIN_SAMPLES} samples)",
        )
    if ev.facing_ratio >= bar:
        return Verdict(True, f"facing {ev.facing_ratio * 100:.0f}%")
    return Verdict(False, f"facing {ev.facing_ratio * 100:.0f}% < {bar * 100:.0f}%")


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
    """Watch the faces in view for a moment. Ranked best first: facing, then size."""
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
            yaw = gaze.head_yaw_deg(lm) if gaze.landmarks_in_frame(lm, sw, sh) else None
            s = 1.0 / scale if scale else 1.0
            box = (int(x * s), int(y * s), int(w * s), int(h * s))
            match = next((t for t in tracks
                          if abs(t["cx"] - cx) <= TRACK_MATCH_FRAC * sw), None)
            if match is None:
                match = {"cx": cx, "h_frac": 0.0, "n": 0, "facing": 0, "box": box,
                         "yaws": [], "heights": []}
                tracks.append(match)
            match["cx"], match["box"] = cx, box
            match["dx"] = (cx - sw / 2.0) / sw
            match["h_frac"] = max(match["h_frac"], float(h) / sh)
            match["yaws"].append(yaw)
            match["heights"].append(float(h) / sh)
            if yaw is not None and float(h) >= config.GAZE_MIN_FACE_PX:
                match["n"] += 1
                if faces_lamp(yaw, float(h)):
                    match["facing"] += 1
    out = [
        FaceTrack(t["box"], FaceEvidence(
            face_h_frac=t["h_frac"],
            facing_ratio=(t["facing"] / t["n"]) if t["n"] else 0.0,
            facing_samples=t["n"],
            dx_frac=t["dx"],
            yaws=tuple(t["yaws"]),
            heights=tuple(t["heights"]),
        ))
        for t in tracks
    ]
    out.sort(key=lambda t: (t.evidence.facing_ratio, t.evidence.face_h_frac), reverse=True)
    return out


def _trail(ev: FaceEvidence) -> str:
    """Per-frame numbers behind a verdict, so thresholds are tuned from data."""
    yaws = ",".join("-" if y is None else f"{y:.0f}" for y in ev.yaws)
    hs = ",".join(f"{h * 100:.0f}" for h in ev.heights)
    return f"yaw=[{yaws}] h=[{hs}]% dx={ev.dx_frac * 100:+.0f}%"


def best_user_face(tracks: List[FaceTrack], log_prefix: str) -> Optional[FaceTrack]:
    """The best-ranked track that may become the user's bearing, or None."""
    for t in tracks:
        v = adopts_bearing(t.evidence)
        if v.ok:
            logger.info("%s user check passed: %s — %s", log_prefix, v.reason, _trail(t.evidence))
            return t
        logger.info("%s user check rejected a face: %s — %s", log_prefix, v.reason,
                    _trail(t.evidence))
    return None
