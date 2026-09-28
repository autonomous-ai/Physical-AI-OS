"""Saved speed reuses the existing TTS pipeline and provider settings."""

import json
import queue
import threading
from types import SimpleNamespace

import httpx
import pytest
import numpy as np

from hal import config
from hal.drivers.voice.tts.elevenlabs import ElevenLabsTTSBackend
from hal.drivers.voice.tts.elevenlabs_ws import ElevenLabsWSTTSBackend
from hal.models import SpeakRequest, TTSConfigRequest, VoiceStartRequest
from hal.drivers.voice.tts.service import TTSService
from hal.routes import voice


def test_elevenlabs_normal_speed_is_sent_explicitly():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=b"\x00\x00")

    backend = object.__new__(ElevenLabsTTSBackend)
    backend._api_key = "test-only"
    backend._base_url = "https://proxy.example/v1/elevenlabs"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        backend._client = client
        list(backend.stream_pcm("Hello", "Rachel", "eleven_v3", 1.0))
    assert requests[0]["voice_settings"]["speed"] == 1.0

    class Socket:
        def send(self, text):
            requests.append(json.loads(text))

        def close(self):
            pass

        def __iter__(self):
            return iter([])

    ws = object.__new__(ElevenLabsWSTTSBackend)
    ws._api_key = "test-only"
    ws._url_tmpl = "wss://proxy.example/{voice_id}?model_id={model}"
    ws._connect = lambda *args, **kwargs: Socket()
    list(ws.stream_pcm("Hello", "Rachel", "", 1.0))
    assert requests[1]["voice_settings"]["speed"] == 1.0


@pytest.mark.parametrize("saved", [None, 0, 5, "bad", True])
def test_missing_or_invalid_saved_speed_preserves_legacy_env(tmp_path, monkeypatch, saved):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "OS_CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "TTS_SPEED", 1.5)
    assert config.get_tts_speed() == 1.5
    path.write_text(json.dumps({"tts_speed": saved}))
    assert config.get_tts_speed() == 1.5


def test_voice_recreation_reads_latest_saved_speed_without_go_startup_field(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "OS_CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "TTS_SPEED", 1.3)
    monkeypatch.setattr(voice.state, "simulation_audio", False)
    monkeypatch.setattr(voice.state, "voice_service", SimpleNamespace(available=True))
    monkeypatch.setattr(voice.state, "music_service", None)
    monkeypatch.setattr(voice, "TTSService", lambda **kwargs: SimpleNamespace(_speed=kwargs["speed"]))
    for speed in [1.2, 1.5]:
        path.write_text(json.dumps({"tts_speed": speed}))
        monkeypatch.setattr(voice.state, "tts_service", None)
        voice.start_voice(VoiceStartRequest(llm_api_key="test", llm_base_url="https://proxy.example"))
        assert voice.state.tts_service._speed == speed


def test_existing_hal_config_endpoint_still_accepts_openai_speed(monkeypatch):
    service = SimpleNamespace(_speed=1.3, _voice="nova", _provider="openai",
                              _backend=SimpleNamespace(_api_key="", _base_url=""))
    monkeypatch.setattr(voice.state, "tts_service", service)
    monkeypatch.setattr(voice, "threading", SimpleNamespace(Thread=lambda **kwargs: SimpleNamespace(start=lambda: None)))
    voice.update_tts_config(TTSConfigRequest(speed=1.5))
    assert service._speed == 1.5


def test_preview_speed_reaches_head_and_tail_without_changing_next_turn():
    speeds = []

    def stream(**kwargs):
        speeds.append(kwargs["speed"])
        yield b"\x01\x00" * 100

    svc = object.__new__(TTSService)
    svc._np = np
    svc._speed = 1.2
    svc._voice = "Rachel"
    svc._model = "eleven_v3"
    svc._instructions = None
    svc._max_retries = 0
    svc._stop_event = threading.Event()
    svc._backend = SimpleNamespace(sample_rate=24000, volume_boost=1.0, stream_pcm=stream)
    svc._head_producer("preview head", 24000, queue.Queue(), (1, 2), speed=1.5)
    svc._tail_producer(["preview tail"], 24000, queue.Queue(), speed=1.5)
    list(svc._iter_tts_samples("next normal turn", 24000))
    assert speeds == [1.5, 1.5, 1.2]
    assert svc._speed == 1.2


def test_preview_route_passes_speed_without_mutating_service(monkeypatch):
    received = []
    svc = SimpleNamespace(available=True, _speed=1.2,
                          speak=lambda text, **kw: received.append(kw) or True)
    monkeypatch.setattr(voice.state, "tts_service", svc)
    monkeypatch.setattr(voice.state, "_speaker_muted", False)
    monkeypatch.setattr(voice.state, "music_service", None)
    assert voice.speak_text(SpeakRequest(text="test", speed=1.5)) == {"status": "ok"}
    assert received[0]["speed"] == 1.5
    assert svc._speed == 1.2


@pytest.mark.parametrize("speed", [0, 4.1, float("nan"), float("inf")])
def test_preview_rejects_invalid_speed(speed):
    with pytest.raises(ValueError):
        SpeakRequest(text="test", speed=speed)


def test_preview_does_not_use_saved_speed_cache(monkeypatch):
    svc = object.__new__(TTSService)
    svc._backend = SimpleNamespace(available=True)
    svc._sd = object()
    svc._optional_speech_blocked = lambda *args: False
    svc._speaker_muted = lambda: False
    svc._tts_cache_path = lambda text: pytest.fail("preview looked up default-speed cache")
    svc._lock = threading.Lock()
    svc._claim_speech = lambda *args: True
    captured = []
    monkeypatch.setattr("hal.drivers.voice.tts.service.threading.Thread",
                        lambda **kw: SimpleNamespace(start=lambda: captured.append(kw)))
    assert svc.speak("preview", speed=1.5)
    assert captured[0]["kwargs"] == {"speed": 1.5}
