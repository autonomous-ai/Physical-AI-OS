"""Remember roughly where the user is, as a single servo yaw."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import hal.config as config

logger = logging.getLogger(__name__)

SCHEMA_VERSION: int = 3
BASE_YAW_JOINT: str = "base_yaw.pos"


def _calibration_path() -> Optional[str]:
    """The calibration file the arm actually loaded, or None.

    Deriving it a second time from DEVICE_ID and the two candidate directories means two
    copies of a rule that can drift, and a mirror that drifts would fingerprint a file
    the arm never loaded.
    """
    try:
        import hal.app_state as state

        svc = getattr(state, "animation_service", None)
        fpath = getattr(getattr(svc, "robot", None), "calibration_fpath", None)
        if fpath:
            return str(fpath)
    except Exception:
        pass
    try:
        from hal.config import DEVICE_ID
        from hal.follower.config_hal_follower import (
            CALIBRATION_DIR,
            PERSISTENT_CALIBRATION_DIR,
        )
    except Exception:
        return None
    try:
        if DEVICE_ID and DEVICE_ID != "hal":
            per_device = PERSISTENT_CALIBRATION_DIR / f"{DEVICE_ID}.json"
            if per_device.is_file():
                return str(per_device)
        return str(CALIBRATION_DIR / "hal.json")
    except Exception:
        return None


def _calibration_fingerprint() -> Optional[str]:
    """Short content hash of the live calibration, or None if unreadable."""
    path = _calibration_path()
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return None

EMA_ALPHA: float = 0.25
# Ignore sightings closer together than this: someone sitting still for an hour
# must not drown out every other position they use.
MIN_SAMPLE_INTERVAL_S: float = 30.0
OUTLIER_DEG: float = 45.0
OUTLIER_ALPHA: float = 0.05
OUTLIER_STREAK: int = 3
# Below this confidence the estimate is not settled enough to call anything an
# outlier — early sightings must be free to move it.
OUTLIER_MIN_CONFIDENCE: float = 0.4
PREDICTION_MISS_LIMIT: int = 3
# ...and they must be CLUSTERED.
MISS_STREAK_WINDOW_S: float = 24 * 3600.0
CONFIDENCE_FULL_SAMPLES: int = 8


@dataclass
class BearingEstimate:
    bearing_deg: float
    confidence: float
    samples: int
    updated: float
    age_s: float
    # Full remembered posture, {joint: degrees}.
    pose: Dict[str, float] = field(default_factory=dict)


def _path() -> str:
    return getattr(config, "USER_BEARING_PATH", "/var/lib/hal/user_bearing.json")


def _load_raw() -> Optional[dict]:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    version = d.get("version")
    if version in (1, 2):
        # Every stored angle is in DEGREES ON A SPECIFIC CALIBRATION, and neither of
        # these schemas recorded which.
        logger.info(
            "[user-bearing] dropping a v%s estimate — it predates calibration "
            "tracking, so its angles cannot be trusted on this arm", version,
        )
        return None
    if version != SCHEMA_VERSION:
        return None
    stored = d.get("calibration")
    live = _calibration_fingerprint()
    if live is None:
        logger.debug("[user-bearing] calibration unreadable — accepting stored estimate")
        return d
    if stored != live:
        logger.info(
            "[user-bearing] calibration changed (%s -> %s) — dropping the stored "
            "pose; every angle in it describes the old arm",
            stored, live,
        )
        return None
    return d


def _write_raw(d: dict) -> bool:
    """Atomic write."""
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception as e:
        logger.debug("[user-bearing] write failed: %s", e)
        return False


def _confidence(samples: int) -> float:
    if samples <= 0:
        return 0.0
    return round(min(1.0, samples / float(CONFIDENCE_FULL_SAMPLES)), 4)


def _blend_pose(
    prev_pose: Dict[str, float], new_pose: Dict[str, float], alpha: float
) -> Dict[str, float]:
    """EMA each joint independently, at the same rate the yaw is smoothed.

    A joint present in only one side is taken as-is rather than dropped: the servo set
    can differ between reads (a joint that failed to report once must not erase what we
    already knew about it).
    """
    out: Dict[str, float] = dict(prev_pose)
    for joint, value in new_pose.items():
        if joint in prev_pose:
            out[joint] = round((1.0 - alpha) * float(prev_pose[joint]) + alpha * float(value), 3)
        else:
            out[joint] = round(float(value), 3)
    return out


def record_sighting(
    yaw_deg: float,
    pose: Optional[Dict[str, float]] = None,
    now: Optional[float] = None,
) -> bool:
    """Fold one CENTRED sighting into the estimate.

    Callers must only pass a yaw whose frame had the subject centred — this function
    cannot tell, and an off-centre yaw would silently bias the estimate by the
    uncorrected offset.
    """
    t = time.time() if now is None else now
    prev = _load_raw()

    new_pose: Dict[str, float] = {
        str(k): float(v) for k, v in (pose or {}).items() if isinstance(v, (int, float))
    }
    prev_pose: Dict[str, float] = (
        {str(k): float(v) for k, v in (prev.get("pose") or {}).items()} if prev else {}
    )

    streak = 0
    if prev:
        last = float(prev.get("updated", 0.0))
        if t - last < MIN_SAMPLE_INTERVAL_S:
            return False
        prev_bearing = float(prev.get("bearing_deg", yaw_deg))
        samples = int(prev.get("samples", 0)) + 1
        settled = _confidence(int(prev.get("samples", 0))) >= OUTLIER_MIN_CONFIDENCE

        if settled and abs(yaw_deg - prev_bearing) > OUTLIER_DEG:
            streak = int(prev.get("outlier_streak", 0)) + 1
            if streak >= OUTLIER_STREAK:
                bearing, samples, streak = yaw_deg, 1, 0
                blended = new_pose
            else:
                # Damp hard: one person crossing the room must not flip the estimate.
                bearing = (1.0 - OUTLIER_ALPHA) * prev_bearing + OUTLIER_ALPHA * yaw_deg
                blended = _blend_pose(prev_pose, new_pose, OUTLIER_ALPHA)
        else:
            bearing = (1.0 - EMA_ALPHA) * prev_bearing + EMA_ALPHA * yaw_deg
            blended = _blend_pose(prev_pose, new_pose, EMA_ALPHA)
    else:
        bearing = yaw_deg
        samples = 1
        blended = new_pose

    blended[BASE_YAW_JOINT] = round(bearing, 3)

    ok = _write_raw({
        "version": SCHEMA_VERSION,
        "calibration": _calibration_fingerprint(),
        "bearing_deg": round(bearing, 3),
        "pose": blended,
        "confidence": _confidence(samples),
        "samples": samples,
        "outlier_streak": streak,
        "updated": t,
    })
    if ok:
        logger.info(
            "[user-bearing] sighting yaw=%+.1f -> estimate %+.1f (n=%d, joints=%d)",
            yaw_deg, bearing, samples, len(blended),
        )
    return ok


def read_estimate(now: Optional[float] = None) -> Optional[BearingEstimate]:
    """Current estimate, or None if never set."""
    d = _load_raw()
    if not d:
        return None
    t = time.time() if now is None else now
    updated = float(d.get("updated", 0.0))
    age = max(0.0, t - updated)
    samples = int(d.get("samples", 0))
    return BearingEstimate(
        bearing_deg=float(d.get("bearing_deg", 0.0)),
        confidence=_confidence(samples),
        samples=samples,
        updated=updated,
        age_s=age,
        pose={str(k): float(v) for k, v in (d.get("pose") or {}).items()},
    )


def record_prediction(hit: bool, now: Optional[float] = None) -> bool:
    """Score one use of the bearing. Returns True if the estimate was dropped."""
    d = _load_raw()
    if not d:
        return False
    if hit:
        if d.get("misses"):
            d["misses"] = 0
            d["last_miss"] = 0.0
            _write_raw(d)
        return False

    t = time.time() if now is None else now
    last_miss = float(d.get("last_miss", 0.0))
    if last_miss > 0.0 and (t - last_miss) > MISS_STREAK_WINDOW_S:
        misses = 1
    else:
        misses = int(d.get("misses", 0)) + 1
    d["last_miss"] = t

    if misses >= PREDICTION_MISS_LIMIT:
        logger.info(
            "[user-bearing] %d failed predictions in a row — dropping estimate "
            "(lamp moved, or the user is no longer where they were)", misses,
        )
        clear()
        return True

    d["misses"] = misses
    _write_raw(d)
    logger.debug("[user-bearing] failed prediction %d/%d", misses, PREDICTION_MISS_LIMIT)
    return False


def clear() -> bool:
    """Forget the estimate — for "I moved you", and for the relocation handling in Task E."""
    try:
        os.unlink(_path())
        logger.info("[user-bearing] estimate cleared")
        return True
    except FileNotFoundError:
        return True
    except Exception as e:
        logger.debug("[user-bearing] clear failed: %s", e)
        return False
