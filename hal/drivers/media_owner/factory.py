"""Media owner factory — resolve a ROBOT.md `owner:` name to a handover class."""
from __future__ import annotations

import importlib
import logging
from typing import Optional, Tuple

logger = logging.getLogger("hal.media_owner.factory")

MEDIA_OWNERS: dict[str, Tuple[str, str]] = {
    # Pollen Robotics' reachy_mini daemon (Reachy Mini). Holds /dev/video* and
    # both ALSA PCMs for its own app runtime; keeps running, and keeps driving
    # the motors, after handing the media over.
    "pollen_daemon": ("hal.drivers.media_owner.pollen", "PollenDaemonMediaOwner"),
}


def resolve_media_owner(owner: Optional[str]) -> Optional[type]:
    """Resolve an `owner:` name to its handover class.

    Args:
        owner: the `owner:` value from a ROBOT.md capability, or None.

    Returns:
        The handover class, or None when no owner is declared or its module
        could not be imported. Raises on an unknown name.
    """
    if owner is None:
        return None

    entry = MEDIA_OWNERS.get(owner)
    if entry is None:
        registered = sorted(MEDIA_OWNERS.keys())
        raise RuntimeError(
            f"ROBOT.md declares media owner '{owner}' but no handover is "
            f"registered for it (registered: {registered}). Implement it and "
            f"register in hal/drivers/media_owner/factory.py, or fix the name."
        )

    module_path, class_name = entry
    try:
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name)
    except (ImportError, AttributeError) as e:
        logger.warning(
            "[media-owner] '%s' registered but import failed: %s — HAL will open "
            "the hardware without asking, and it will likely report busy",
            owner, e,
        )
        return None
