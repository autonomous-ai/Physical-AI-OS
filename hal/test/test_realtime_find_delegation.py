"""Finding a physical object is a device action, in ANY phrasing."""

import pytest

from hal.realtime import orchestrator
from hal.realtime.constants import RESOURCES_DIR

PROMPTS = (
    "system_prompt.md",
    "system_prompt_gemini.md",
    "system_prompt_openai.md",
    "system_prompt_gptlive.md",
)

QUESTION_SHAPED = (
    "can you help me find my pen",
    "do you see my pen anywhere",
    "where is my cup",
)


def test_delegate_description_names_finding_as_a_device_action():
    text = orchestrator.DELEGATE_TOOL_DESCRIPTION
    assert "Finding, locating or looking for a physical object or a person" in text
    for phrase in QUESTION_SHAPED:
        assert phrase in text, phrase
    assert "never a conversation" in text
    assert "questions about what it looks like" in text
    assert "offers to look" in text
    assert "claims about what you can see" in text


def test_look_description_excludes_finding():
    text = orchestrator.LOOK_TOOL_DESCRIPTION
    assert "Do NOT use it to find or locate" in text
    assert "delegate_to_main" in text


@pytest.mark.parametrize("name", PROMPTS)
def test_every_prompt_carries_the_finding_bullet(name):
    text = (RESOURCES_DIR / name).read_text(encoding="utf-8")
    assert "**Finding things is an action" in text, name
    assert "can you help me find my pen" in text, name
    assert "still an action" in text, name


@pytest.mark.parametrize("name", PROMPTS)
def test_every_prompt_has_a_find_example(name):
    text = (RESOURCES_DIR / name).read_text(encoding="utf-8")
    assert 'User: "Can you help me find my pen?"' in text, name
    assert 'delegate_to_main(message="Can you help me find my pen?")' in text, name
