"""Device declaration, safety and LED presets resolved once per HAL process."""

import os
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from hal.config import SIMULATE, SIM_MEDIA


def _resolve_device_type() -> str:
    dev = os.environ.get("DEVICE_TYPE")
    if dev:
        return dev
    try:
        from hal.config import _os_cfg_get
        cfg = _os_cfg_get("device_type")
    except Exception:
        cfg = None
    if cfg:
        return cfg
    # No "lamp" fallback — refuse to boot the wrong body's drivers/soul/OTA.
    raise RuntimeError(
        "DEVICE_TYPE unresolved: set the DEVICE_TYPE env (provisioning) or "
        "config.json device_type — refusing to assume 'lamp'"
    )


def _devices_dir() -> str:
    # Repository fallback; managed installs set DEVICES_DIR=/opt/devices.
    return os.environ.get("DEVICES_DIR") or os.path.normpath(
        str(Path(__file__).resolve().parents[2] / "robots")
    )


def _device_profile():
    """This device's DeviceProfile; ROBOT.md is required, so a missing one fails loudly."""
    from hal.board.device import load_device
    devices_dir = _devices_dir()
    try:
        return load_device(_resolve_device_type(), devices_dir)
    except Exception as e:
        raise RuntimeError(
            f"ROBOT.md required but not loaded for device '{_resolve_device_type()}' "
            f"(devices_dir={devices_dir}): {e}"
        ) from e


@lru_cache(maxsize=1)
def boot_config():
    from hal.board.board import assert_board_supported
    from hal.board.presets_overlay import apply_device_presets
    from hal.safety.policy import load_safety

    profile = _device_profile()
    if SIM_MEDIA not in {"virtual", "host"}:
        raise RuntimeError("HAL_SIM_MEDIA must be 'virtual' or 'host'")
    if not SIMULATE and os.environ.get("HAL_BOARD") == "sim":
        raise RuntimeError("HAL_BOARD=sim requires HAL_SIMULATE=1")
    board = assert_board_supported([] if SIMULATE else profile.boards)
    device_dir = os.path.join(_devices_dir(), _resolve_device_type())
    safety = load_safety(device_dir, profile.safety_ref)
    led_count = apply_device_presets(_resolve_device_type(), _devices_dir())
    return SimpleNamespace(profile=profile, board=board, safety=safety, led_count=led_count)
