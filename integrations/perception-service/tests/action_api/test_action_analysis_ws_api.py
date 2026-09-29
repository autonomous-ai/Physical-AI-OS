"""Integration tests against a remote DL backend (action). Requires DL_BACKEND_URL and DL_API_KEY."""

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


def _http_url(path: str) -> str:
    return f"{DL_BACKEND_URL}{path}"


def _ws_url(path: str) -> str:
    return DL_BACKEND_URL.replace("http://", "ws://").replace("https://", "wss://") + path


AUTH_HEADERS = {"X-API-Key": DL_API_KEY}


class TestApiKeyAuth:
    @pytest.mark.skipif(not DL_API_KEY, reason="DL_API_KEY not set — skipping auth tests")
    def test_health_without_key_returns_401(self):
        resp = httpx.get(_http_url("/hal/api/dl/health"))
        assert resp.status_code == 401

    @pytest.mark.skipif(not DL_API_KEY, reason="DL_API_KEY not set — skipping auth tests")
    def test_health_with_wrong_key_returns_401(self):
        resp = httpx.get(_http_url("/hal/api/dl/health"), headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401

    def test_health_with_valid_key(self):
        resp = httpx.get(_http_url("/hal/api/dl/health"), headers=AUTH_HEADERS)
        assert resp.status_code == 200


class TestHealthEndpoint:
    def test_health_ok(self):
        resp = httpx.get(_http_url("/hal/api/dl/health"), headers=AUTH_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["models"]["action"] is True


class TestActionAnalysisWebSocket:
    @pytest_asyncio.fixture()
    async def ws(self):
        """Connect to the remote WebSocket with auth headers."""
        async with websockets.connect(
            _ws_url("/hal/api/dl/action-analysis/ws"),
            additional_headers=AUTH_HEADERS,
        ) as conn:
            yield conn

    @pytest.mark.asyncio
    async def test_frame_returns_detected_classes(self, ws):
        await ws.send(
            json.dumps({"type": "frame", "task": "action", "frame_b64": _make_frame_b64()})
        )
        resp = json.loads(await ws.recv())
        assert "detected_classes" in resp
        assert isinstance(resp["detected_classes"], list)

    @pytest.mark.asyncio
    async def test_multiple_frames(self, ws):
        for _ in range(3):
            await ws.send(
                json.dumps({"type": "frame", "task": "action", "frame_b64": _make_frame_b64()})
            )
            resp = json.loads(await ws.recv())
            assert "detected_classes" in resp

    @pytest.mark.asyncio
    async def test_whitelist_update(self, ws):
        await ws.send(
            json.dumps(
                {"type": "config", "task": "action", "whitelist": ["applauding", "clapping"]}
            )
        )
        resp = json.loads(await ws.recv())
        assert resp["status"] == "config_updated"

    @pytest.mark.asyncio
    async def test_whitelist_reset(self, ws):
        await ws.send(json.dumps({"type": "config", "task": "action", "whitelist": None}))
        resp = json.loads(await ws.recv())
        assert resp["status"] == "config_updated"

    @pytest.mark.asyncio
    async def test_whitelist_then_frame(self, ws):
        allowed = {"applauding", "clapping"}
        await ws.send(json.dumps({"type": "config", "task": "action", "whitelist": list(allowed)}))
        resp = json.loads(await ws.recv())
        assert resp["status"] == "config_updated"

        await ws.send(
            json.dumps({"type": "frame", "task": "action", "frame_b64": _make_frame_b64()})
        )
        resp = json.loads(await ws.recv())
        assert "detected_classes" in resp
        for det in resp["detected_classes"]:
            assert det["class_name"] in allowed

        await ws.send(json.dumps({"type": "config", "task": "action", "whitelist": None}))
        await ws.recv()

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
        await ws.send(json.dumps({"type": "frame"}))
        resp = json.loads(await ws.recv())
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_heartbeat_returns_ok(self, ws):
        await ws.send(json.dumps({"type": "heartbeat", "task": "action"}))
        resp = json.loads(await ws.recv())
        assert resp == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_heartbeat_multiple(self, ws):
        """Multiple heartbeats in a row should all return ok."""
        for _ in range(3):
            await ws.send(json.dumps({"type": "heartbeat", "task": "action"}))
            resp = json.loads(await ws.recv())
            assert resp == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_heartbeat_interleaved_with_frames(self, ws):
        """Heartbeat should work between frame requests."""
        await ws.send(
            json.dumps({"type": "frame", "task": "action", "frame_b64": _make_frame_b64()})
        )
        await ws.recv()

        await ws.send(json.dumps({"type": "heartbeat", "task": "action"}))
        resp = json.loads(await ws.recv())
        assert resp == {"status": "ok"}

        await ws.send(
            json.dumps({"type": "frame", "task": "action", "frame_b64": _make_frame_b64()})
        )
        resp = json.loads(await ws.recv())
        assert "detected_classes" in resp

    @pytest.mark.asyncio
    async def test_ws_without_api_key_rejected(self):
        with pytest.raises(Exception):
            async with websockets.connect(
                _ws_url("/hal/api/dl/action-analysis/ws"),
            ) as conn:
                await conn.send(json.dumps({"type": "config", "task": "action", "whitelist": None}))
                _ = await conn.recv()


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _extract_frames_b64(video_path: Path, max_frames: int = 16) -> list[str]:
    """Extract up to *max_frames* frames from a GIF / WebP / video file and return as base64 JPEGs."""
    cap = cv2.VideoCapture(str(video_path))
    frames: list[str] = []
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        _, buf = cv2.imencode(".jpg", frame)
        frames.append(base64.b64encode(buf.tobytes()).decode())
    cap.release()
    interval = max(len(frames) // max_frames, 1)
    return frames[0 : len(frames) : interval]


class TestActionPerformance:
    """Validate that the action model correctly identifies actions on known images."""

    @pytest.fixture(scope="session")
    def person_drinking_b64(self) -> str:
        img_path = FIXTURES_DIR / "images" / "person_drinking.jpg"
        if not img_path.exists():
            pytest.skip(f"Fixture image not found: {img_path}")
        frame = cv2.imread(str(img_path))
        _, buf = cv2.imencode(".jpg", frame)
        return base64.b64encode(buf.tobytes()).decode()

    @pytest.fixture(scope="session")
    def drinking_gif_frames(self) -> list[str]:
        vid_path = FIXTURES_DIR / "images" / "drinking.mp4"
        if not vid_path.exists():
            pytest.skip(f"Fixture video not found: {vid_path}")
        frames = _extract_frames_b64(vid_path, max_frames=16)
        if not frames:
            pytest.skip("Could not extract frames from drinking.mp4")
        return frames

    @pytest.fixture(scope="session")
    def eating_gif_frames(self) -> list[str]:
        vid_path = FIXTURES_DIR / "images" / "eating.mp4"
        if not vid_path.exists():
            pytest.skip(f"Fixture video not found: {vid_path}")
        frames = _extract_frames_b64(vid_path, max_frames=16)
        if not frames:
            pytest.skip("Could not extract frames from eating.mp4")
        return frames

    @pytest_asyncio.fixture()
    async def ws(self):
        async with websockets.connect(
            _ws_url("/hal/api/dl/action-analysis/ws"),
            additional_headers=AUTH_HEADERS,
        ) as conn:
            yield conn

    @pytest.mark.asyncio
    async def test_drinking_gif_action_detected(self, ws, drinking_gif_frames: list[str]) -> None:
        """Send frames extracted from drinking.webp and assert 'drinking' is detected."""
        resp = None
        frames = drinking_gif_frames
        while len(frames) < 16:
            frames = frames + drinking_gif_frames

        await ws.send(json.dumps({
            "type": "config",
            "task": "action",
            "threshold": 0.1,
        }))
        await ws.recv()
        for frame_b64 in frames[:16]:
            await ws.send(json.dumps({"type": "frame", "task": "action", "frame_b64": frame_b64}))
            resp = json.loads(await ws.recv())
        assert resp is not None
        assert "detected_classes" in resp
        class_names = [d["class_name"].lower() for d in resp["detected_classes"]]
        assert any("drinking" in name for name in class_names), (
            f"Expected 'drinking' in detected classes from webp, got {class_names}"
        )

    @pytest.mark.asyncio
    async def test_eating_gif_action_detected(self, ws, eating_gif_frames: list[str]) -> None:
        """Send frames extracted from eating.gif and assert 'eating' is detected."""
        resp = None
        frames = eating_gif_frames
        while len(frames) < 16:
            frames = frames + eating_gif_frames

        await ws.send(json.dumps({
            "type": "config",
            "task": "action",
            "threshold": 0.1,
        }))
        await ws.recv()
        for frame_b64 in frames[:16]:
            await ws.send(json.dumps({"type": "frame", "task": "action", "frame_b64": frame_b64}))
            resp = json.loads(await ws.recv())

        assert resp is not None
        assert "detected_classes" in resp
        class_names = [d["class_name"].lower() for d in resp["detected_classes"]]
        assert any("eating" in name for name in class_names), (
            f"Expected 'eating' in detected classes from gif, got {class_names}"
        )
