"""Realtime must not report the outcome of work only the main agent does (#564)."""

from hal.realtime.orchestrator import DELEGATE_TOOL_DESCRIPTION


def test_delegate_rule_forbids_claiming_main_task_completion():
    text = DELEGATE_TOOL_DESCRIPTION.lower()
    assert "never say a task the main agent is handling is done" in text
    assert "enrollment" in text


def test_enrollment_requests_always_delegate_even_for_a_known_user():
    # 2026-10-05 green-lamp: "Please remember my face." was answered by realtime
    # ("I already have your face remembered, Long") instead of reaching face-enroll.
    text = DELEGATE_TOOL_DESCRIPTION.lower()
    assert "remember my face" in text
    assert "even when the user seems already known" in text
