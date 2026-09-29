"""Regression guard for realtime routing of multi-step work."""
import pytest

from hal.realtime.constants import RESOURCES_DIR
from hal.realtime.orchestrator import DELEGATE_TOOL_DESCRIPTION

PROMPTS = [
    "system_prompt.md",
    "system_prompt_openai.md",
    "system_prompt_gemini.md",
    "system_prompt_pipecat.md",
    "system_prompt_gptlive.md",
]


@pytest.mark.parametrize("name", PROMPTS)
def test_prompt_delegates_research_analysis_and_documents(name):
    text = (RESOURCES_DIR / name).read_text(encoding="utf-8")
    assert "**Research, analysis & documents:**" in text
    assert "brainstorm" in text
    assert "report" in text


@pytest.mark.parametrize("name", ["system_prompt_gemini.md", "system_prompt_gptlive_backend.md"])
def test_search_grounding_bullet_excludes_multi_step_work(name):
    text = (RESOURCES_DIR / name).read_text(encoding="utf-8")
    assert "Single facts only" in text


def test_delegate_tool_description_covers_research_and_documents():
    lowered = DELEGATE_TOOL_DESCRIPTION.lower()
    for word in ("research", "brainstorm", "report"):
        assert word in lowered
