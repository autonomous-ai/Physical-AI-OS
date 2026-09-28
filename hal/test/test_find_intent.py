"""#481: find/search requests must be recognised deterministically, not by the model."""
import pytest

from hal.realtime.find_intent import is_find_request


@pytest.mark.parametrize("text", [
    "Find my mouse.",
    "Find my mouse. Yeah. Do that one.",
    "Can you help me find my pen?",
    "Where is my cup?",
    "where's my phone",
    "Where’s the remote?",
    "Do you see my keys?",
    "can you see my glasses anywhere",
    "Look for my phone",
    "look around for the charger",
    "Locate my wallet",
    "Search for my headphones",
    "Tìm con chuột giúp mình",
    "Cái bút của mình ở đâu?",
])
def test_find_requests_match(text):
    assert is_find_request(text)


@pytest.mark.parametrize("text", [
    "Look what I am holding!",
    "What am I holding?",
    "Look at this",
    "What is this?",
    "What color is this?",
    "Read this label",
    "What's in front of you?",
    "Look what I found!",
    "",
])
def test_visual_questions_do_not_match(text):
    assert not is_find_request(text)
