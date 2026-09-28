"""Tests for the pose-estimation WebSocket + HTTP endpoints using the local RTMPose model."""

import asyncio
import base64
import json
import os
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.enums.files import ModelEnum
from core.perception.pose.perception import PosePerception
from core.perception.pose.utils import PoseEstimator2DFactory
from core.utils.files import get_default_model_path
from dlserver.utils.state import get_pose_model, set_pose_model

TEST_API_KEY = "test-secret-key"
os.environ["DL_API_KEY"] = TEST_API_KEY

RTMPOSE_MODEL_PATH = get_default_model_path(ModelEnum.RTMPOSE_M_ONNX)

pytestmark = pytest.mark.skipif(
    RTMPOSE_MODEL_PATH is None,
    reason="Model enum not found in CDN_PATHS",
)


def _make_frame_b64(width: int = 320, height: int = 240) -> str:
    """Create a base64-encoded JPEG of a random BGR image."""
    frame = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


def _make_person_frame_b64(width: int = 320, height: int = 240) -> str:
    """Create a base64-encoded JPEG with a rough person-like silhouette."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    center_x = width // 2
    # Head
    cv2.circle(frame, (center_x, 40), 20, (200, 180, 170), -1)
    # Torso
    cv2.rectangle(frame, (center_x - 30, 60), (center_x + 30, 150), (150, 150, 160), -1)
    # Legs
    cv2.rectangle(frame, (center_x - 25, 150), (center_x - 5, 230), (140, 140, 150), -1)
    cv2.rectangle(frame, (center_x + 5, 150), (center_x + 25, 230), (140, 140, 150), -1)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


@pytest.fixture(scope="session")
def model():
    """Load the real RTMPose model once for the entire test session."""
    from core.enums.pose import PoseEstimator2DEnum

    factory = PoseEstimator2DFactory(
        model_name=PoseEstimator2DEnum.RTMPOSE, model_path=RTMPOSE_MODEL_PATH
    )
    pose_model = PosePerception(estimator_2d_factory=factory)
    asyncio.run(pose_model.start())
    return pose_model


@pytest.fixture()
def client(model):
    """Create a TestClient with the real pose model."""
    import config
    import server

    config.settings.dl_api_key = TEST_API_KEY
    set_pose_model(model)
    return TestClient(server.app)


AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}


class TestHealthEndpoint:
    def test_health_reports_pose_model(self, client):
        resp = client.get("/hal/api/dl/health", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["models"]["pose"] is True

    def test_health_pose_not_loaded(self, client):
        saved = get_pose_model()
        set_pose_model(None)
        resp = client.get("/hal/api/dl/health", headers=AUTH_HEADERS)
        assert resp.json()["models"]["pose"] is False
        set_pose_model(saved)


class TestPoseEstimationWebSocket:
    def test_frame_returns_pose_2d(self, client):
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()})
            )
            resp = ws.receive_json()
            assert "pose_2d" in resp
            assert "joints" in resp["pose_2d"]
            assert "confs" in resp["pose_2d"]
            assert len(resp["pose_2d"]["joints"]) == 17
            assert len(resp["pose_2d"]["confs"]) == 17

    def test_frame_joints_have_xy(self, client):
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_person_frame_b64()})
            )
            resp = ws.receive_json()
            assert "pose_2d" in resp
            for joint in resp["pose_2d"]["joints"]:
                assert len(joint) == 2
                assert isinstance(joint[0], float)
                assert isinstance(joint[1], float)

    def test_multiple_frames(self, client):
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            for _ in range(3):
                ws.send_text(
                    json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()})
                )
                resp = ws.receive_json()
                assert "pose_2d" in resp

    def test_config_update_frame_interval(self, client):
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({"type": "config", "task": "pose", "frame_interval": 0.5}))
            resp = ws.receive_json()
            assert resp["status"] == "config_updated"

    def test_invalid_json(self, client):
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text("not json at all")
            resp = ws.receive_json()
            assert "error" in resp

    def test_missing_type_field(self, client):
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({"frame_b64": "abc"}))
            resp = ws.receive_json()
            assert "error" in resp

    def test_frame_missing_frame_b64(self, client):
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({"type": "frame", "task": "pose"}))
            resp = ws.receive_json()
            assert "error" in resp

    def test_model_not_loaded_closes_ws(self, client):
        saved = get_pose_model()
        set_pose_model(None)
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
            ) as ws:
                ws.send_text(json.dumps({"type": "frame", "task": "pose", "frame_b64": "abc"}))
                ws.receive_json()
        set_pose_model(saved)

    def test_heartbeat_returns_ok(self, client):
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({"type": "heartbeat", "task": "pose"}))
            resp = ws.receive_json()
            assert resp == {"status": "ok"}

    def test_heartbeat_multiple(self, client):
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            for _ in range(3):
                ws.send_text(json.dumps({"type": "heartbeat", "task": "pose"}))
                resp = ws.receive_json()
                assert resp == {"status": "ok"}

    def test_heartbeat_interleaved_with_frames(self, client):
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()})
            )
            ws.receive_json()

            ws.send_text(json.dumps({"type": "heartbeat", "task": "pose"}))
            resp = ws.receive_json()
            assert resp == {"status": "ok"}

            ws.send_text(
                json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()})
            )
            resp = ws.receive_json()
            assert "pose_2d" in resp

    def test_ws_without_api_key_rejected(self, client):
        with pytest.raises(Exception):
            with client.websocket_connect("/hal/api/dl/pose-estimation/ws") as ws:
                ws.send_text(json.dumps({"type": "heartbeat", "task": "pose"}))
                ws.receive_json()


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "images"


def _load_image_b64(path: Path) -> str:
    """Read an image from disk and return its base64-encoded JPEG string."""
    frame = cv2.imread(str(path))
    assert frame is not None, f"Could not load image: {path}"
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


@pytest.fixture(scope="session")
def person_frame_b64() -> str:
    return _load_image_b64(FIXTURES_DIR / "person_drinking.jpg")


class TestPosePerformance:
    def test_person_keypoints_detected(self, client, person_frame_b64: str) -> None:
        """Send a real person image and verify keypoint quality."""
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "frame",
                        "task": "pose",
                        "frame_b64": person_frame_b64,
                    }
                )
            )
            resp = ws.receive_json()

            assert "pose_2d" in resp
            joints = resp["pose_2d"]["joints"]
            confs = resp["pose_2d"]["confs"]

            assert len(joints) == 17, f"Expected 17 keypoints, got {len(joints)}"

            high_conf_count = sum(1 for c in confs if c > 0.3)
            assert high_conf_count >= 10, (
                f"Expected at least 10 keypoints with confidence > 0.3, "
                f"got {high_conf_count} (confs: {confs})"
            )
