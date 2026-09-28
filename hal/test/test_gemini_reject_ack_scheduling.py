"""Gemini ACKs omit unsupported scheduling, including on NON_BLOCKING models.

The deployed 3.8 extended-thinking backend closes with code 1007 when scheduling
is present. SDK model validation alone does not establish server support.
"""
import asyncio
import threading
from types import SimpleNamespace

from hal.realtime.models import FunctionCallResultInput
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


class _RecordingSession:
    def __init__(self):
        self.tool_responses = []

    async def send_tool_response(self, function_responses):
        self.tool_responses.append(function_responses)


def _agent(model: str) -> GeminiLiveAgent:
    agent = object.__new__(GeminiLiveAgent)
    agent._session = _RecordingSession()
    agent._config = SimpleNamespace(sample_rate=16000, model=model)
    agent._pending_tool_calls = {"c1"}
    agent._gated_audio_frames = 0
    agent._turn_done = threading.Event()
    return agent


def _send(agent, **kw):
    asyncio.run(agent._async_send_input(
        FunctionCallResultInput(call_id="c1", output='{"result": "turn dropped"}', **kw)
    ))
    return agent._session.tool_responses[0][0]


def test_scheduling_omitted_on_extended_thinking():
    agent = _agent("gemini-3.8-live-extended-thinking")
    fr = _send(agent)
    assert "scheduling" not in fr.model_dump(exclude_none=True)
    assert fr.response == {"result": "turn dropped"}
    assert not agent._pending_tool_calls


def test_scheduling_omitted_on_plain_live():
    fr = _send(_agent("gemini-3.8-live"))
    assert "scheduling" not in fr.model_dump(exclude_none=True)


if __name__ == "__main__":
    test_scheduling_omitted_on_extended_thinking()
    test_scheduling_omitted_on_plain_live()
    print("ok")
