"""Where the arm was standing when it could actually see a face."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, Optional

import hal.config as config

logger = logging.getLogger(__name__)

SCHEMA_VERSION: int = 1

HEIGHT_JOINTS = ("base_pitch.pos", "elbow_pitch.pos", "wrist_pitch.pos")


def _path() -> str:
    return getattr(config, "FACE_HEIGHT_PATH", "/var/lib/hal/face_height.json")


def record(pose: Optional[Dict[str, float]]) -> bool:
    """Remember a pose a face was actually framed from. Never raises."""
    if not isinstance(pose, dict) or not pose:
        return False
    clean = {
        j: float(v) for j, v in pose.items()
        if isinstance(v, (int, float)) and j.endswith(".pos")
    }
    if not any(j in clean for j in HEIGHT_JOINTS):
        return False
    try:
        path = _path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"version": SCHEMA_VERSION, "recorded_at": time.time(), "pose": clean},
                f,
            )
        # Atomic: a half-written height read back as a pose would aim the camera
        # at whatever the truncated numbers happened to mean.
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.debug("[face-height] could not record: %s", e)
        return False


def read() -> Optional[Dict[str, float]]:
    """The remembered pose, or None. Never raises."""
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.debug("[face-height] could not read: %s", e)
        return None
    if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
        return None
    pose = data.get("pose")
    if not isinstance(pose, dict) or not pose:
        return None
    return {
        j: float(v) for j, v in pose.items()
        if isinstance(v, (int, float))
    }


def height_target(current: Dict[str, float]) -> Optional[Dict[str, float]]:
    """The remembered pose reduced to the joints a restore may move."""
    pose = read()
    if not pose:
        return None
    target = {j: pose[j] for j in HEIGHT_JOINTS if j in pose}
    if not target:
        return None
    if all(abs(target[j] - float(current.get(j, target[j]))) < 1.0 for j in target):
        return None
    return target
