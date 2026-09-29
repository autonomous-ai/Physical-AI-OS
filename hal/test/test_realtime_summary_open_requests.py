"""The realtime summary's pending tasks must expire (issues #419 / #421)."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from hal.realtime.context_manager.base import OPEN_REQUESTS_HEADING, expire_open_requests
from hal.realtime.context_manager.openclaw import OpenClawContextManager

TTL = 3600
NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()

# Well inside the TTL; one offset-aware, one naive (treated as UTC).
FRESH_A = "2026-09-15T11:56:02+00:00"
FRESH_B = "2026-09-15T11:58"
# Two hours before NOW — past the TTL.
STALE_A = "2026-09-15T09:56:02+00:00"
STALE_B = "2026-09-15T09:58"


def summary_with(*stamps: str) -> str:
    bullets = "".join(f"- [{s}] request {i}\n" for i, s in enumerate(stamps))
    return (
        "## Facts\n"
        "- The user prefers Vietnamese\n"
        "\n"
        f"{OPEN_REQUESTS_HEADING}\n"
        f"{bullets}"
        "\n"
        "## Mood\n"
        "- Calm\n"
    )


def test_fresh_summary_is_untouched():
    # The file itself is ancient: the stamps, not the mtime, decide.
    fresh = summary_with(FRESH_A, FRESH_B)
    assert expire_open_requests(fresh, now_s=NOW, file_age_s=10**6, ttl_s=TTL) == fresh


def test_stale_summary_drops_only_the_open_requests_section():
    # Just rewritten (file_age_s=0): the TTL must not key on mtime alone.
    out = expire_open_requests(summary_with(STALE_A, STALE_B), now_s=NOW, file_age_s=0, ttl_s=TTL)
    assert "request 0" not in out
    assert "request 1" not in out
    assert OPEN_REQUESTS_HEADING not in out
    assert "- The user prefers Vietnamese" in out
    assert "## Mood\n- Calm" in out


def test_only_expired_bullets_are_dropped():
    out = expire_open_requests(summary_with(STALE_A, FRESH_B), now_s=NOW, file_age_s=0, ttl_s=TTL)
    assert "request 0" not in out
    assert f"- [{FRESH_B}] request 1" in out
    assert OPEN_REQUESTS_HEADING in out
    assert "## Mood\n- Calm" in out


def test_unstamped_bullet_falls_back_to_file_age():
    unstamped = summary_with("t")
    assert expire_open_requests(unstamped, now_s=NOW, file_age_s=TTL - 1, ttl_s=TTL) == unstamped
    out = expire_open_requests(unstamped, now_s=NOW, file_age_s=TTL, ttl_s=TTL)
    assert "request 0" not in out
    assert OPEN_REQUESTS_HEADING not in out


def test_zero_ttl_disables_expiry():
    stale = summary_with(STALE_A, STALE_B)
    assert expire_open_requests(stale, now_s=NOW, file_age_s=10**9, ttl_s=0) == stale


def test_summary_without_the_section_is_untouched_when_stale():
    plain = "## Facts\n- The user prefers Vietnamese\n"
    assert expire_open_requests(plain, now_s=NOW, file_age_s=10**6, ttl_s=TTL) == plain


def test_section_at_end_of_summary_is_dropped():
    tail = "## Facts\n- x\n\n" + OPEN_REQUESTS_HEADING + f"\n- [{STALE_A}] pending thing\n"
    assert expire_open_requests(tail, now_s=NOW, file_age_s=0, ttl_s=TTL) == "## Facts\n- x"


def test_refeed_fails_open_when_summary_vanishes_between_read_and_stat():
    """A concurrently deleted summary.md does not raise."""
    manager = object.__new__(OpenClawContextManager)
    fake_path = MagicMock(spec=Path)
    fake_path.read_text.return_value = summary_with(FRESH_A)
    fake_path.stat.side_effect = FileNotFoundError("summary.md vanished")
    manager._summary_path = fake_path
    manager._open_request_ttl_s = TTL

    assert manager._read_summary_for_refeed() == ""
