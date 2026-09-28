"""The independent completion check fails closed without executing tools."""
import asyncio

import pytest

from hal.realtime import response_outcome


@pytest.mark.parametrize('reply,expected', [('COMPLETE', True), ('INCOMPLETE', False),
                                         ('CLARIFICATION', True),
                                         ('', None), ('COMPLETE maybe', None),
                                         ('CLARIFICATION maybe', None)])
def test_exact_completion_only(monkeypatch, reply, expected):
    import anthropic
    captured = {}

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @property
        def text_stream(self):
            async def chunks():
                yield reply
            return chunks()

    class Client(Stream):
        def __init__(self, **kwargs):
            self.messages = self

        def stream(self, **kwargs):
            captured.update(kwargs)
            return Stream()

    monkeypatch.setattr(anthropic, 'AsyncAnthropic', Client)
    monkeypatch.setattr(response_outcome.app_config, 'REALTIME_SUMMARIZER_API_KEY', 'test')
    result = asyncio.run(response_outcome.spoken_response_complete(
        'What is two plus two?', 'Four.', grounded=False, timeout=1))
    assert result is expected
    assert 'tools' not in captured
    assert 'Four.' in captured['messages'][0]['content']


def test_missing_credentials_preserves_fallback(monkeypatch):
    monkeypatch.setattr(response_outcome.app_config, 'REALTIME_SUMMARIZER_API_KEY', '')
    assert not asyncio.run(response_outcome.spoken_response_complete(
        'Play music', 'Sure', grounded=False, timeout=1))
