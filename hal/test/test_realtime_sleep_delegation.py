"""Prompt contract coverage; these checks do not evaluate live model routing."""
from pathlib import Path

import pytest

RESOURCES = Path(__file__).resolve().parents[1] / 'realtime' / 'resources'


@pytest.mark.parametrize('path', sorted(RESOURCES.glob('system_prompt*.md')), ids=lambda p: p.name)
def test_sleep_requests_override_identity_chat_for_every_provider(path):
    text = path.read_text()
    rule = next(line for line in text.splitlines() if '**Device sleep/wake requests' in line)
    for example in ('Can you sleep?', 'Go to sleep', 'Wake up', 'Do robots need sleep?', "I can't sleep", 'Can we sleep now?',
                    'I mean, can you sleep now?', 'I mean, can we sleep now?', 'Ngủ đi'):
        assert example in rule
    assert 'no spoken output' in rule
    assert 'overrides the direct-answer default' in rule
    assert 'Do not call `complete_response`' in rule
    assert "I don't go to sleep like humans do" in rule
    assert "I'll lower my light" in rule
    assert 'sleep keyword' in rule
    assert 'Harness voice mode' in rule
    assert 'not a command to put the device to sleep' in rule
    if path.name == 'system_prompt_gptlive.md':
        assert 'native backend handoff' in rule
    else:
        assert '`delegate_to_main`' in rule
