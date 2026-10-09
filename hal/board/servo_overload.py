"""Device-owned servo overload cut-off thresholds; no file or board entry leaves it off."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_FIELDS = {"enabled", "load", "hold_s", "retry_s"}


@dataclass(frozen=True)
class ServoOverloadConfig:
    # Present_Load floor in 0.1 % of full drive (1..1000).
    load: int
    # How long a joint must stay at or above `load` before torque is cut.
    hold_s: float
    # Lockout before the servos are re-enabled.
    retry_s: float

    def __post_init__(self):
        if type(self.load) is not int or not 1 <= self.load <= 1000:
            raise ValueError("load must be an integer 1..1000 (0.1 % of full drive)")
        for name in ("hold_s", "retry_s"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not value > 0:
                raise ValueError(f"{name} must be a positive number")
            object.__setattr__(self, name, float(value))


def load_servo_overload_config(device_dir: str, board_id: str) -> Optional[ServoOverloadConfig]:
    """Load servo_overload.json's `boards` map for one board; None means no cut-off."""
    path = Path(device_dir) / "servo_overload.json"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != {"boards"} or not isinstance(data["boards"], dict):
            raise ValueError("expected an object containing a 'boards' map")
        configs = {}
        for board, entry in data["boards"].items():
            if not isinstance(entry, dict) or set(entry) - _FIELDS:
                raise ValueError(f"{board}: invalid servo overload fields")
            enabled = entry.get("enabled", True)
            if type(enabled) is not bool:
                raise ValueError(f"{board}: enabled must be a boolean")
            if not enabled:
                configs[board] = None
                continue
            missing = {"load", "hold_s", "retry_s"} - set(entry)
            if missing:
                raise ValueError(f"{board}: missing {sorted(missing)}")
            configs[board] = ServoOverloadConfig(entry["load"], entry["hold_s"], entry["retry_s"])
        return configs.get(board_id)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid servo overload config at {path}: {exc}") from exc
