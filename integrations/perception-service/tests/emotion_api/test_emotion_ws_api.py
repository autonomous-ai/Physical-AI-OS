"""Integration tests against a remote DL backend (emotion). Requires DL_BACKEND_URL and DL_API_KEY."""

import base64
import json
import os
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
import pytest_asyncio
import websockets
from dotenv import load_dotenv

from core.perception.facial_emotion.constants import RESOURCES_DIR

POSTERV2_EMOTIONS: list[str] = (RESOURCES_DIR / "posterv2_classes.txt").read_text().strip().split("\n")
EMOTIONS_8: list[str] = (RESOURCES_DIR / "emonet_8_classes.txt").read_text().strip().split("\n")
EMOTIONS_5: list[str] = (RESOURCES_DIR / "emonet_5_classes.txt").read_text().strip().split("\n")

_ = load_dotenv(override=True)

DL_BACKEND_URL = os.getenv("DL_BACKEND_URL", "")
DL_API_KEY = os.getenv("DL_API_KEY", "")

pytestmark = pytest.mark.skipif(
    not DL_BACKEND_URL, reason="DL_BACKEND_URL not set — skipping remote API tests"
)


def _make_frame_b64(width: int = 320, height: int = 240) -> str:
    """Create a base64-encoded JPEG of a random BGR image."""
    frame = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


