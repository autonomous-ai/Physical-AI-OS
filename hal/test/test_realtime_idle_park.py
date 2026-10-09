"""The device must not let the server close an idle Gemini session."""

import threading
import time
from types import SimpleNamespace

from hal import config as hal_config
from hal.realtime.orchestrator import RealtimeOrchestrator


class _Agent:
    def __init__(self, available: bool = True):
        self.available = available
        self.disconnected = 0
        self.requires_fresh_session = False

    def disconnect(self):
        self.disconnected += 1
        self.available = False


def _orch(monkeypatch, *, idle_s: float, threshold: float = 45.0, agent=None):
    monkeypatch.setattr(hal_config, "REALTIME_PROVIDER", "gemini", raising=False)
    monkeypatch.setattr(
        hal_config, "REALTIME_GEMINI_IDLE_PARK_S", threshold, raising=False
    )
    o = object.__new__(RealtimeOrchestrator)
    o._lifecycle_lock = threading.Lock()
    o._started = threading.Event()
    o._started.set()
    o._rebuild_lock = threading.Lock()
    o._rebuild_done = threading.Event()
    o._idle_parked = False
    o._park_resume_failed = False
    o._turn_in_flight = False
    o._turn_started_monotonic = 0.0
    o._agent = _Agent() if agent is None else agent
    o._last_activity_monotonic = time.monotonic() - idle_s
    o._last_turn_monotonic = 0.0
    return o


def test_parks_after_threshold(monkeypatch):
    o = _orch(monkeypatch, idle_s=90)
    agent = o._agent
    o._maybe_park_idle_session()
    assert agent.disconnected == 1, "an idle session must be closed by the device"
    assert o._idle_parked is True


def test_no_park_below_threshold(monkeypatch):
    o = _orch(monkeypatch, idle_s=10)
    o._maybe_park_idle_session()
    assert o._agent.disconnected == 0
    assert o._idle_parked is False


def test_threshold_zero_disables(monkeypatch):
    o = _orch(monkeypatch, idle_s=900, threshold=0.0)
    o._maybe_park_idle_session()
    assert o._agent.disconnected == 0


def test_non_gemini_provider_untouched(monkeypatch):
    """OpenAI Realtime bills per token: an idle session is free, leave it open."""
    o = _orch(monkeypatch, idle_s=900)
    monkeypatch.setattr(hal_config, "REALTIME_PROVIDER", "openai", raising=False)
    o._maybe_park_idle_session()
    assert o._agent.disconnected == 0


def test_gptlive_parks_on_its_own_threshold(monkeypatch):
    """GPT-Live parks on REALTIME_GPTLIVE_IDLE_PARK_S."""
    o = _orch(monkeypatch, idle_s=40, threshold=45.0)
    monkeypatch.setattr(hal_config, "REALTIME_PROVIDER", "gptlive", raising=False)
    monkeypatch.setattr(hal_config, "REALTIME_GPTLIVE_IDLE_PARK_S", 30.0, raising=False)
    o._maybe_park_idle_session()
    assert o._agent.disconnected == 1 and o._idle_parked
    o2 = _orch(monkeypatch, idle_s=40)
    monkeypatch.setattr(hal_config, "REALTIME_GPTLIVE_IDLE_PARK_S", 0.0, raising=False)
    o2._maybe_park_idle_session()
    assert o2._agent.disconnected == 0


def test_turn_in_flight_blocks_park(monkeypatch):
    """Parking mid-turn would kill the turn the user is waiting on."""
    o = _orch(monkeypatch, idle_s=90)
    o._turn_in_flight = True
    o._turn_started_monotonic = time.monotonic()
    o._maybe_park_idle_session()
    assert o._agent.disconnected == 0


def test_abandoned_turn_marker_expires(monkeypatch):
    """A prepared-then-abandoned turn must not disable parking forever."""
    # A freshly booted host can have less than 900 s of monotonic uptime;
    # use a fixed clock so the synthetic activity timestamp stays valid.
    monkeypatch.setattr(time, "monotonic", lambda: 10_000.0)
    o = _orch(monkeypatch, idle_s=900)
    o._turn_in_flight = True
    o._turn_started_monotonic = time.monotonic() - 600
    o._maybe_park_idle_session()
    assert o._agent.disconnected == 1


def test_parked_session_still_available(monkeypatch):
    """A parked session still reports available."""
    o = _orch(monkeypatch, idle_s=90)
    o._maybe_park_idle_session()
    assert o.available is True


