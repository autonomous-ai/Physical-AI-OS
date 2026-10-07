"""Load device-owned TTP223 wiring with legacy board defaults."""

import json
from pathlib import Path
from typing import Optional

from hal.board.board import PROFILES, TouchConfig


def _lines(value, name):
    if (not isinstance(value, list) or not value or len(value) > 64
            or any(type(line) is not int or line < 0 for line in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"{name} must contain 1..64 unique non-negative GPIO line numbers")
    return value


def load_touch_config(device_dir: str, board_id: str) -> Optional[TouchConfig]:
    """Absent file/board uses boards.json; enabled:false explicitly disables touch."""
    fallback = PROFILES[board_id].touch
    path = Path(device_dir) / "ttp223.json"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return fallback
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != {"boards"} or not isinstance(data["boards"], dict):
            raise ValueError("expected an object containing a 'boards' map")
        configs = {}
        for board, entry in data["boards"].items():
            if not isinstance(entry, dict) or set(entry) - {"enabled", "chip", "lines", "axis", "detect"}:
                raise ValueError(f"{board}: invalid TTP223 fields")
            enabled = entry.get("enabled", True)
            if type(enabled) is not bool:
                raise ValueError(f"{board}: enabled must be a boolean")
            if not enabled:
                configs[board] = None
                continue
            chip = entry.get("chip")
            if type(chip) is not int or chip < 0:
                raise ValueError(f"{board}: chip must be a non-negative integer")
            lines = _lines(entry.get("lines"), "lines")
            axis = entry.get("axis")
            if axis is not None and set(_lines(axis, "axis")) != set(lines):
                raise ValueError(f"{board}: axis must be a permutation of lines")
            detect = entry.get("detect", False)
            if type(detect) is not bool:
                raise ValueError(f"{board}: detect must be a boolean")
            configs[board] = TouchConfig(chip=chip, lines=lines, axis=axis, detect=detect)
        return configs.get(board_id, fallback)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid TTP223 wiring at {path}: {exc}") from exc
