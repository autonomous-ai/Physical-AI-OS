"""Realtime must not report the outcome of work only the main agent does (#564)."""

from hal.realtime.orchestrator import DELEGATE_TOOL_DESCRIPTION


def test_delegate_rule_forbids_claiming_main_task_completion():
    text = DELEGATE_TOOL_DESCRIPTION.lower()
    assert "never say a task the main agent is handling is done" in text
    assert "enrollment" in text
