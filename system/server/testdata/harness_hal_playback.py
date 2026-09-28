"""Local HAL HTTP/playback fixture; never opens a microphone or physical speaker.

Real components: voice router, Harness announcer/queue/sanitized fallback,
TTSService constructor/admission/workers, PCM
conversion/resampling, result cue and _WatchedStream playback tracking.
Substitutions: external TTS provider and sounddevice output hardware. The default
provider emits deterministic tones (not speech); HARNESS_TEST_MAC_SAY=1 uses the
Mac's local `say` command to synthesize the actual request text instead.
"""
import json
import logging
import os
from pathlib import Path
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import sys
import tempfile
import threading
import time
import wave
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import numpy as np
from fastapi import FastAPI, HTTPException, Request
import httpx
from hal import app_state
from hal.drivers.voice.tts import service as tts_module
from hal.routes import voice
from hal.drivers.harness import announcer as announcer_module

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
lock = threading.RLock()
scratch = Path(tempfile.mkdtemp(prefix="harness-hal-playback-"))
wav_path = scratch / "captured.wav"
stats = {"requests": [], "writes": [], "completions": [], "playback_owners": []}
captured = []
svc = None
use_say = os.environ.get("HARNESS_TEST_MAC_SAY") == "1"


class LocalBackend:
    available = True
    sample_rate = 24000
    volume_boost = 1.0
    supports_synthesis_cancellation = True

    def stream_pcm(self, *, text, cancelled, **kwargs):
        if use_say:
            with tempfile.TemporaryDirectory(dir=scratch) as directory:
                source = Path(directory) / "text.txt"
                output = Path(directory) / "speech.wav"
                source.write_text(text)
                subprocess.run(["/usr/bin/say", "--file-format=WAVE",
                                "--data-format=LEI16@24000", "-f", str(source),
                                "-o", str(output)], check=True, capture_output=True,
                               timeout=60)
                with wave.open(str(output), "rb") as audio:
                    if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
                        raise RuntimeError("say must produce mono signed 16-bit PCM")
                    raw = audio.readframes(audio.getnframes())
        else:
            samples = np.arange(12000, dtype=np.float32) / self.sample_rate
            raw = (np.sin(2 * np.pi * 440 * samples) * 8000).astype("<i2").tobytes()
        for offset in range(0, len(raw), 2400):
            if cancelled():
                return
            yield raw[offset:offset + 2400]


class CaptureStream:
    latency = .01
    active = True

    def __init__(self, samplerate=44100, **kwargs):
        self.samplerate = samplerate

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def abort(self):
        self.active = False

    def close(self):
        self.active = False

    def write(self, data):
        # Inspect the unmodified _WatchedStream caller only to classify writes.
        # Probes/idle keepalive never enter a tracked playback write.
        caller = sys._getframe(1)
        tracked = caller.f_locals.get("track_playback")
        samples = np.asarray(data, dtype=np.float32).reshape(-1)
        if tracked is not None and len(samples):
            with lock:
                stats["writes"].append({"kind": "speech" if tracked else "cue",
                                        "frames": len(samples),
                                        "nonzero": int(np.count_nonzero(samples)),
                                        "owner": svc._playback_owner,
                                        "sample_rate": self.samplerate})
                captured.append(samples.copy())
            # A bounded sink delay leaves cancellation/busy gates observable.
            time.sleep(min(len(samples) / self.samplerate, .01))
        return False


class CaptureDevice:
    OutputStream = CaptureStream
    PortAudioError = RuntimeError

    @staticmethod
    def query_devices(*args, **kwargs):
        return {"name": "test PCM sink", "default_high_output_latency": .01}


def on_audio(owner):
    with lock:
        stats["playback_owners"].append(owner)


def on_end():
    with lock:
        snapshot = svc.history_completion()
        stats["completions"].append({"owner": svc._playback_owner,
                                     "history": snapshot})
        pcm = np.concatenate(captured) if captured else np.empty(0)
        with wave.open(str(wav_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(svc._stream_rate or 44100)
            output.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())


with patch.object(tts_module, "create_backend", return_value=LocalBackend()):
    svc = tts_module.TTSService(api_key="local-test", base_url="local-test",
                                sound_device_module=CaptureDevice(), numpy_module=np,
                                on_playback_audio=on_audio, on_speak_end=on_end)
app_state.tts_service = svc
app_state._speaker_muted = False
app_state.music_service = SimpleNamespace(streaming=False)
# Keep the real queue/worker/gate; disable cloud/realtime rendering explicitly.
announcer = announcer_module.HarnessAnnouncer(
    get_tts=lambda: svc, get_voice_service=lambda: None,
    get_music=lambda: app_state.music_service, summarize=lambda instructions, content: "",
)
announcer_module._default = announcer
app = FastAPI()
app.include_router(voice.router)


@app.middleware("http")
async def record_request(request: Request, call_next):
    if request.url.path == "/voice/harness/update":
        body = await request.json()
        with lock:
            stats["requests"].append(body)
    return await call_next(request)


@app.get("/test/state")
def get_state():
    with lock:
        return {**stats, "speaking": svc.speaking,
                "busy": svc._lock.locked(), "muted": app_state._speaker_muted,
                "speech_frames": sum(w["frames"] for w in stats["writes"] if w["kind"] == "speech"),
                "cue_frames": sum(w["frames"] for w in stats["writes"] if w["kind"] == "cue"),
                "wav_path": str(wav_path), "provider": "mac-say" if use_say else "synthetic-tone"}


@app.post("/test/reset")
def reset():
    with lock:
        if svc.speaking or svc._lock.locked():
            raise HTTPException(409, "playback still active")
        for value in stats.values():
            value.clear()
        captured.clear()
    return {"status": "ok"}


@app.post("/test/music")
def music(body: dict):
    app_state.music_service.streaming = bool(body.get("streaming", False))
    return {"streaming": app_state.music_service.streaming}


@app.post("/test/mute")
def mute(body: dict):
    app_state._speaker_muted = bool(body.get("muted", True))
    return {"muted": app_state._speaker_muted}


@app.post("/test/stop")
def stop():
    announcer.stop()
    svc.stop()
    return {"status": "ok"}


class HTTPBridge(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def forward(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))

        async def dispatch():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://hal.test") as client:
                return await client.request(self.command, self.path, content=body,
                                            headers={"Content-Type": "application/json"})
        response = asyncio.run(dispatch())
        self.send_response(response.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response.content)))
        self.end_headers()
        self.wfile.write(response.content)

    do_GET = forward
    do_POST = forward


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), HTTPBridge)
    print(json.dumps({"port": server.server_port, "wav_path": str(wav_path)}), flush=True)
    server.serve_forever()