def test_prepare_turn_resumes_parked_session(monkeypatch):
    o = _orch(monkeypatch, idle_s=90)
    o._maybe_park_idle_session()
    rebuilt = []
    o._rebuild_now = lambda reason, **kw: (rebuilt.append(reason), True)[1]
    o._skip_post_idle_recycle = False
    o.prepare_turn()
    assert rebuilt == ["idle-park-resume"]
    assert o._skip_post_idle_recycle is True


def test_failed_resume_reports_unavailable(monkeypatch):
    """A failed resume reports unavailable so the turn falls back to the main agent."""
    o = _orch(monkeypatch, idle_s=90)
    o._maybe_park_idle_session()
    o._rebuild_now = lambda reason, **kw: False
    o._skip_post_idle_recycle = False
    o.prepare_turn()
    assert o.available is False
    assert o._idle_parked is True, "stay parked so the next turn retries the resume"


def test_prewarm_resumes_in_background_and_prepare_turn_joins(monkeypatch):
    """prepare_turn() joins an in-flight resume instead of reconnecting."""
    o = _orch(monkeypatch, idle_s=90)
    o._maybe_park_idle_session()
    o._skip_post_idle_recycle = False
    reasons = []

    def slow_rebuild(reason, discard_old_on_failure=False, cancel_event=None):
        try:
            reasons.append(reason)
            time.sleep(0.2)
            o._agent = _Agent()
            o._idle_parked = False
            return True
        finally:
            o._finish_rebuild()

    o._rebuild_locked = slow_rebuild
    o._rebuild_now = lambda reason, **kw: (reasons.append(reason), True)[1]
    assert o.prewarm() is True
    assert o.rebuilding is True
    assert o.prewarm() is False, "second prewarm must not start another rebuild"
    o.prepare_turn()
    assert reasons == ["idle-park-prewarm"], "prepare_turn must join, not reconnect"
    assert o._idle_parked is False
    assert o._skip_post_idle_recycle is True
    assert o.available is True


def test_prewarm_noop_when_not_parked(monkeypatch):
    o = _orch(monkeypatch, idle_s=10)
    assert o.prewarm() is False
    assert o.rebuilding is False


class _StreamingAgent(_Agent):
    """Streams a long reply, running the idle watchdog between chunks.

    Device-observed 2026-09-29 (green-lamp, #449 follow-up): a 124-143 s
    recap was parked mid-sentence — "124s idle (>= 45s) — parking gemini
    session" — because streamed output never counted as activity once the
    120 s in-flight guard expired.
    """

    def __init__(self, orch, clock, chunks: int, gap_s: float):
        super().__init__()
        self.execution_completed = True
        self._orch, self._clock, self._chunks, self._gap_s = orch, clock, chunks, gap_s

    def receive(self, **_kw):
        from hal.realtime.models import TextOutput
        for i in range(self._chunks):
            self._clock[0] += self._gap_s
            self._orch._maybe_park_idle_session()  # the watchdog tick in this gap
            if not self.available:
                return
            yield TextOutput(text=f"part {i}. ")

    def end_turn(self):
        pass


def _streaming_orch(monkeypatch, *, chunks: int, gap_s: float):
    from hal.realtime import orchestrator as orch_mod

    clock = [10_000.0]
    monkeypatch.setattr(orch_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hal_config, "REALTIME_SESSION_MAX_TURNS", 0, raising=False)
    o = _orch(monkeypatch, idle_s=0)
    o._agent = _StreamingAgent(o, clock, chunks, gap_s)
    # prepare_turn() + commit at t0: the turn is in flight from here.
    o._turn_in_flight = True
    o._turn_started_monotonic = clock[0]
    o._last_activity_monotonic = clock[0]
    o._skip_post_idle_recycle = False
    o._consecutive_silent = 0
    o._turns_since_recycle = 0
    o._idle_reset_pending = False
    return o


def test_long_streaming_reply_is_not_parked(monkeypatch):
    # 15 chunks, 10 s apart: a 150 s reply, past the 120 s in-flight guard.
    o = _streaming_orch(monkeypatch, chunks=15, gap_s=10.0)
    agent = o._agent
    outputs = list(o.stream_output())
    assert agent.disconnected == 0, "a session that is still talking is not idle"
    assert len(outputs) == 15
