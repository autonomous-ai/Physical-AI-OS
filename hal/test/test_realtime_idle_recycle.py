"""Gemini closes a session it receives nothing on (WS 1008), regardless of model.
prepare_turn() must recycle before a post-silence turn streams, or that turn lands
on a dead session and is lost — the user-visible "I had to say it twice"."""

import time
from types import SimpleNamespace

import pytest

from hal import config as hal_config
from hal.realtime.orchestrator import RealtimeOrchestrator


def _orch(monkeypatch, model: str, idle_s: float, threshold: float = 60.0):
    monkeypatch.setattr(hal_config, "REALTIME_PROVIDER", "gemini", raising=False)
    monkeypatch.setattr(hal_config, "REALTIME_GEMINI_MODEL", model, raising=False)
    monkeypatch.setattr(
        hal_config, "REALTIME_GEMINI_PRE_TURN_RECYCLE_S", threshold, raising=False
    )
    o = object.__new__(RealtimeOrchestrator)
    o._skip_post_idle_recycle = False
    o._last_turn_monotonic = time.monotonic() - idle_s
    o._rebuilt = []
    o._agent = None
    o._rebuild_now = lambda reason, **kwargs: (o._rebuilt.append(reason), True)[1]
    return o


# The regression this fixes: the recycle used to be gated on
# gemini_needs_idle_workaround(), which is True only for native-audio. Every 3.1
# device therefore ran with no protection while still dying to the same 1008.
def test_recycles_on_non_native_audio_model(monkeypatch):
    o = _orch(monkeypatch, "models/gemini-3.1-flash-live-preview", idle_s=90)
    o.prepare_turn()
    assert o._rebuilt == ["gemini-idle-pre-turn"], (
        "a 3.1 session idle past the threshold must be recycled before the turn"
    )
    assert o._skip_post_idle_recycle is True


def test_still_recycles_on_native_audio(monkeypatch):
    """The models that always had this must keep it."""
    o = _orch(monkeypatch, "models/gemini-2.5-flash-native-audio", idle_s=90)
    o.prepare_turn()
    assert o._rebuilt == ["gemini-idle-pre-turn"]


def test_no_recycle_below_threshold(monkeypatch):
    """An active conversation must not pay a handshake between every turn."""
    o = _orch(monkeypatch, "models/gemini-3.1-flash-live-preview", idle_s=5)
    o.prepare_turn()
    assert o._rebuilt == []
    assert o._skip_post_idle_recycle is False


def test_threshold_zero_disables(monkeypatch):
    """0 is the documented off switch — it must actually turn it off."""
    o = _orch(monkeypatch, "models/gemini-3.1-flash-live-preview", idle_s=999, threshold=0)
    o.prepare_turn()
    assert o._rebuilt == []


def test_non_gemini_provider_untouched(monkeypatch):
    o = _orch(monkeypatch, "models/gemini-3.1-flash-live-preview", idle_s=999)
    monkeypatch.setattr(hal_config, "REALTIME_PROVIDER", "openai", raising=False)
    o.prepare_turn()
    assert o._rebuilt == []


def test_unresolved_tool_call_rebuilds_before_audio(monkeypatch):
    o = _orch(monkeypatch, "models/gemini-3.1-flash-live-preview", idle_s=5)
    o._agent = SimpleNamespace(requires_fresh_session=True)

    o.prepare_turn()

    assert o._rebuilt == ["gemini-unresolved-tool-call"]
    assert o._skip_post_idle_recycle is True


# The threshold has to sit under the shortest idle gap that actually killed a
# session in the field (86s), or the recycle fires after Gemini already closed it.
def test_default_threshold_clears_the_observed_failure_floor():
    assert 0 < hal_config.REALTIME_GEMINI_PRE_TURN_RECYCLE_S < 86, (
        "default must be below the 86s shortest observed idle death; "
        f"got {hal_config.REALTIME_GEMINI_PRE_TURN_RECYCLE_S}"
    )


# Device 2026-09-23 14:09: a privacy-switch unmute restarts realtime on the SAME
# orchestrator and connects a fresh session, but the idle clock still counted
# from the last turn (133s ago). prepare_turn() threw the 0s-old session away,
# the rebuild took 10s, and the turn fell back to the main agent.
def test_fresh_session_is_not_recycled_for_an_old_last_turn(monkeypatch):
    o = _orch(monkeypatch, "models/gemini-3.8-live-extended-thinking", idle_s=133)
    o._session_connected_monotonic = time.monotonic() - 1
    o.prepare_turn()
    assert o._rebuilt == []
    assert o._skip_post_idle_recycle is False


def test_session_idle_since_connect_is_still_recycled(monkeypatch):
    o = _orch(monkeypatch, "models/gemini-3.8-live-extended-thinking", idle_s=133)
    o._session_connected_monotonic = time.monotonic() - 90
    o.prepare_turn()
    assert o._rebuilt == ["gemini-idle-pre-turn"]


def test_rebuild_stamps_the_session_connect_time(monkeypatch):
    import threading

    monkeypatch.setattr(hal_config, "REALTIME_PROVIDER", "gemini", raising=False)
    o = object.__new__(RealtimeOrchestrator)
    o._agent = None
    o._context = SimpleNamespace(build_instructions=lambda: "")
    o._make_agent = lambda provider, instructions: SimpleNamespace(
        connect=lambda: None, disconnect=lambda: None,
    )
    o._lifecycle_lock = threading.Lock()
    o._started = threading.Event()
    o._started.set()
    o._rebuild_lock = threading.Lock()
    o._rebuild_lock.acquire()
    o._rebuild_done = threading.Event()
    o._session_connected_monotonic = 0.0
    before = time.monotonic()
    assert o._rebuild_locked("test") is True
    assert o._session_connected_monotonic >= before