def _make_face_frame_b64(width: int = 320, height: int = 240) -> str:
    """Create a base64-encoded JPEG with a synthetic face-like region."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    center = (width // 2, height // 2)
    axes = (50, 65)
    cv2.ellipse(frame, center, axes, 0, 0, 360, (200, 180, 170), -1)
    cv2.circle(frame, (center[0] - 20, center[1] - 15), 5, (40, 40, 40), -1)
    cv2.circle(frame, (center[0] + 20, center[1] - 15), 5, (40, 40, 40), -1)
    cv2.ellipse(frame, (center[0], center[1] + 25), (15, 8), 0, 0, 180, (40, 40, 80), -1)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


def _http_url(path: str) -> str:
    return f"{DL_BACKEND_URL}{path}"


def _ws_url(path: str) -> str:
    return DL_BACKEND_URL.replace("http://", "ws://").replace("https://", "wss://") + path


AUTH_HEADERS = {"X-API-Key": DL_API_KEY}


class TestHealthEndpoint:
    def test_health_reports_emotion_model(self):
        resp = httpx.get(_http_url("/hal/api/dl/health"), headers=AUTH_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["models"]["emotion"] is True


class TestEmotionAnalysisWebSocket:
    @pytest_asyncio.fixture()
    async def ws(self):
        """Connect to the remote emotion WebSocket with auth headers."""
        async with websockets.connect(
            _ws_url("/hal/api/dl/emotion-analysis/ws"),
            additional_headers=AUTH_HEADERS,
        ) as conn:
            yield conn

    @pytest.mark.asyncio
    async def test_frame_returns_detections(self, ws):
        await ws.send(
            json.dumps({"type": "frame", "task": "emotion", "frame_b64": _make_frame_b64()})
        )
        resp = json.loads(await ws.recv())
        assert "detections" in resp
        assert isinstance(resp["detections"], list)

    @pytest.mark.asyncio
    async def test_frame_with_face_returns_emotion_fields(self, ws):
        await ws.send(
            json.dumps(
                {"type": "frame", "task": "emotion", "frame_b64": _make_face_frame_b64()}
            )
        )
        resp = json.loads(await ws.recv())
        assert "detections" in resp
        for det in resp["detections"]:
            assert "emotion" in det
            assert "confidence" in det
            assert "face_confidence" in det
            assert "bbox" in det
            all_labels = set(POSTERV2_EMOTIONS) | set(EMOTIONS_8) | set(EMOTIONS_5)
            assert det["emotion"] in all_labels
            assert 0.0 <= det["confidence"] <= 1.0
            assert len(det["bbox"]) == 4

    @pytest.mark.asyncio
    async def test_multiple_frames(self, ws):
        for _ in range(3):
            await ws.send(
                json.dumps(
                    {"type": "frame", "task": "emotion", "frame_b64": _make_frame_b64()}
                )
            )
            resp = json.loads(await ws.recv())
            assert "detections" in resp

    @pytest.mark.asyncio
    async def test_config_update_threshold(self, ws):
        await ws.send(
            json.dumps({"type": "config", "task": "emotion", "threshold": 0.8})
        )
        resp = json.loads(await ws.recv())
        assert resp["status"] == "config_updated"

    @pytest.mark.asyncio
    async def test_high_threshold_filters_detections(self, ws):
        await ws.send(
            json.dumps({"type": "config", "task": "emotion", "threshold": 1.0})
        )
        resp = json.loads(await ws.recv())
        assert resp["status"] == "config_updated"

        await ws.send(
            json.dumps(
                {"type": "frame", "task": "emotion", "frame_b64": _make_face_frame_b64()}
            )
        )
        resp = json.loads(await ws.recv())
        assert resp["detections"] == []

    @pytest.mark.asyncio
    async def test_invalid_json(self, ws):
        await ws.send("not json at all")
        resp = json.loads(await ws.recv())
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_missing_type_field(self, ws):
        await ws.send(json.dumps({"frame_b64": "abc"}))
        resp = json.loads(await ws.recv())
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_unknown_type(self, ws):
        await ws.send(json.dumps({"type": "bogus"}))
        resp = json.loads(await ws.recv())
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_frame_missing_frame_b64(self, ws):
        await ws.send(json.dumps({"type": "frame", "task": "emotion"}))
        resp = json.loads(await ws.recv())
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_heartbeat_returns_ok(self, ws):
        await ws.send(json.dumps({"type": "heartbeat", "task": "emotion"}))
        resp = json.loads(await ws.recv())
        assert resp == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_heartbeat_multiple(self, ws):
        """Multiple heartbeats in a row should all return ok."""
        for _ in range(3):
            await ws.send(json.dumps({"type": "heartbeat", "task": "emotion"}))
            resp = json.loads(await ws.recv())
            assert resp == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_heartbeat_interleaved_with_frames(self, ws):
        """Heartbeat should work between frame requests."""
        await ws.send(
            json.dumps({"type": "frame", "task": "emotion", "frame_b64": _make_frame_b64()})
        )
        await ws.recv()

        await ws.send(json.dumps({"type": "heartbeat", "task": "emotion"}))
        resp = json.loads(await ws.recv())
        assert resp == {"status": "ok"}

        await ws.send(
            json.dumps({"type": "frame", "task": "emotion", "frame_b64": _make_frame_b64()})
        )
        resp = json.loads(await ws.recv())
        assert "detections" in resp

    @pytest.mark.asyncio
    async def test_ws_without_api_key_rejected(self):
        with pytest.raises(Exception):
            async with websockets.connect(
                _ws_url("/hal/api/dl/emotion-analysis/ws"),
            ) as conn:
                await conn.send(
                    json.dumps({"type": "config", "task": "emotion", "threshold": 0.5})
                )
                _ = await conn.recv()


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


class TestEmotionPerformance:
    """Validate that the emotion model correctly identifies emotions on known images."""

    @pytest.fixture(scope="session")
    def happy_frame_b64(self) -> str:
        img_path = FIXTURES_DIR / "images" / "happy.jpeg"
        if not img_path.exists():
            pytest.skip(f"Fixture image not found: {img_path}")
        frame = cv2.imread(str(img_path))
        _, buf = cv2.imencode(".jpg", frame)
        return base64.b64encode(buf.tobytes()).decode()

    @pytest.fixture(scope="session")
    def sad_frame_b64(self) -> str:
        img_path = FIXTURES_DIR / "images" / "sad.jpg"
        if not img_path.exists():
            pytest.skip(f"Fixture image not found: {img_path}")
        frame = cv2.imread(str(img_path))
        _, buf = cv2.imencode(".jpg", frame)
        return base64.b64encode(buf.tobytes()).decode()

    @pytest_asyncio.fixture()
    async def ws(self):
        async with websockets.connect(
            _ws_url("/hal/api/dl/emotion-analysis/ws"),
            additional_headers=AUTH_HEADERS,
        ) as conn:
            yield conn

    @pytest.mark.asyncio
    async def test_happy_face_detected(self, ws, happy_frame_b64: str) -> None:
        """Send a known happy face image and assert happiness is detected."""
        await ws.send(json.dumps({"type": "config", "task": "emotion", "threshold": 0.5}))
        await ws.recv()

        await ws.send(
            json.dumps({"type": "frame", "task": "emotion", "frame_b64": happy_frame_b64})
        )
        resp = json.loads(await ws.recv())
        assert "detections" in resp
        assert len(resp["detections"]) > 0, "Expected at least one face detection"
        detected_emotions = [d["emotion"].lower() for d in resp["detections"]]
        assert any(
            e in ("happy", "happiness") for e in detected_emotions
        ), f"Expected 'happy' or 'happiness' in {detected_emotions}"

    @pytest.mark.asyncio
    async def test_sad_face_detected(self, ws, sad_frame_b64: str) -> None:
        """Send a known sad face image and assert sadness is detected."""
        await ws.send(json.dumps({"type": "config", "task": "emotion", "threshold": 0.5}))
        await ws.recv()

        await ws.send(
            json.dumps({"type": "frame", "task": "emotion", "frame_b64": sad_frame_b64})
        )
        resp = json.loads(await ws.recv())
        assert "detections" in resp
        assert len(resp["detections"]) > 0, "Expected at least one face detection"
        detected_emotions = [d["emotion"].lower() for d in resp["detections"]]
        assert any(
            e in ("sad", "sadness") for e in detected_emotions
        ), f"Expected 'sad' or 'sadness' in {detected_emotions}"
