"""Gemini TTS selected + Gemini Live realtime → Live speaks natively in the TTS voice."""

import time
from types import SimpleNamespace

from hal import config as hal_config
from hal.drivers.voice.tts.gemini import native_voice
from hal.realtime.orchestrator import RealtimeOrchestrator


def test_native_voice_only_for_gemini_tts_on_gemini_live(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_PROVIDER", "gemini", raising=False)
    assert native_voice(SimpleNamespace(_provider="gemini", _voice="Puck")) == "Puck"
    # A voice saved under another provider falls back like the TTS backend does.
    assert native_voice(SimpleNamespace(_provider="gemini", _voice="Rachel")) == "Kore"
    assert native_voice(SimpleNamespace(_provider="elevenlabs", _voice="Puck")) is None
    assert native_voice(None) is None
    monkeypatch.setattr(hal_config, "REALTIME_PROVIDER", "openai", raising=False)
    assert native_voice(SimpleNamespace(_provider="gemini", _voice="Puck")) is None


def _orch(monkeypatch, session_voice, wanted):
    monkeypatch.setattr(hal_config, "REALTIME_PROVIDER", "gemini", raising=False)
    monkeypatch.setattr(hal_config, "REALTIME_GEMINI_VOICE", "Kore", raising=False)
    monkeypatch.setattr(hal_config, "REALTIME_GEMINI_PRE_TURN_RECYCLE_S", 0, raising=False)
    o = object.__new__(RealtimeOrchestrator)
    o._skip_post_idle_recycle = False
    o._last_turn_monotonic = time.monotonic()
    o._voice_override = lambda: wanted
    o._agent = SimpleNamespace(_config=SimpleNamespace(voice=session_voice), requires_fresh_session=False)
    o._rebuilt = []
    o._rebuild_now = lambda reason, **kwargs: (o._rebuilt.append(reason), True)[1]
    return o


def test_voice_change_rebuilds_session_before_turn(monkeypatch):
    o = _orch(monkeypatch, session_voice="Kore", wanted="Puck")
    o.prepare_turn()
    assert o._rebuilt == ["gemini-voice-change"]


def test_same_voice_keeps_session(monkeypatch):
    o = _orch(monkeypatch, session_voice="Puck", wanted="Puck")
    o.prepare_turn()
    assert o._rebuilt == []


def test_leaving_gemini_tts_returns_to_config_voice(monkeypatch):
    o = _orch(monkeypatch, session_voice="Puck", wanted=None)
    o.prepare_turn()
    assert o._rebuilt == ["gemini-voice-change"]
