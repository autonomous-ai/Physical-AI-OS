"""Device-local wall clock that reads /etc/timezone on every call (runtime tz changes apply without restart)."""

from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TZ_FILE = Path("/etc/timezone")


def device_timezone() -> Optional[ZoneInfo]:
    """The device zone from /etc/timezone, or None (caller falls back to naive local time)."""
    try:
        name = _TZ_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def device_now() -> datetime:
    """Current time in the device timezone (naive local time if unresolved)."""
    return datetime.now(device_timezone())


def device_fromtimestamp(ts: float) -> datetime:
    """Convert an epoch timestamp to the device timezone (naive local time if unresolved)."""
    return datetime.fromtimestamp(ts, device_timezone())
