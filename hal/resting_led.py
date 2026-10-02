"""Owner-chosen resting LED look layered over the device default.

The device preset (`ambient_led.resting` in presets.json) is the default. The
owner can turn the resting light off or pick a custom solid colour from the web
UI; that choice is saved under /var/lib/hal and re-applied on boot. The choice
rewrites AMBIENT_RESTING_LED in place, so every existing restore path keeps
working unchanged.
"""
import copy
import json
import logging
import os
import threading
from typing import Optional

import hal.config as config
from hal.presets import AMBIENT_RESTING_LED, LST_SOLID

logger = logging.getLogger("hal.resting_led")

MODE_DEFAULT = "default"
MODE_OFF = "off"
MODE_CUSTOM = "custom"
MODES = (MODE_DEFAULT, MODE_OFF, MODE_CUSTOM)

_lock = threading.Lock()
# Snapshot of the device preset, taken once after presets.json is applied.
_device_default: Optional[dict] = None
_choice: dict = {"mode": MODE_DEFAULT}


def _valid_color(color) -> list[int]:
    if not isinstance(color, (list, tuple)) or len(color) != 3:
        raise ValueError("color must be [r, g, b]")
    out = []
    for c in color:
        if isinstance(c, bool) or not isinstance(c, int) or not 0 <= c <= 255:
            raise ValueError("color channels must be integers 0-255")
        out.append(c)
    return out


def _normalize(mode: str, color=None) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {list(MODES)}")
    if mode != MODE_CUSTOM:
        return {"mode": mode}
    rgb = _valid_color(color)
    # A black custom colour is the same as turning the resting light off.
    if not any(rgb):
        return {"mode": MODE_OFF}
    return {"mode": MODE_CUSTOM, "color": rgb}


def _load() -> dict:
    try:
        with open(config.RESTING_LED_PATH) as f:
            data = json.load(f)
        return _normalize(data.get("mode", MODE_DEFAULT), data.get("color"))
    except FileNotFoundError:
        return {"mode": MODE_DEFAULT}
    except Exception as e:
        logger.warning("Resting LED choice unreadable, using device default: %s", e)
        return {"mode": MODE_DEFAULT}


def _save(choice: dict) -> None:
    path = config.RESTING_LED_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(choice, f)
            # Root is mounted commit=600 and devices are unplugged to power off.
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception as e:
        # The choice still applies until HAL restarts.
        logger.warning("Resting LED choice not saved: %s", e)


def _look(choice: dict) -> dict:
    if choice["mode"] == MODE_OFF:
        return {"effect": LST_SOLID, "color": [0, 0, 0]}
    if choice["mode"] == MODE_CUSTOM:
        return {"effect": LST_SOLID, "color": list(choice["color"])}
    return copy.deepcopy(_device_default)


def _apply_locked() -> None:
    look = _look(_choice)
    AMBIENT_RESTING_LED.clear()
    AMBIENT_RESTING_LED.update(look)


def init() -> None:
    """Snapshot the device preset and re-apply the saved choice; call after presets load."""
    global _device_default, _choice
    with _lock:
        _device_default = copy.deepcopy(AMBIENT_RESTING_LED)
        _choice = _load()
        _apply_locked()
    if _choice["mode"] != MODE_DEFAULT:
        logger.info("Resting LED: owner choice %s", _choice)


def snapshot() -> dict:
    """The owner's choice, the device default and the look in effect."""
    with _lock:
        default = copy.deepcopy(_device_default if _device_default is not None
                                else AMBIENT_RESTING_LED)
        return {
            "mode": _choice["mode"],
            "color": list(_choice["color"]) if "color" in _choice else None,
            "default": default,
            "effective": copy.deepcopy(AMBIENT_RESTING_LED),
        }


def set_choice(mode: str, color=None) -> dict:
    """Validate, save and apply a new choice; raises ValueError on bad input."""
    global _device_default, _choice
    choice = _normalize(mode, color)
    with _lock:
        if _device_default is None:
            _device_default = copy.deepcopy(AMBIENT_RESTING_LED)
        _choice = choice
        _apply_locked()
        _save(choice)
    logger.info("Resting LED: owner set %s", choice)
    return snapshot()
