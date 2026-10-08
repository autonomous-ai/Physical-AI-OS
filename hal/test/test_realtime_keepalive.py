"""An idle live session can be kept open with pings instead of parked."""

import asyncio
import threading
import time
from types import SimpleNamespace

from hal import config as hal_config
from hal.realtime.orchestrator import RealtimeOrchestrator
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


class _Agent:
    def __init__(self):
        self.available = True
        self.pings = 0

    def keepalive(self):
        self.pings += 1
        return True


def _orch(monkeypatch, *, idle_s, interval=20.0):
    monkeypatch.setattr(hal_config, "REALTIME_GEMINI_KEEPALIVE_S", interval, raising=False)
    o = object.__new__(RealtimeOrchestrator)
    o._started = threading.Event()
    o._started.set()
    o._rebuild_lock = threading.Lock()
    o._idle_parked = False
    o._turn_in_flight = False
    o._agent = _Agent()
    o._last_activity_monotonic = time.monotonic() - idle_s
    o._last_turn_monotonic = 0.0
    return o


def test_pings_an_idle_session_once_per_interval(monkeypatch):
    o = _orch(monkeypatch, idle_s=30)
    o._maybe_keepalive()
    o._maybe_keepalive()
    assert o._agent.pings == 1
    o._last_keepalive_monotonic -= 25
    o._maybe_keepalive()
    assert o._agent.pings == 2


def test_no_ping_before_the_interval_or_when_disabled(monkeypatch):
    o = _orch(monkeypatch, idle_s=5)
    o._maybe_keepalive()
    assert o._agent.pings == 0
    o = _orch(monkeypatch, idle_s=60, interval=0)
    o._maybe_keepalive()
    assert o._agent.pings == 0


def test_no_ping_while_parked_or_mid_turn(monkeypatch):
    o = _orch(monkeypatch, idle_s=60)
    o._idle_parked = True
    o._maybe_keepalive()
    o._idle_parked = False
    o._turn_in_flight = True
    o._maybe_keepalive()
    assert o._agent.pings == 0


def test_gemini_keepalive_pings_the_socket_on_the_io_loop():
    class _WS:
        def __init__(self):
            self.pings = 0

        async def ping(self):
            self.pings += 1

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        agent = object.__new__(GeminiLiveAgent)
        agent._loop = loop
        ws = _WS()
        agent._session = SimpleNamespace(_ws=ws)
        assert agent.keepalive()
        assert ws.pings == 1
        agent._session = None
        assert not agent.keepalive()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
