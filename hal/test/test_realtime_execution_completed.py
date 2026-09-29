"""Execution KPI observes provider completion, never partial speech or unblock sentinels."""

import asyncio
import queue
import threading
from types import SimpleNamespace

import pytest

from hal import config
from hal.realtime.models import OutputEvent, TextOutput, TurnDoneEvent
from hal.realtime.voice_agent.base import VoiceAgentBase
from hal.realtime.voice_agent.openai_realtime import OpenAIRealtimeAgent
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


class Agent(VoiceAgentBase):
    @property
    def sample_rate(self):
        return 16000

    def _do_connect(self): ...
    def _do_disconnect(self): ...
    def _send_loop(self): ...
    def _recv_loop(self): ...


def test_completion_resets_before_partial_timeout_and_synthetic_end(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_RECV_QUEUE_TIMEOUT_S", 0.001)
    agent = Agent()
    agent._recv_queue.put(TurnDoneEvent(execution_completed=True))
    assert list(agent.receive()) == []
    assert agent.execution_completed
    agent._recv_queue.put(OutputEvent(output=TextOutput(text="Partial answer")))
    assert len(list(agent.receive())) == 1
    assert not agent.execution_completed
    agent._recv_queue.put(TurnDoneEvent())
    assert list(agent.receive()) == []
    assert not agent.execution_completed


def test_stale_done_and_abandoned_generator_never_complete(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_RECV_QUEUE_TIMEOUT_S", 0.001)
    agent = Agent()
    agent.skip_next_turn_done()
    agent._recv_queue.put(TurnDoneEvent(execution_completed=True))
    assert list(agent.receive()) == []
    assert not agent.execution_completed
    agent._recv_queue.put(OutputEvent(output=TextOutput(text="partial")))
    agent._recv_queue.put(TurnDoneEvent(execution_completed=True))
    output = agent.receive()
    next(output)
    output.close()
    assert not agent.execution_completed


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "incomplete", None])
def test_openai_terminal_status_is_not_always_completion(status):
    agent = object.__new__(OpenAIRealtimeAgent)
    agent._recv_queue = queue.Queue()
    agent._turn_done = threading.Event()
    event = SimpleNamespace(type="response.done", response=SimpleNamespace(status=status, usage=None))
    assert agent._sync_receive_turn(iter([event]))
    assert agent._recv_queue.get_nowait().execution_completed is (status == "completed")


@pytest.mark.parametrize("interrupted", [False, True])
def test_gemini_generation_completion_requires_uninterrupted_turn(interrupted):
    agent = object.__new__(GeminiLiveAgent)
    agent._pending_tool_calls = set()
    agent._turn_gen = 0
    message = SimpleNamespace(
        usage_metadata=None, tool_call=None, session_resumption_update=None,
        go_away=None,
        server_content=SimpleNamespace(
            grounding_metadata=None, model_turn=None,
            output_transcription=SimpleNamespace(text="An answer."),
            interrupted=interrupted, turn_complete=False, generation_complete=True,
        ),
    )

    async def receive():
        yield message

    agent._session = SimpleNamespace(receive=receive)
    agent._recv_queue = queue.Queue()
    agent._turn_done = threading.Event()
    asyncio.run(agent._async_receive_turn())
    events = []
    while not agent._recv_queue.empty():
        events.append(agent._recv_queue.get_nowait())
    assert events[-1].execution_completed is (not interrupted)


def test_orchestrator_captures_completion_before_session_recycle(monkeypatch):
    from hal.realtime.orchestrator import RealtimeOrchestrator

    agent = Agent()
    agent._recv_queue.put(TurnDoneEvent(execution_completed=True))
    orchestrator = object.__new__(RealtimeOrchestrator)
    orchestrator._agent = agent
    orchestrator._skip_post_idle_recycle = False
    orchestrator._consecutive_silent = 0
    orchestrator._turns_since_recycle = 0
    orchestrator._idle_reset_pending = False
    monkeypatch.setattr(config, "REALTIME_SESSION_MAX_TURNS", 1)
    orchestrator._force_rebuild = lambda: setattr(orchestrator, "_agent", Agent())
    assert list(orchestrator.stream_output()) == []
    assert orchestrator._agent is not agent
    assert orchestrator.execution_completed
    orchestrator._agent._recv_queue.put(TurnDoneEvent())
    assert list(orchestrator.stream_output()) == []
    assert not orchestrator.execution_completed


@pytest.mark.parametrize("completed", [False, True])
def test_partial_speech_routing_is_unchanged_but_completion_is_propagated(monkeypatch, completed):
    from unittest.mock import Mock
    from hal.drivers.voice._internal import realtime_turn

    monkeypatch.setattr(config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(config, "REALTIME_PROVIDER", "openai")
    monkeypatch.setattr(realtime_turn, "_thinking_cue_start", lambda: None)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", lambda: None)
    monkeypatch.setattr(realtime_turn, "_reply_language_name", lambda: "English")
    monkeypatch.setattr(realtime_turn, "_WaitFiller", Mock())
    monkeypatch.setattr(realtime_turn, "harness_followup_active", lambda: False)
    realtime = Mock(available=True, execution_completed=completed)
    realtime.stream_output.return_value = iter([TextOutput(text="An answer.")])
    result = realtime_turn.run_realtime_turn(
        realtime, Mock(), lambda text: text, "Hello", [object()], 1.0,
        interaction_id="vi-execution",
    )
    assert result.handled
    assert result.execution_completed is completed
