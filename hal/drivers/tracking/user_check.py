"""One answer to "is this my user?", shared by every path that picks the user (#545).

Two strengths. CONFIRMING a bearing the lamp already holds needs only a face near enough
to be at the desk: the user often talks while looking at their own monitor. ADOPTING a
new bearing also needs that face recognised or facing the lamp: a co-worker's back, or a
side-on face across the office, must never become the user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

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
