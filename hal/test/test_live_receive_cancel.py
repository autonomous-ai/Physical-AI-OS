"""An ended live consumer releases the queue before the next session reads it."""

import queue
import threading
import time

import pytest

from hal import config
from hal.realtime.models import OutputEvent, TextOutput, TurnDoneEvent
from hal.realtime.orchestrator import RealtimeOrchestrator
from hal.realtime.voice_agent.base import VoiceAgentBase


class Agent(VoiceAgentBase):
    @property
    def sample_rate(self):
        return 16000

    def _do_connect(self): ...
    def _do_disconnect(self): ...
    def _send_loop(self): ...
    def _recv_loop(self): ...


class ObservedQueue(queue.Queue):
    def __init__(self):
        super().__init__()
        self.waiting = threading.Event()

    def get(self, *args, **kwargs):
        self.waiting.set()
        return super().get(*args, **kwargs)


def test_cancel_idle_reader_before_next_session_consumes_output(monkeypatch):
    monkeypatch.setattr(config, 'REALTIME_RECV_QUEUE_TIMEOUT_S', 8.0)
    agent = Agent()
    agent._recv_queue = ObservedQueue()
    agent._recv_timeout_override_s = 5.0
    stop = threading.Event()
    outputs = []
    reader = threading.Thread(target=lambda: outputs.extend(agent.receive(stop_event=stop)), daemon=True)
    reader.start()
    assert agent._recv_queue.waiting.wait(1)
    started = time.monotonic()
    stop.set()
    reader.join(timeout=1)
    assert not reader.is_alive()
    assert time.monotonic() - started < 0.5
    assert outputs == []
    assert not agent.execution_completed
    assert agent._recv_timeout_override_s == 5.0

    agent._recv_queue.put(OutputEvent(output=TextOutput(text='new session')))
    agent._recv_queue.put(TurnDoneEvent(execution_completed=True))
    assert [out.text for out in agent.receive()] == ['new session']
    assert agent.execution_completed


@pytest.mark.parametrize('progress', [False, True])
def test_cancel_polling_preserves_gap_timeout_and_progress(monkeypatch, progress):
    monkeypatch.setattr(config, 'REALTIME_RECV_QUEUE_TIMEOUT_S', 0.24)
    agent = Agent()
    started = time.monotonic()
    if progress:
        agent.allow_progress_until(started + 0.38)
    assert list(agent.receive(stop_event=threading.Event())) == []
    elapsed = time.monotonic() - started
    assert elapsed >= (0.38 if progress else 0.24)
    assert elapsed < 1.0
    assert agent._progress_deadline_at == 0.0


def test_orchestrator_cancel_does_not_count_silent_or_completed_turn(monkeypatch):
    monkeypatch.setattr(config, 'REALTIME_RECV_QUEUE_TIMEOUT_S', 8.0)
    agent = Agent()
    agent._recv_queue = ObservedQueue()
    orchestrator = object.__new__(RealtimeOrchestrator)
    orchestrator._agent = agent
    orchestrator._consecutive_silent = 2
    orchestrator._turns_since_recycle = 4
    orchestrator._last_turn_monotonic = 123.0
    stop = threading.Event()
    outputs = []
    reader = threading.Thread(
        target=lambda: outputs.extend(orchestrator.stream_output(stop_event=stop)), daemon=True,
    )
    reader.start()
    assert agent._recv_queue.waiting.wait(1)
    stop.set()
    reader.join(timeout=1)
    assert not reader.is_alive()
    assert outputs == []
    assert not orchestrator.execution_completed
    assert orchestrator._consecutive_silent == 2
    assert orchestrator._turns_since_recycle == 4
    assert orchestrator._last_turn_monotonic == 123.0
