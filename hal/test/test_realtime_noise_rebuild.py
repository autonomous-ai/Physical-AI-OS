"""Regression tests for the non-blocking Gemini noise-drop recovery path."""

import threading
import time

from hal.realtime.orchestrator import RealtimeOrchestrator


class _OldAgent:
    available = True
    _activity_started = True

    def __init__(self) -> None:
        self.disconnected = False

    def disconnect(self) -> None:
        self.disconnected = True


class _NewAgent:
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self.available = False
        self._entered = entered
        self._release = release

    def connect(self) -> None:
        self._entered.set()
        assert self._release.wait(timeout=1.0)
        self.available = True


class _FailingNewAgent:
    available = False

    def connect(self) -> None:
        raise RuntimeError("test connection failure")


class _Context:
    def build_instructions(self) -> str:
        return "test instructions"


def _orchestrator_for_rebuild(
    entered: threading.Event, release: threading.Event
) -> tuple[RealtimeOrchestrator, _OldAgent]:
    """Build the smallest orchestrator fixture that exercises the rebuild path."""
    old = _OldAgent()
    orchestrator = object.__new__(RealtimeOrchestrator)
    orchestrator._rebuild_lock = threading.Lock()
    orchestrator._rebuild_done = threading.Event()
    orchestrator._rebuild_done.set()
    orchestrator._started = threading.Event()
    orchestrator._started.set()
    orchestrator._lifecycle_lock = threading.Lock()
    orchestrator._agent = old
    orchestrator._context = _Context()
    orchestrator._make_agent = lambda provider, instructions: _NewAgent(entered, release)
    orchestrator._consecutive_silent = 3
    orchestrator._idle_reset_pending = True
    orchestrator._turns_since_recycle = 4
    orchestrator._looked_this_turn = True
    orchestrator._last_look_sent_monotonic = 1.0
    return orchestrator, old


def _wait_disconnected(agent, timeout=2.0):
    """The replaced session is closed on its own thread, so poll for it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if agent.disconnected:
            return True
        time.sleep(0.01)
    return agent.disconnected


def test_noise_drop_rebuild_reserves_session_before_connecting():
    """Audio capture can identify the in-flight rebuild before its WS is ready."""
    entered = threading.Event()
    release = threading.Event()
    orchestrator, old = _orchestrator_for_rebuild(entered, release)

    assert orchestrator.discard_open_activity("noise-drop")
    assert entered.wait(timeout=1.0)
    assert orchestrator.rebuilding
    assert not orchestrator.available
    assert not orchestrator.discard_open_activity("noise-drop")

    release.set()
    assert orchestrator.wait_until_available(timeout_s=1.0)
    assert orchestrator.available
    assert _wait_disconnected(old)


def test_noise_drop_does_not_reuse_contaminated_session_after_connect_failure():
    """A failed clean replacement falls back instead of reusing dropped audio."""
    entered = threading.Event()
    release = threading.Event()
    orchestrator, old = _orchestrator_for_rebuild(entered, release)
    orchestrator._make_agent = lambda provider, instructions: _FailingNewAgent()

    assert orchestrator.discard_open_activity("noise-drop")
    assert not orchestrator.wait_until_available(timeout_s=1.0)
    assert orchestrator._agent is None
    assert _wait_disconnected(old)
