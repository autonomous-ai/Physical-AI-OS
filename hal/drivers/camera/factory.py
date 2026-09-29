"""Camera driver factory — resolve a ROBOT.md `driver:` name to a device class."""
from __future__ import annotations

import importlib
import logging
from typing import Optional, Tuple

logger = logging.getLogger("hal.camera.factory")

CAMERA_DRIVERS: dict[str, Tuple[str, str]] = {
    "opencv": ("hal.drivers.camera.video_capture_device", "LocalVideoCaptureDevice"),
    "rpicam": ("hal.drivers.camera.rpicam_capture_device", "RpicamVideoCaptureDevice"),
    "virtual": ("hal.drivers.camera.virtual_capture_device", "VirtualVideoCaptureDevice"),
    # Simulation only (HAL_SIM_MEDIA=host): the developer machine's webcam via
    # the platform's native OpenCV backend. Never selected by a ROBOT.md
    # `driver:` on real hardware.
    "host": ("hal.drivers.camera.host_capture_device", "HostVideoCaptureDevice"),
}


def resolve_camera_class(driver: Optional[str], required: bool) -> Optional[type]:
    """Resolve a camera driver name to its capture device class.

    Args:
        driver: the `driver:` value from ROBOT.md vision capability, or None.
        required: whether the vision capability is declared required.

    Returns:
        The capture device class, or None when the driver is unavailable/unknown
        and the capability is optional. Raises on unknown + required.
    """
    if driver is None:
        driver = "opencv"

    entry = CAMERA_DRIVERS.get(driver)
    if entry is None:
        registered = sorted(CAMERA_DRIVERS.keys())
        if required:
            raise RuntimeError(
                f"ROBOT.md declares camera driver '{driver}' but no capture "
                f"device is registered for it (registered: {registered}). "
                f"Implement it and register in hal/drivers/camera/factory.py, "
                f"or fix the driver name."
            )
        logger.warning(
            "[camera] driver '%s' is not registered (registered: %s); "
            "vision capability is optional, skipping",
            driver, registered,
        )
        return None

    module_path, class_name = entry
    try:
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name)
    except (ImportError, AttributeError) as e:
        logger.warning("[camera] driver '%s' registered but import failed: %s", driver, e)
        return None
