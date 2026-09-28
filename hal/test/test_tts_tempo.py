"""Local v3 tempo preserves pitch, streams early, and leaves other models alone."""

import json
import shutil

import httpx
import numpy as np
import pytest

from hal.drivers.voice.tts.elevenlabs import ElevenLabsTTSBackend
from hal.drivers.voice.tts.tempo import change_tempo


@pytest.mark.parametrize("speed", [0.25, 0.8, 1.15, 2.0, 4.0])
def test_duration_and_pitch(speed):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    rate = 24000
    source = (np.sin(np.arange(rate * 3) * 2 * np.pi * 440 / rate) * 12000).astype("<i2").tobytes()
    out = b"".join(change_tempo((source[i:i+4096] for i in range(0, len(source), 4096)), speed, rate))
    samples = np.frombuffer(out, dtype="<i2")
    assert len(samples) / rate == pytest.approx(3 / speed, abs=0.12)
    spectrum = np.abs(np.fft.rfft(samples))
    frequency = np.argmax(spectrum) * rate / len(samples)
    assert frequency == pytest.approx(440, abs=4)


def test_normal_speed_bypasses_process(monkeypatch):
    monkeypatch.setattr("hal.drivers.voice.tts.tempo.subprocess.Popen", lambda *a, **k: pytest.fail("process started"))
    assert list(change_tempo([b"abc", b"de"], 1.0, 24000)) == [b"abc", b"de"]


def test_streams_before_input_eof_and_closes():
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    finished = []

    def source():
        for _ in range(10000):
            yield b"\x01\x00" * 2048
        finished.append(True)

    output = change_tempo(source(), 1.15, 24000)
    assert next(output)
    assert not finished
    output.close()


@pytest.mark.parametrize("model,provider_speed,local", [
    ("eleven_v3", 1.0, True), ("tts-1", 1.0, True),
    ("eleven_multilingual_v2", 1.2, False),
])
def test_only_effective_v3_gets_local_speed(monkeypatch, model, provider_speed, local):
    requests, tempos = [], []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=b"\x01\x00")

    def tempo(chunks, speed, rate):
        tempos.append((speed, rate))
        yield from chunks

    monkeypatch.setattr("hal.drivers.voice.tts.elevenlabs.change_tempo", tempo)
    backend = object.__new__(ElevenLabsTTSBackend)
    backend._api_key = "test"
    backend._base_url = "https://test.example"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        backend._client = client
        assert b"".join(backend.stream_pcm("Hello", "Ngan", model, 1.5)) == b"\x01\x00"
    assert requests[0]["voice_settings"]["speed"] == provider_speed
    assert tempos == ([(1.5, 24000)] if local else [])


@pytest.mark.parametrize("sizes", [[1, 100, 3995, 17], [9000], [2]])
def test_timing_preserves_http_chunk_boundaries_and_pcm(sizes, caplog):
    class NetworkStream(httpx.SyncByteStream):
        def __iter__(self):
            for size in sizes:
                yield b"a" * size

    backend = object.__new__(ElevenLabsTTSBackend)
    backend._api_key = "private-test-key"
    backend._base_url = "https://test.example"
    caplog.set_level("INFO", logger="hal.voice.tts")
    with httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, stream=NetworkStream())
    )) as client:
        backend._client = client
        chunks = list(backend.stream_pcm("Hello", "Rachel", "eleven_v3", 1.0))
    raw = b"a" * sum(sizes)
    assert chunks == [raw[i:i + 4096] for i in range(0, len(raw), 4096)]
    stages = [r.message.split("stage=", 1)[1].split()[0]
              for r in caplog.records if "[tts-timing]" in r.message]
    assert stages == ["http_start", "http_headers", "http_first_bytes",
                      "buffer_first_chunk", "tempo_first_output"]
    assert "private-test-key" not in caplog.text


@pytest.mark.parametrize("status", [200, 503])
def test_tempo_process_starts_before_http_and_is_reaped(monkeypatch, status):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    import subprocess
    processes = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    def respond(request):
        assert processes, "HTTP started before the tempo process"
        return httpx.Response(status, content=b"\x01\x00" * 24000)

    monkeypatch.setattr("hal.drivers.voice.tts.tempo.subprocess.Popen", spawn)
    backend = object.__new__(ElevenLabsTTSBackend)
    backend._api_key = "test"
    backend._base_url = "https://test.example"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        backend._client = client
        if status == 200:
            assert b"".join(backend.stream_pcm("Hello", "Rachel", "eleven_v3", 1.2))
        else:
            with pytest.raises(httpx.HTTPStatusError):
                list(backend.stream_pcm("Hello", "Rachel", "eleven_v3", 1.2))
    assert len(processes) == 1
    assert processes[0].poll() is not None
