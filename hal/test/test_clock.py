"""Tests for hal.clock — device-local time that tracks runtime timezone changes."""

from datetime import datetime, timezone
from pathlib import Path

import hal.clock as clock


def _write_tz(tmp_path: Path, name: str) -> Path:
    p = tmp_path / "timezone"
    p.write_text(name + "\n", encoding="utf-8")
    return p


def test_device_timezone_resolves_valid_zone(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "_TZ_FILE", _write_tz(tmp_path, "Asia/Tokyo"))
    tz = clock.device_timezone()
    assert tz is not None
    assert str(tz) == "Asia/Tokyo"


def test_timezone_change_reflected_without_restart(tmp_path, monkeypatch):
    """The whole point: change /etc/timezone → next call reflects it immediately."""
    tzfile = _write_tz(tmp_path, "Asia/Tokyo")
    monkeypatch.setattr(clock, "_TZ_FILE", tzfile)
    assert str(clock.device_now().tzinfo) == "Asia/Tokyo"

    tzfile.write_text("America/New_York\n", encoding="utf-8")
    assert str(clock.device_now().tzinfo) == "America/New_York"


def test_missing_or_bogus_falls_back_to_naive(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "_TZ_FILE", _write_tz(tmp_path, "Not/AZone"))
    assert clock.device_timezone() is None
    assert clock.device_now().tzinfo is None

    monkeypatch.setattr(clock, "_TZ_FILE", _write_tz(tmp_path, ""))
    assert clock.device_timezone() is None

    monkeypatch.setattr(clock, "_TZ_FILE", tmp_path / "nope")
    assert clock.device_timezone() is None
    assert clock.device_now().tzinfo is None


def test_fromtimestamp_uses_device_zone(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "_TZ_FILE", _write_tz(tmp_path, "Asia/Tokyo"))
    dt = clock.device_fromtimestamp(0)
    assert dt == datetime(1970, 1, 1, 9, 0, tzinfo=dt.tzinfo)
    assert dt.astimezone(timezone.utc).year == 1970
