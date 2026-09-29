"""Copy the realtime `look` frame where the Flow Monitor can serve it and return a `[snapshot: ...]` marker."""

from __future__ import annotations

import logging
import os
import shutil
import time
from typing import Optional

import hal.config as config

logger = logging.getLogger(__name__)

# Must begin with "sensing_" or the monitor will not build a thumbnail for it.
MONITOR_CATEGORY: str = "sensing_look"
KEEP_LAST: int = 20


def _monitor_dir() -> str:
    root = getattr(config, "SNAPSHOT_PERSIST_DIR", "/var/lib/hal/snapshots")
    return os.path.join(root, MONITOR_CATEGORY)


def _prune(directory: str) -> None:
    try:
        files = sorted(
            (f for f in os.listdir(directory) if f.endswith(".jpg")),
            reverse=True,
        )
        for stale in files[KEEP_LAST:]:
            try:
                os.unlink(os.path.join(directory, stale))
            except OSError:
                pass
    except Exception as e:
        logger.debug("[look-monitor] prune skipped: %s", e)


def persist_for_monitor(src_path: Optional[str]) -> Optional[str]:
    """Copy the look frame somewhere the monitor can serve it; returns the path or None (best-effort)."""
    if not src_path or not os.path.exists(src_path):
        return None
    try:
        directory = _monitor_dir()
        os.makedirs(directory, exist_ok=True)
        dst = os.path.join(directory, f"{int(time.time() * 1000)}.jpg")
        shutil.copyfile(src_path, dst)
        _prune(directory)
        return dst
    except Exception as e:
        logger.debug("[look-monitor] persist failed: %s", e)
        return None


def snapshot_marker(monitor_path: Optional[str]) -> str:
    """The marker to append to a turn message, or "" when there is no frame."""
    return f"[snapshot: {monitor_path}]" if monitor_path else ""
