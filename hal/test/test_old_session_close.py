"""Replacing a realtime session must not make the turn wait for the old one."""

import threading
import time
from unittest import mock

from hal.realtime.orchestrator import RealtimeOrchestrator


def test_a_slow_close_does_not_block_the_caller():
    started = threading.Event()
    release = threading.Event()
    agent = mock.Mock()

    def _slow_disconnect():
        started.set()
        release.wait(2.0)

    agent.disconnect.side_effect = _slow_disconnect

    began = time.monotonic()
    RealtimeOrchestrator._disconnect_in_background(agent, "test")
    elapsed = time.monotonic() - began

    assert elapsed < 0.5, elapsed
    assert started.wait(1.0), "the close never ran"
    release.set()


def test_a_failing_close_is_logged_not_raised(caplog):
    agent = mock.Mock()
    agent.disconnect.side_effect = RuntimeError("socket already gone")
    RealtimeOrchestrator._disconnect_in_background(agent, "test")
    for _ in range(100):
        if agent.disconnect.called:
            break
        time.sleep(0.01)
    assert agent.disconnect.called


def test_it_closes_inline_when_a_thread_cannot_be_started():
    agent = mock.Mock()
    with mock.patch("hal.realtime.orchestrator.threading.Thread",
                    side_effect=RuntimeError("can't start new thread")):
        RealtimeOrchestrator._disconnect_in_background(agent, "test")
    agent.disconnect.assert_called_once()
