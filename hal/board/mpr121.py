"""Device-owned MPR121 wiring; absent declarations preserve existing inputs."""

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Optional

# Allowed MPR121 filter values mapped to their register codes (datasheet CONFIG1/CONFIG2).
FFI_CODES = {6: 0, 10: 1, 18: 2, 34: 3}
SFI_CODES = {4: 0, 6: 1, 10: 2, 18: 3}
ESI_CODES = {1: 0, 2: 1, 4: 2, 8: 3, 16: 4, 32: 5, 64: 6, 128: 7}


@dataclass(frozen=True)
class MPR121Config:
    bus: int
    address: int = 0x5A
    electrodes: tuple[int, ...] = tuple(range(12))
    touch_threshold: int = 2
    release_threshold: int = 1
    autoconfig: bool = True
    poll_ms: int = 10
    debounce_ms: int = 30
    # Chip debounce encoding (reg 0x5B), 0..7: N requires N+1 consecutive
    # touch/release detections, independently for both directions.
    chip_debounce: int = 0
    ffi: int = 6
    sfi: int = 10
    esi_ms: int = 1
    swipe_axis: tuple[int, ...] | None = None
    tap_min_electrodes: int = 1

    def __post_init__(self):
        for name in ("bus", "address", "touch_threshold", "release_threshold", "poll_ms", "debounce_ms",
                     "chip_debounce"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be an integer")
        if self.bus < 0:
            raise ValueError("bus must be non-negative")
        if not 0x5A <= self.address <= 0x5D:
            raise ValueError("address must be between 90 (0x5A) and 93 (0x5D)")
        if not 0 <= self.release_threshold < self.touch_threshold <= 255:
            raise ValueError("thresholds must satisfy 0 <= release < touch <= 255")
        if type(self.autoconfig) is not bool:
            raise ValueError("autoconfig must be a boolean")
        if not 1 <= self.poll_ms <= 1000 or not 0 <= self.debounce_ms <= 1000:
            raise ValueError("poll_ms must be 1..1000; debounce_ms must be 0..1000")
        if not 0 <= self.chip_debounce <= 7:
            raise ValueError("chip_debounce must be 0..7")
        for name, codes in (("ffi", FFI_CODES), ("sfi", SFI_CODES), ("esi_ms", ESI_CODES)):
            if type(getattr(self, name)) is not int or getattr(self, name) not in codes:
                raise ValueError(f"{name} must be one of {sorted(codes)}")
        if not isinstance(self.electrodes, (list, tuple)) or not self.electrodes:
            raise ValueError("electrodes must be a non-empty list")
        if any(type(i) is not int or not 0 <= i < 12 for i in self.electrodes):
            raise ValueError("electrodes must contain integers 0..11")
        if len(set(self.electrodes)) != len(self.electrodes):
            raise ValueError("electrodes must not contain duplicates")
        object.__setattr__(self, "electrodes", tuple(self.electrodes))
        if (type(self.tap_min_electrodes) is not int
                or not 1 <= self.tap_min_electrodes <= len(self.electrodes)):
            raise ValueError("tap_min_electrodes must be 1..number of selected electrodes")
        if self.swipe_axis is not None:
            axis = self.swipe_axis
            if (not isinstance(axis, (list, tuple)) or not 2 <= len(axis) <= 12
                    or any(type(i) is not int or i not in self.electrodes for i in axis)
                    or len(set(axis)) != len(axis)):
                raise ValueError("swipe_axis must contain 2..12 distinct selected electrodes in physical left-to-right order")
            object.__setattr__(self, "swipe_axis", tuple(axis))


def load_mpr121_config(device_dir: str, board_id: str) -> Optional[MPR121Config]:
    """Load mpr121.json boards map; no legacy MPR121 wiring is assumed."""
    path = Path(device_dir) / "mpr121.json"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != {"boards"} or not isinstance(data["boards"], dict):
            raise ValueError("expected an object containing a 'boards' map")
        configs = {}
        allowed = {field.name for field in fields(MPR121Config)} | {"enabled"}
        for board, entry in data["boards"].items():
            if not isinstance(entry, dict):
                raise ValueError(f"{board}: invalid MPR121 configuration fields")
            values = dict(entry)
            # Retired setting that older device declarations may still carry.
            values.pop("harness_voice_chord", None)
            if set(values) - allowed:
                raise ValueError(f"{board}: invalid MPR121 configuration fields")
            enabled = values.pop("enabled", True)
            if type(enabled) is not bool:
                raise ValueError(f"{board}: enabled must be a boolean")
            configs[board] = MPR121Config(**values) if enabled else None
        return configs.get(board_id)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid MPR121 wiring at {path}: {exc}") from exc
