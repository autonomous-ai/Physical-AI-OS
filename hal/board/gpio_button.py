"""Load mechanical button wiring owned by a device, keyed by board ID."""

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re

from hal.board.board import ButtonConfig, PROFILES


@dataclass(frozen=True)
class ButtonInputConfig:
    wiring: ButtonConfig
    name: str = "primary"
    behavior: str = "standard"
    hold_s: float = 5.0
    # False keeps a 10s+ hold at shutdown (device has a dedicated reset button).
    factory_reset: bool = True


def _wiring(values):
    fields = {key: values[key] for key in ("chip", "line", "debounce_ns")}
    if any(type(value) is not int or value < 0 for value in fields.values()):
        raise ValueError("wiring values must be non-negative integers")
    return ButtonConfig(**fields)


def _board_buttons(values):
    if not isinstance(values, dict):
        raise ValueError("expected a board object")
    if "buttons" not in values:
        if set(values) != {"chip", "line", "debounce_ns"}:
            raise ValueError("expected chip, line and debounce_ns")
        return [ButtonInputConfig(_wiring(values))]
    if set(values) != {"buttons"} or not isinstance(values["buttons"], list) or not values["buttons"]:
        raise ValueError("expected a non-empty buttons list")
    result, names, pins = [], set(), set()
    required = {"name", "chip", "line", "debounce_ns"}
    for entry in values["buttons"]:
        if not isinstance(entry, dict) or not required <= set(entry) or set(entry) - required - {"behavior", "hold_s", "factory_reset"}:
            raise ValueError("button requires name, chip, line, debounce_ns and optional behavior/hold_s/factory_reset")
        name = entry["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("button name must contain only letters, digits, '_' or '-'")
        behavior = entry.get("behavior", "standard")
        if behavior not in ("standard", "factory_reset"):
            raise ValueError("behavior must be standard or factory_reset")
        if behavior == "standard" and "hold_s" in entry:
            raise ValueError("hold_s is only valid for factory_reset buttons")
        factory_reset = entry.get("factory_reset", True)
        if type(factory_reset) is not bool:
            raise ValueError("factory_reset must be a boolean")
        if behavior == "factory_reset" and "factory_reset" in entry:
            raise ValueError("factory_reset flag is only valid for standard buttons")
        hold_s = entry.get("hold_s", 5.0)
        if type(hold_s) not in (int, float) or not math.isfinite(hold_s) or hold_s <= 0:
            raise ValueError("hold_s must be finite and positive")
        wiring = _wiring(entry)
        pin = (wiring.chip, wiring.line)
        if name in names or pin in pins:
            raise ValueError("duplicate button name or chip/line")
        names.add(name)
        pins.add(pin)
        result.append(ButtonInputConfig(wiring, name, behavior, float(hold_s), factory_reset))
    return result


def load_button_configs(device_dir: str, board_id: str) -> list[ButtonInputConfig]:
    """Load one or more inputs; absent file/board keeps one legacy button.

    GPIO lines are chip-relative offsets (pull-up, active-low).
    """
    fallback = [ButtonInputConfig(PROFILES[board_id].button)]
    path = Path(device_dir) / "gpio_button.json"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return fallback
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != {"boards"} or not isinstance(data["boards"], dict):
            raise ValueError("expected an object containing a boards map")
        configs = {name: _board_buttons(values) for name, values in data["boards"].items()}
        return configs.get(board_id, fallback)
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"Invalid GPIO button wiring at {path}: {exc}") from exc


def load_button_config(device_dir: str, board_id: str) -> ButtonConfig:
    """Compatibility accessor for callers that only need the first button."""
    return load_button_configs(device_dir, board_id)[0].wiring
