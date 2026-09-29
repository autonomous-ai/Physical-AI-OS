"""Per-device preset overlay: deep-merge a sparse robots/<type>/presets.json onto the base tables in place.

Example: {"emotion": {"listening": {"color": [255, 120, 0]}}, "led_count": 60}
"""
import json
import logging
import os
from typing import Any, Dict

from hal.presets import (
    AIM_PRESETS,
    BUTTON_LED_PRESETS,
    EMOTION_PRESETS,
    SCENE_PRESETS,
    STATUS_LED_PRESETS,
)

logger = logging.getLogger(__name__)

# Lamp reference ring; other rings (e.g. intern-v2's 8) override via presets.json.
DEFAULT_LED_COUNT = 64

# Mutated in place so modules that imported the tables by reference see merged values.
_TABLES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "emotion": EMOTION_PRESETS,
    "scene": SCENE_PRESETS,
    "aim": AIM_PRESETS,
    "status_led": STATUS_LED_PRESETS,
    "button_led": BUTTON_LED_PRESETS,
}


def _merge_table(name: str, base: Dict[str, Dict], override: Any, device_type: str) -> None:
    """Deep-merge ``override`` onto ``base`` in place, one level deep; unknown keys fail loud."""
    if not isinstance(override, dict):
        raise ValueError(
            f"presets.json '{name}' for device '{device_type}' must be an object, "
            f"got {type(override).__name__}"
        )
    for key, fields in override.items():
        if key not in base:
            raise ValueError(
                f"presets.json '{name}.{key}' for device '{device_type}' overrides a "
                f"preset that does not exist; valid {name} keys: {sorted(base)}"
            )
        if not isinstance(fields, dict):
            raise ValueError(
                f"presets.json '{name}.{key}' for device '{device_type}' must be an "
                f"object of fields to override, got {type(fields).__name__}"
            )
        base[key].update(fields)


def apply_device_presets(device_type: str, devices_dir: str) -> int:
    """Overlay the device presets.json onto the base tables; return the LED count.

    Missing file keeps the base; a malformed file or unknown preset fails loud.
    """
    path = os.path.join(devices_dir, device_type, "presets.json")
    if not os.path.exists(path):
        logger.info("[presets] no per-device overrides for '%s' (using base presets)", device_type)
        return DEFAULT_LED_COUNT

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"presets.json for device '{device_type}' must be a JSON object")

    applied = []
    for name, table in _TABLES.items():
        if name in data:
            _merge_table(name, table, data[name], device_type)
            applied.append(name)

    led_count = data.get("led_count", DEFAULT_LED_COUNT)
    if not isinstance(led_count, int) or isinstance(led_count, bool) or led_count <= 0:
        raise ValueError(
            f"presets.json led_count for device '{device_type}' must be a positive "
            f"integer, got {led_count!r}"
        )

    logger.info(
        "[presets] applied per-device overrides for '%s': sections=%s led_count=%d",
        device_type, applied or ["none"], led_count,
    )
    return led_count
