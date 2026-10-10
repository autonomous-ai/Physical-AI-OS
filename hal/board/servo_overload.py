"""Device-owned servo overload cut-off thresholds; no file or board entry leaves it off."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Union

_FIELDS = {"enabled", "load", "hold_s", "retry_s", "contact", "torque_limit"}
_CONTACT_FIELDS = {"load", "hold_s", "pause_s"}
_CONTACT_OPTIONAL = {"profile_margin", "lag_margin", "off_playback"}


def _check_load(value) -> None:
    if type(value) is not int or not 1 <= value <= 1000:
        raise ValueError("load must be an integer 1..1000 (0.1 % of full drive)")


def _validate(config, seconds) -> None:
    if isinstance(config.load, dict):
        if not config.load:
            raise ValueError("a per-joint load map needs at least one joint")
        for joint, value in config.load.items():
            if not isinstance(joint, str) or not joint:
                raise ValueError("per-joint load keys must be joint names")
            _check_load(value)
    else:
        _check_load(config.load)
    for name in seconds:
        value = getattr(config, name)
        if type(value) not in (int, float) or not value > 0:
            raise ValueError(f"{name} must be a positive number")
        object.__setattr__(config, name, float(value))


@dataclass(frozen=True)
class ContactStopConfig:
    # Present_Load floor in 0.1 % of full drive (1..1000), one for every joint or a
    # {joint: floor} map (a joint left out is not watched). Set each above that joint's
    # free-motion peak: joints that carry the arm's weight peak far higher than the rest.
    load: Union[int, Dict[str, int]]
    # How long a joint must stay at or above `load` before the arm halts in place.
    hold_s: float
    # How long the arm holds still before it eases back into idle.
    pause_s: float
    # While a recording with a learned load envelope plays, a joint's floor is that
    # envelope plus this margin (0.1 % of full drive). None: fixed floors only.
    profile_margin: Optional[int] = None
    # Same envelope, for how far each joint trails its goal: a joint lagging this
    # much (0.1 deg) beyond its learned lag counts as a hit. Needs profile_margin.
    lag_margin: Optional[int] = None
    # Floors while no recording plays (gaze, tracking, moves, holds): joint names for
    # load (0.1 %), "lag:<joint>" for how far it trails its goal (0.1 deg).
    off_playback: Optional[Dict[str, int]] = None

    def __post_init__(self):
        if self.off_playback is not None:
            if not isinstance(self.off_playback, dict) or not self.off_playback:
                raise ValueError("off_playback must be a non-empty {name: floor} map")
            for name, value in self.off_playback.items():
                if not isinstance(name, str) or not name:
                    raise ValueError("off_playback keys must be joint or lag:<joint> names")
                _check_load(value)
        _validate(self, ("hold_s", "pause_s"))
        for name in ("profile_margin", "lag_margin"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 1 <= value <= 1000):
                raise ValueError(f"{name} must be an integer 1..1000")
        if self.lag_margin is not None and self.profile_margin is None:
            raise ValueError("lag_margin needs profile_margin (the learned envelope)")


@dataclass(frozen=True)
class ServoOverloadConfig:
    # Present_Load floor in 0.1 % of full drive (1..1000).
    load: int
    # How long a joint must stay at or above `load` before torque is cut.
    hold_s: float
    # Lockout before the servos are re-enabled.
    retry_s: float
    # Halt-in-place on a short hit; None leaves it off.
    contact: Optional[ContactStopConfig] = None
    # {joint: Torque_Limit in 0.1 % of full drive}: caps how hard that joint pushes.
    # Joints left out keep the servo's own limit.
    torque_limit: Optional[Dict[str, int]] = None

    def __post_init__(self):
        _check_load(self.load)
        _validate(self, ("hold_s", "retry_s"))
        if self.torque_limit is not None:
            if not isinstance(self.torque_limit, dict) or not self.torque_limit:
                raise ValueError("torque_limit must be a non-empty {joint: limit} map")
            for joint, value in self.torque_limit.items():
                if not isinstance(joint, str) or not joint:
                    raise ValueError("torque_limit keys must be joint names")
                _check_load(value)


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
            contact = entry.get("contact")
            if contact is not None:
                if (
                    not isinstance(contact, dict)
                    or not _CONTACT_FIELDS <= set(contact)
                    or set(contact) - _CONTACT_FIELDS - _CONTACT_OPTIONAL
                ):
                    raise ValueError(
                        f"{board}: contact needs {sorted(_CONTACT_FIELDS)}"
                        f" and may add {sorted(_CONTACT_OPTIONAL)}"
                    )
                contact = ContactStopConfig(
                    contact["load"], contact["hold_s"], contact["pause_s"],
                    contact.get("profile_margin"), contact.get("lag_margin"),
                    contact.get("off_playback"),
                )
            configs[board] = ServoOverloadConfig(
                entry["load"], entry["hold_s"], entry["retry_s"], contact,
                entry.get("torque_limit"),
            )
        return configs.get(board_id)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid servo overload config at {path}: {exc}") from exc
