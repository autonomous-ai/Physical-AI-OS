"""[TURN CONTEXT] carries the device location derived from the timezone (#558)."""

from pathlib import Path

import hal.clock as clock
from hal import app_state as hal_app_state
from hal.drivers.voice._internal import realtime_turn


def _setup(tmp_path: Path, monkeypatch, zone: str | None) -> None:
    tzfile = tmp_path / "timezone"
    if zone is not None:
        tzfile.write_text(zone + "\n", encoding="utf-8")
    monkeypatch.setattr(clock, "_TZ_FILE", tzfile)
    monkeypatch.setattr(realtime_turn, "_reply_language_name", lambda: "English")
    monkeypatch.setattr(hal_app_state, "sensing_service", None, raising=False)


def test_turn_context_includes_location_after_time(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "Asia/Ho_Chi_Minh")
    fields = realtime_turn.build_turn_context("Lee").removeprefix("[TURN CONTEXT] ").split(" | ")
    assert fields[0].startswith("Time: ")
    assert fields[1].startswith("Location: Ho Chi Minh (from device timezone")
    assert "unless the user names another place" in fields[1]
    assert fields[2].startswith("Reply language: English")
    assert fields[3].startswith("Current user: Lee")


def test_turn_context_omits_location_for_utc(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "Etc/UTC")
    assert "Location:" not in realtime_turn.build_turn_context("Lee")


def test_turn_context_omits_location_without_timezone_file(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, None)
    ctx = realtime_turn.build_turn_context()
    assert ctx.startswith("[TURN CONTEXT] Time: ")
    assert "Location:" not in ctx
