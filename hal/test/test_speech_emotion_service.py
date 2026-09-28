"""End-to-end test of `SpeechEmotionService` on a local machine."""

from __future__ import annotations

import argparse
import http.server
import io
import json
import logging
import os
import sys
import threading
import time
import wave
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test.speech_emotion_service")

SAMPLE_RATE = 16000
CHANNELS = 1
MOCK_OS_HOST = "127.0.0.1"
MOCK_OS_PORT = 5000
DEFAULT_SER_ENDPOINT = "/api/dl/ser/recognize"


class _CapturedPost:
    def __init__(self, path: str, payload: dict):
        self.path = path
        self.payload = payload

    def __repr__(self) -> str:
        return f"<POST {self.path} payload={self.payload}>"


class _MockOSHandler(http.server.BaseHTTPRequestHandler):
    captured: list[_CapturedPost] = []

    def do_POST(self) -> None:  # noqa: N802 (http.server contract)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"_raw": body}
        _MockOSHandler.captured.append(_CapturedPost(self.path, payload))
        logger.info("[mock-lamp] received %s payload=%s", self.path, payload)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":1,"data":null,"message":null}')

    def log_message(self, fmt: str, *args) -> None:
        return


def _start_mock_os() -> http.server.HTTPServer:
    server = http.server.HTTPServer(
        (MOCK_OS_HOST, MOCK_OS_PORT), _MockOSHandler,
    )
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info(
        "Mock os-server listening on http://%s:%d  (override with --sensing-url)",
        MOCK_OS_HOST, MOCK_OS_PORT,
    )
    return server


def record_wav_bytes(duration_s: float, device: int | None) -> bytes:
    import numpy as np
    import sounddevice as sd

    frames = int(duration_s * SAMPLE_RATE)
    logger.info(
        ">>> Recording %.1fs (device=%s) — speak NOW",
        duration_s, device if device is not None else "default",
    )
    pcm = sd.rec(
        frames, samplerate=SAMPLE_RATE, channels=CHANNELS,
        dtype=np.int16, device=device,
    )
    sd.wait()
    logger.info("<<< Recording done (%d samples)", len(pcm))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description="E2E test of SpeechEmotionService")
    parser.add_argument(
        "--dl-backend-url",
        default=os.environ.get("DL_BACKEND_URL", "http://localhost:8008"),
        help="Base URL of the hosted perception-service.",
    )
    parser.add_argument(
        "--api-key", default=os.environ.get("DL_API_KEY", ""),
        help="X-API-Key header (if perception-service requires it).",
    )
    parser.add_argument(
        "--endpoint", default=os.environ.get("DL_SER_ENDPOINT", "") or DEFAULT_SER_ENDPOINT,
        help=f"SER endpoint path. Default {DEFAULT_SER_ENDPOINT} (FastAPI "
             "direct — no nginx prefix).",
    )
    parser.add_argument(
        "--sensing-url", default="",
        help="Override Lamp sensing URL. Default: spin up a local mock on "
             f"http://{MOCK_OS_HOST}:{MOCK_OS_PORT}/api/sensing/event",
    )
    parser.add_argument("--user", default="alice",
                        help="Identifier to attribute each clip to. Use "
                             "'unknown' to exercise the unknown-collapse path.")
    parser.add_argument("--reps", type=int, default=3,
                        help="How many clips to record + submit (default 3).")
    parser.add_argument("--duration", type=float, default=3.0,
                        help="Per-clip duration in seconds (default 3.0).")
    parser.add_argument("--pause", type=float, default=1.0,
                        help="Pause between recordings (default 1s).")
    parser.add_argument("--device", type=int, default=None,
                        help="sounddevice input index (see `python -m sounddevice`).")
    args = parser.parse_args()

    if not args.dl_backend_url:
        print(
            "ERROR: --dl-backend-url is required (or set DL_BACKEND_URL).",
            file=sys.stderr,
        )
        return 2

    from hal import config as _cfg
    _cfg.DL_BACKEND_URL = args.dl_backend_url
    _cfg.DL_API_KEY = args.api_key
    _cfg.DL_SER_ENDPOINT = args.endpoint
    _cfg.SPEECH_EMOTION_API_URL = (
        args.dl_backend_url.rstrip("/") + "/" + args.endpoint.strip("/")
    )
    _cfg.SPEECH_EMOTION_API_KEY = args.api_key
    logger.info("Resolved SER URL: %s", _cfg.SPEECH_EMOTION_API_URL)

    server = None
    if not args.sensing_url:
        server = _start_mock_os()
        args.sensing_url = f"http://{MOCK_OS_HOST}:{MOCK_OS_PORT}/api/sensing/event"
    _cfg.OS_SENSING_URL = args.sensing_url

    from hal.drivers.voice.speech_emotion import SpeechEmotionService

    svc = SpeechEmotionService()
    if not svc.available:
        print(
            "ERROR: SpeechEmotionService reports unavailable. Check "
            "DL_BACKEND_URL + that perception-service is reachable.",
            file=sys.stderr,
        )
        return 3

    logger.info("Service state at start: %s", svc.to_dict())

    for i in range(1, args.reps + 1):
        logger.info("===== clip %d / %d =====", i, args.reps)
        try:
            wav_bytes = record_wav_bytes(args.duration, args.device)
        except Exception:
            logger.exception("recording failed")
            continue
        svc.submit(user=args.user, wav_bytes=wav_bytes, duration_s=args.duration)
        time.sleep(args.pause)

    flush_wait = svc._flush_s + 2.0  # type: ignore[attr-defined]
    logger.info("Waiting %.1fs for flush thread to drain buffer...", flush_wait)
    time.sleep(flush_wait)

    logger.info("Service state at end: %s", svc.to_dict())

    print()
    print("=" * 60)
    print(f"Submitted clips     : {args.reps}")
    print(f"os-server POSTs captured : {len(_MockOSHandler.captured)}")
    for cap in _MockOSHandler.captured:
        print(f"  - {cap.path}  type={cap.payload.get('type')}  "
              f"user={cap.payload.get('current_user')}")
        print(f"    message={cap.payload.get('message')!r}")
    print("=" * 60)
    print()
    print("Tips:")
    print("  - Nothing captured? Check the log for `[speech_emotion] DROP`")
    print("    lines — most likely 'duration < min' (raise --duration) or")
    print("    'all neutral' (try a clearly emotional reading).")
    print("  - dedup drops? Run the service with")
    print("    HAL_SPEECH_EMOTION_DEDUP_WINDOW_S=5 to retest quickly.")

    svc.stop()
    if server is not None:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
