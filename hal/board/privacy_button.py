"""Device-owned microphone slide-switch wiring with the legacy Intern fallback."""

from dataclasses import dataclass, fields
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class PrivacyButtonConfig:
    chip: int = 0
    line: int = 97
    settle_s: float = 0.06
    muted_level: int = 0
    watchdog_s: float = 30.0
    disable_camera_on_mute: bool = False
    mute_speaker_on_mute: bool = False


def load_privacy_button_config(device_dir: str, board_id: str,
                           device_type: str) -> PrivacyButtonConfig | None:
    """Load the privacy button config; missing declarations keep the legacy Intern wiring."""
    fallback = PrivacyButtonConfig() if device_type == "intern-v2" else None
    path = Path(device_dir) / "privacy_button.json"
    try:
        text = path.read_text()
    except FileNotFoundError:
        # Accept the old filename while HAL and device files roll out separately.
        path = Path(device_dir) / "mic_button.json"
        try:
            text = path.read_text()
        except FileNotFoundError:
            return fallback
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != {"boards"} or not isinstance(data["boards"], dict):
            raise ValueError("expected an object containing a boards map")
        configs = {}
        allowed = {field.name for field in fields(PrivacyButtonConfig)} | {"enabled"}
        for board, entry in data["boards"].items():
            if not isinstance(entry, dict) or set(entry) - allowed:
                raise ValueError(f"{board}: invalid mic switch fields")
            values = dict(entry)
            enabled = values.pop("enabled", True)
            if type(enabled) is not bool:
                raise ValueError(f"{board}: enabled must be boolean")
            if not enabled:
                configs[board] = None
                continue
            if not {"chip", "line"} <= set(values):
                raise ValueError(f"{board}: chip and line are required")
            config = PrivacyButtonConfig(**values)
            for name in ("disable_camera_on_mute", "mute_speaker_on_mute"):
                if type(getattr(config, name)) is not bool:
                    raise ValueError(f"{board}: {name} must be boolean")
            for name in ("chip", "line"):
                value = getattr(config, name)
                if type(value) is not int or value < 0:
                    raise ValueError(f"{board}: {name} must be a non-negative integer")
            if type(config.muted_level) is not int or config.muted_level not in (0, 1):
                raise ValueError(f"{board}: muted_level must be 0 or 1")
            for name in ("settle_s", "watchdog_s"):
                value = getattr(config, name)
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError(f"{board}: {name} must be finite and non-negative")
            if config.watchdog_s == 0:
                raise ValueError(f"{board}: watchdog_s must be positive")
            configs[board] = config
        return configs.get(board_id, fallback)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"Invalid mic switch wiring at {path}: {exc}") from exc
