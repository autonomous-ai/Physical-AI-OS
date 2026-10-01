"""Regression tests for Gemini Live generation-complete handling."""

import asyncio
import queue
import threading
from types import SimpleNamespace

from hal.realtime.models import InterruptedOutput, OutputEvent, TextOutput, TextSegmentEndOutput, TurnDoneEvent
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


class _Session:
    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self._messages = messages

    async def receive(self):
        for message in self._messages:
            yield message


def _message_with_generation_complete() -> SimpleNamespace:
    content = SimpleNamespace(
        grounding_metadata=None,
        model_turn=None,
        output_transcription=SimpleNamespace(text="Đã xong."),
        interrupted=False,
        turn_complete=False,
        generation_complete=True,
    )
    return SimpleNamespace(
        usage_metadata=None,
        server_content=content,
        tool_call=None,
        session_resumption_update=None,
        go_away=None,
    )


def test_generation_complete_ends_consumer_turn_without_waiting_for_playback_ack():
    """Do not wait for Gemini's delayed turn_complete after it has generated output."""
    agent = object.__new__(GeminiLiveAgent)
    agent._session = _Session([_message_with_generation_complete()])
    agent._first_audio_received = True
    agent._recv_queue = queue.Queue()
    agent._turn_done = threading.Event()

    asyncio.run(agent._async_receive_turn())

    reset = agent._recv_queue.get_nowait()
    assert isinstance(reset.output, InterruptedOutput)
    assert reset.output.reason == "output_reset"
    output = agent._recv_queue.get_nowait()
    boundary = agent._recv_queue.get_nowait()
    assert isinstance(boundary.output, TextSegmentEndOutput)
    done = agent._recv_queue.get_nowait()
    assert isinstance(output, OutputEvent)
    assert isinstance(output.output, TextOutput)
    assert output.output.text == "Đã xong."
    assert isinstance(done, TurnDoneEvent)
    assert agent._turn_done.is_set()
