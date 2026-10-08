"""Cloud startup cannot delay a local mode change or publish a retired session."""

import threading
import time
from unittest.mock import Mock

import pytest

from hal.test.test_realtime_initial_connect_retry import _orchestrator_for_initial_retry


def orchestrator():
    rt = _orchestrator_for_initial_retry()
    rt._agent = None
    rt._started.clear()
    rt._startup_generation = 0
    rt._startup_pending = None
    rt._startup_worker = None
    rt._idle_park_thread = None
    rt._idle_park_stop = threading.Event()
    rt._initial_connect_exc = None
    rt._catch_up_memory_summaries = Mock()
    rt._start_idle_park_loop = Mock()
    return rt


def test_repeated_switches_coalesce_startup_and_close_late_old_connection(monkeypatch):
    rt = orchestrator()
    entered, release, published = (threading.Event() for _ in range(3))
    old, new = Mock(), Mock()
    old.available = new.available = True

    def blocked_connect():
        entered.set()
        assert release.wait(2)

    old.connect.side_effect = blocked_connect
    rt._make_agent = Mock(side_effect=[old, new])
    rt._start_idle_park_loop.side_effect = published.set
    rt.start()
    assert entered.wait(1)
    worker = rt._startup_worker
    start = time.perf_counter()
    for _ in range(20):
        rt.stop(summarize=False)
        rt.start()
        assert rt._startup_worker is worker
    elapsed = time.perf_counter() - start
    assert elapsed < .5
    assert not rt.available
    assert rt._make_agent.call_count == 1
    release.set()
    assert published.wait(1)
    worker.join(1)
    old.disconnect.assert_called_once()
    assert rt._agent is new
    assert rt.available
    rt.stop(summarize=False)
    new.disconnect.assert_called_once()
    assert not rt.available


def test_stop_during_connect_does_not_reopen_stopped_session():
    rt = orchestrator()
    entered, release = threading.Event(), threading.Event()
    old = Mock()
    old.connect.side_effect = lambda: (entered.set(), release.wait(2))
    rt._make_agent = Mock(return_value=old)
    rt.start()
    assert entered.wait(1)
    worker = rt._startup_worker
    rt.stop(summarize=False)
    release.set()
    worker.join(1)
    assert not worker.is_alive()
    assert rt._agent is None
    assert not rt._started.is_set()
    rt._start_idle_park_loop.assert_not_called()
    old.disconnect.assert_called_once()


@pytest.mark.parametrize("fails", [False, True])
def test_retired_rebuild_cannot_replace_or_clear_new_agent(fails):
    rt = orchestrator()
    rt._started.set()
    entered, release = threading.Event(), threading.Event()
    failed, current = Mock(), Mock()

    def connect():
        entered.set()
        assert release.wait(2)
        if fails:
            raise RuntimeError('retired connection failed')

    failed.connect.side_effect = connect
    rt._make_agent = Mock(return_value=failed)
    worker = threading.Thread(target=lambda: rt._rebuild_now('test', discard_old_on_failure=True))
    worker.start()
    assert entered.wait(1)
    rt.stop(summarize=False)
    with rt._lifecycle_lock:
        rt._agent = current
        rt._started.set()
    release.set()
    worker.join(1)
    assert not worker.is_alive()
    assert rt._agent is current
    failed.disconnect.assert_called_once()
