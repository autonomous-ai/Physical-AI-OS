"""summary.md must fit its cap without losing the activity or the newest content (#449).

Device-observed 2026-09-28: every summary came back 5.3-6.0k chars against a
5,000 cap and was hard-cut — dropping the newest debate rounds.
"""

import hal.config as app_config
from hal.realtime.constants import RESOURCES_DIR
from hal.realtime.context_manager.base import (
    CURRENT_ACTIVITY_HEADING,
    OPEN_REQUESTS_HEADING,
    fit_summary,
)
from hal.realtime.summarizer import RealtimeSummarizer

ACTIVITY = (
    f"{CURRENT_ACTIVITY_HEADING}\n"
    "- Debate: remote work vs office; user argues remote, count rounds out loud\n"
    "- Now: round 20 done\n"
)
HISTORY = "## History\n" + "".join(f"- old fact {i:02d} " + "x" * 40 + "\n" for i in range(40))
OPEN = f"{OPEN_REQUESTS_HEADING}\n- [2026-09-28T10:00:00+00:00] remind about water\n"


def test_summary_under_cap_is_untouched():
    text = ACTIVITY + "## History\n- a\n"
    assert fit_summary(text, 5000) == text


def test_over_cap_drops_oldest_history_bullets_first():
    text = ACTIVITY + HISTORY + OPEN
    out = fit_summary(text, 900)
    assert len(out) <= 900
    assert ACTIVITY.strip() in out
    assert OPEN.strip() in out
    assert "old fact 00" not in out          # oldest gone
    assert "old fact 39" in out              # newest history kept


def test_summary_without_activity_section_still_fits():
    out = fit_summary(HISTORY, 500)
    assert len(out) <= 500
    assert "old fact 39" in out


def test_protected_sections_alone_over_cap_hard_cut_as_last_resort():
    huge = f"{CURRENT_ACTIVITY_HEADING}\n" + "".join(f"- round {i}\n" for i in range(500))
    out = fit_summary(huge, 300)
    assert len(out) <= 300
    assert out.startswith(CURRENT_ACTIVITY_HEADING)


def test_prompt_states_the_real_budget_and_the_activity_section():
    prompt = (RESOURCES_DIR / "summarize_prompt.md").read_text(encoding="utf-8")
    assert "{max_chars}" in prompt
    assert "2000 words" not in prompt
    assert f"`{CURRENT_ACTIVITY_HEADING}`" in prompt

    system = RealtimeSummarizer(api_key="test")._system_prompt
    assert "{max_chars}" not in system
    assert str(app_config.REALTIME_SUMMARY_MAX_CHARS) in system
