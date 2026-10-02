"""Device-local wall clock and zone city; both read /etc/timezone on every call (runtime tz changes apply without restart)."""

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


# IANA areas whose last segment names a city; legacy links (US/, Etc/) don't.
_CITY_AREAS = frozenset({
    "Africa", "America", "Antarctica", "Arctic", "Asia",
    "Atlantic", "Australia", "Europe", "Indian", "Pacific",
})


def device_city() -> str:
    """City of the device zone ("Asia/Ho_Chi_Minh" → "Ho Chi Minh"), or "" when the zone names none."""
    tz = device_timezone()
    if tz is None:
        return ""
    parts = tz.key.split("/")
    if len(parts) < 2 or parts[0] not in _CITY_AREAS:
        return ""
    return parts[-1].replace("_", " ")


def device_now() -> datetime:
    """Current time in the device timezone (naive local time if unresolved)."""
    return datetime.now(device_timezone())


def device_fromtimestamp(ts: float) -> datetime:
    """Convert an epoch timestamp to the device timezone (naive local time if unresolved)."""
    return datetime.fromtimestamp(ts, device_timezone())
