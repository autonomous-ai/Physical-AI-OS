"""A provider/voice preview speaks one utterance; the running service keeps its saved backend and voice."""

import queue
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException

from hal.drivers.voice.tts.service import TTSService
from hal.models import SpeakRequest
from hal.routes import voice


def _stream(log, tag):
    def stream(**kwargs):
        log.append((tag, kwargs["voice"]))
        yield b"\x01\x00" * 100
    return stream


def test_preview_backend_and_voice_reach_head_and_tail_only():
    log = []
    svc = object.__new__(TTSService)
    svc._np = np
    svc._speed = 1.0
    svc._voice = "Ngan"
    svc._model = "eleven_v3"
    svc._instructions = None
    svc._max_retries = 0
    svc._stop_event = threading.Event()
    saved = SimpleNamespace(sample_rate=24000, volume_boost=1.0, stream_pcm=_stream(log, "saved"))
    other = SimpleNamespace(sample_rate=24000, volume_boost=1.0, stream_pcm=_stream(log, "preview"))
    svc._backend = saved
    svc._head_producer("head", 24000, queue.Queue(), (1, 2), preview=(other, "Kore"))
    svc._tail_producer(["tail"], 24000, queue.Queue(), preview=(other, "Kore"))
    list(svc._iter_tts_samples("next normal turn", 24000))
    assert log == [("preview", "Kore"), ("preview", "Kore"), ("saved", "Ngan")]
    assert svc._backend is saved and svc._voice == "Ngan"


def _route(monkeypatch, created):
    received = []
    saved = SimpleNamespace(available=True, _api_key="k", _base_url="https://p/api/v1/ai/v1/elevenlabs")
    svc = SimpleNamespace(available=True, _backend=saved, _provider="elevenlabs", _voice="Ngan",
                          _sd=object(), speak=lambda text, **kw: received.append(kw) or True)
    monkeypatch.setattr(voice.state, "tts_service", svc)
    monkeypatch.setattr(voice.state, "_speaker_muted", False)
    monkeypatch.setattr(voice.state, "music_service", None)
    import hal.drivers.voice.tts as tts_pkg
    monkeypatch.setattr(tts_pkg, "create_backend",
                        lambda **kw: created.append(kw) or SimpleNamespace(available=True))
    return svc, saved, received


def test_route_preview_of_other_provider_does_not_swap_service(monkeypatch):
    created = []
    svc, saved, received = _route(monkeypatch, created)
    assert voice.speak_text(SpeakRequest(text="hi", provider="gemini", voice="Kore")) == {"status": "ok"}
    assert created[0]["provider"] == "gemini"
    backend, preview_voice = received[0]["preview"]
    assert preview_voice == "Kore" and backend is not saved
    assert svc._backend is saved and svc._provider == "elevenlabs" and svc._voice == "Ngan"


def test_route_voice_only_preview_keeps_saved_voice(monkeypatch):
    created = []
    svc, saved, received = _route(monkeypatch, created)
    voice.speak_text(SpeakRequest(text="hi", voice="Linh"))
    assert created == []
    assert received[0]["preview"] == (saved, "Linh")
    assert svc._voice == "Ngan"


def test_route_rejects_cached_preview(monkeypatch):
    _route(monkeypatch, [])
    with pytest.raises(HTTPException) as e:
        voice.speak_text(SpeakRequest(text="hi", voice="Linh", cached=True))
    assert e.value.status_code == 400
