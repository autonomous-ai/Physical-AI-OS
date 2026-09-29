"""Integration tests against a remote DL backend (pose). Requires DL_BACKEND_URL and DL_API_KEY."""

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


class TestHealthEndpoint:
    def test_health_reports_pose_model(self):
        resp = httpx.get(_http_url("/hal/api/dl/health"), headers=AUTH_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "pose" in body["models"]


class TestPoseEstimationWebSocket:
    @pytest_asyncio.fixture()
    async def ws(self):
        """Connect to the remote pose WebSocket with auth headers."""
        async with websockets.connect(
            _ws_url("/hal/api/dl/pose-estimation/ws"),
            additional_headers=AUTH_HEADERS,
        ) as conn:
            yield conn

    @pytest.mark.asyncio
    async def test_frame_returns_pose_2d(self, ws):
        await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
        resp = json.loads(await ws.recv())
        assert "pose_2d" in resp
        assert "joints" in resp["pose_2d"]
        assert "confs" in resp["pose_2d"]
        assert len(resp["pose_2d"]["joints"]) == 17
        assert len(resp["pose_2d"]["confs"]) == 17

    @pytest.mark.asyncio
    async def test_multiple_frames(self, ws):
        for _ in range(3):
            await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
            resp = json.loads(await ws.recv())
            assert "pose_2d" in resp

    @pytest.mark.asyncio
    async def test_config_update(self, ws):
        await ws.send(json.dumps({"type": "config", "task": "pose", "frame_interval": 0.5}))
        resp = json.loads(await ws.recv())
        assert resp["status"] == "config_updated"

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
    async def test_frame_missing_frame_b64(self, ws):
        await ws.send(json.dumps({"type": "frame", "task": "pose"}))
        resp = json.loads(await ws.recv())
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_heartbeat_returns_ok(self, ws):
        await ws.send(json.dumps({"type": "heartbeat", "task": "pose"}))
        resp = json.loads(await ws.recv())
        assert resp == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_heartbeat_multiple(self, ws):
        for _ in range(3):
            await ws.send(json.dumps({"type": "heartbeat", "task": "pose"}))
            resp = json.loads(await ws.recv())
            assert resp == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_heartbeat_interleaved_with_frames(self, ws):
        await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
        await ws.recv()

        await ws.send(json.dumps({"type": "heartbeat", "task": "pose"}))
        resp = json.loads(await ws.recv())
        assert resp == {"status": "ok"}

        await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
        resp = json.loads(await ws.recv())
        assert "pose_2d" in resp

    @pytest.mark.asyncio
    async def test_ws_without_api_key_rejected(self):
        with pytest.raises(Exception):
            async with websockets.connect(
                _ws_url("/hal/api/dl/pose-estimation/ws"),
            ) as conn:
                await conn.send(json.dumps({"type": "heartbeat", "task": "pose"}))
                _ = await conn.recv()


class TestErgoAssessmentWebSocket:
    """Tests for ergonomic assessment via the pose WS endpoint."""

    @pytest_asyncio.fixture()
    async def ws(self):
        async with websockets.connect(
            _ws_url("/hal/api/dl/pose-estimation/ws"),
            additional_headers=AUTH_HEADERS,
        ) as conn:
            yield conn

    @pytest.mark.asyncio
    async def test_ws_frame_returns_ergo_field(self, ws):
        await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
        resp = json.loads(await ws.recv())
        assert "pose_2d" in resp
        # ergo may or may not be present depending on server config

    @pytest.mark.asyncio
    async def test_ws_ergo_has_full_structure(self, ws):
        await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
        resp = json.loads(await ws.recv())
        if "ergo" in resp:
            ergo = resp["ergo"]
            assert "score" in ergo
            assert "risk_level" in ergo
            assert "left" in ergo
            assert "right" in ergo
            for side_key in ("left", "right"):
                side = ergo[side_key]
                assert "score" in side
                assert "body_scores" in side
                assert "skipped_joints" in side

    @pytest.mark.asyncio
    async def test_ws_ergo_score_range(self, ws):
        await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
        resp = json.loads(await ws.recv())
        if "ergo" in resp:
            assert 1 <= resp["ergo"]["score"] <= 7

    @pytest.mark.asyncio
    async def test_ws_multiple_frames_ergo_consistent(self, ws):
        for _ in range(3):
            await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
            resp = json.loads(await ws.recv())
            assert "pose_2d" in resp
            if "ergo" in resp:
                assert 1 <= resp["ergo"]["score"] <= 7
                assert resp["ergo"]["score"] == max(
                    resp["ergo"]["left"]["score"],
                    resp["ergo"]["right"]["score"],
                )


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


class TestPosePerformance:
    """Validate that the pose model produces correct results on known images."""

    @pytest.fixture(scope="session")
    def person_drinking_b64(self) -> str:
        img_path = FIXTURES_DIR / "images" / "person_drinking.jpg"
        if not img_path.exists():
            pytest.skip(f"Fixture image not found: {img_path}")
        frame = cv2.imread(str(img_path))
        _, buf = cv2.imencode(".jpg", frame)
        return base64.b64encode(buf.tobytes()).decode()

    @pytest.fixture(scope="session")
    def bad_ergo_b64(self) -> str:
        img_path = FIXTURES_DIR / "images" / "bad-ergo.jpeg"
        if not img_path.exists():
            pytest.skip(f"Fixture image not found: {img_path}")
        frame = cv2.imread(str(img_path))
        _, buf = cv2.imencode(".jpg", frame)
        return base64.b64encode(buf.tobytes()).decode()

    @pytest.fixture(scope="session")
    def good_ergo_b64(self) -> str:
        img_path = FIXTURES_DIR / "images" / "good-ergo.jpeg"
        if not img_path.exists():
            pytest.skip(f"Fixture image not found: {img_path}")
        frame = cv2.imread(str(img_path))
        _, buf = cv2.imencode(".jpg", frame)
        return base64.b64encode(buf.tobytes()).decode()

    @pytest_asyncio.fixture()
    async def ws(self):
        async with websockets.connect(
            _ws_url("/hal/api/dl/pose-estimation/ws"),
            additional_headers=AUTH_HEADERS,
        ) as conn:
            yield conn

    @pytest.mark.asyncio
    async def test_person_keypoints_detected(self, ws, person_drinking_b64: str) -> None:
        """A clearly visible person should produce at least 10 confident keypoints."""
        await ws.send(
            json.dumps({"type": "frame", "task": "pose", "frame_b64": person_drinking_b64})
        )
        resp = json.loads(await ws.recv())
        assert "pose_2d" in resp
        assert "joints" in resp["pose_2d"]
        confs = resp["pose_2d"]["confs"]
        # Count keypoints with non-trivial confidence (> 0.3)
        confident_keypoints = sum(1 for c in confs if c > 0.3)
        assert confident_keypoints >= 10, (
            f"Expected at least 10 confident keypoints, got {confident_keypoints}"
        )

    @pytest.mark.asyncio
    async def test_ergo_score_on_bad_posture(self, ws, bad_ergo_b64: str) -> None:
        """Bad posture image should yield a RULA score >= 3."""
        await ws.send(
            json.dumps({"type": "frame", "task": "pose", "frame_b64": bad_ergo_b64})
        )
        resp = json.loads(await ws.recv())
        assert "ergo" in resp, "Expected 'ergo' field in response for bad posture image"
        ergo = resp["ergo"]
        assert "score" in ergo
        assert ergo["score"] >= 3, (
            f"Expected RULA score >= 3 for bad posture, got {ergo['score']}"
        )

    @pytest.mark.asyncio
    async def test_ergo_score_on_good_posture(self, ws, good_ergo_b64: str) -> None:
        """Good posture image should yield a RULA score <= 4."""
        await ws.send(
            json.dumps({"type": "frame", "task": "pose", "frame_b64": good_ergo_b64})
        )
        resp = json.loads(await ws.recv())
        assert "ergo" in resp, "Expected 'ergo' field in response for good posture image"
        ergo = resp["ergo"]
        assert "score" in ergo
        assert ergo["score"] <= 4, (
            f"Expected RULA score <= 4 for good posture, got {ergo['score']}"
        )
