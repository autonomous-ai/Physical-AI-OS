"""Shared on-disk paths and constants for the v2 face pipeline."""

from pathlib import Path

import hal.config as config

_NO_MATCH = -2.0

USERS_DIR = Path(config.USERS_DIR)
USERS_DIR.mkdir(parents=True, exist_ok=True)

STRANGER_STATE_DIR = Path(config.STRANGERS_DIR)
STRANGER_STATE_DIR.mkdir(exist_ok=True, parents=True)
_STRANGER_STATS_FILE = USERS_DIR / ".stranger_stats.json"
_STRANGER_SNAPSHOTS_DIR = STRANGER_STATE_DIR / "snapshots"
