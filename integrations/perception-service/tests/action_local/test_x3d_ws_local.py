"""Tests for the action-analysis WebSocket endpoint."""

import asyncio
import base64
import json
import os
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.perception.action.perception import ActionPerception
from dlserver.utils.state import get_action_model, set_action_model

TEST_API_KEY = "test-secret-key"
os.environ["DL_API_KEY"] = TEST_API_KEY

def _make_frame_b64(width: int = 320, height: int = 240) -> str:
    """Create a base64-encoded JPEG of a random BGR image."""
    frame = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


@pytest.fixture(scope="session")
def model():
    """Load the real X3DActionRecognizer once for the entire test session."""
    from core.enums import HumanActionRecognizerEnum
    from core.perception.action.utils import ActionRecognizerFactory

    factory = ActionRecognizerFactory(model_name=HumanActionRecognizerEnum.X3D)
    model = ActionPerception(action_recognizer_factory=factory)
    asyncio.run(model.start())
    return model


@pytest.fixture()
def client(model):
    """Create a TestClient with the real recognizer."""
    import config
    import server

    config.settings.dl_api_key = TEST_API_KEY
    set_action_model(model)

    return TestClient(server.app)


AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}


class TestApiKeyAuth:
    def test_health_without_key_returns_401(self, client):
        resp = client.get("/hal/api/dl/health")
        assert resp.status_code == 401

    def test_health_with_wrong_key_returns_401(self, client):
        resp = client.get("/hal/api/dl/health", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401

    def test_health_with_valid_key(self, client):
        resp = client.get("/hal/api/dl/health", headers=AUTH_HEADERS)
        assert resp.status_code == 200


class TestHealthEndpoint:
    def test_health_ok(self, client):
        resp = client.get("/hal/api/dl/health", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["models"]["action"] is True

    def test_health_not_loaded(self, client):

        saved = get_action_model()
        set_action_model(None)
        resp = client.get("/hal/api/dl/health", headers=AUTH_HEADERS)
        assert resp.json()["models"]["action"] is False
        set_action_model(saved)


class TestActionAnalysisWebSocket:
    def test_frame_returns_detected_classes(self, client):
        frame_b64 = _make_frame_b64()
        with client.websocket_connect("/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "frame", "task": "action", "frame_b64": frame_b64}))
            resp = ws.receive_json()
            assert "detected_classes" in resp
            assert isinstance(resp["detected_classes"], list)

    def test_multiple_frames(self, client):
        """Sending multiple frames should each produce a response."""
        with client.websocket_connect("/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS) as ws:
            for _ in range(3):
                ws.send_text(
                    json.dumps({"type": "frame", "task": "action", "frame_b64": _make_frame_b64()})
                )
                resp = ws.receive_json()
                assert "detected_classes" in resp

    def test_whitelist_update(self, client):
        with client.websocket_connect("/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS) as ws:
            ws.send_text(
                json.dumps(
                    {"type": "config", "task": "action", "whitelist": ["walking", "running"]}
                )
            )
            resp = ws.receive_json()
            assert resp["status"] == "config_updated"

    def test_threshold_update(self, client):
        with client.websocket_connect("/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "config", "task": "action", "threshold": 0.2}))
            resp = ws.receive_json()
            assert resp["status"] == "config_updated"

    def test_whitelist_reset(self, client):
        with client.websocket_connect("/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "config", "task": "action", "whitelist": None}))
            resp = ws.receive_json()
            assert resp["status"] == "config_updated"

    def test_whitelist_then_frame(self, client):
        """Set a whitelist, then send a frame — response classes should be from whitelist."""
        allowed = {"applauding", "clapping"}
        with client.websocket_connect("/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS) as ws:
            ws.send_text(
                json.dumps({"type": "config", "task": "action", "whitelist": list(allowed)})
            )
            resp = ws.receive_json()
            assert resp["status"] == "config_updated"

            ws.send_text(
                json.dumps({"type": "frame", "task": "action", "frame_b64": _make_frame_b64()})
            )
            resp = ws.receive_json()
            assert "detected_classes" in resp
            for det in resp["detected_classes"]:
                assert det["class_name"] in allowed

            ws.send_text(json.dumps({"type": "config", "task": "action", "whitelist": None}))
            ws.receive_json()

    def test_invalid_json(self, client):
        with client.websocket_connect("/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS) as ws:
            ws.send_text("not json at all")
            resp = ws.receive_json()
            assert "error" in resp

    def test_missing_type_field(self, client):
        with client.websocket_connect("/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS) as ws:
            ws.send_text(json.dumps({"frame_b64": "abc"}))
            resp = ws.receive_json()
            assert "error" in resp

    def test_unknown_type(self, client):
        with client.websocket_connect("/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "bogus"}))
            resp = ws.receive_json()
            assert "error" in resp

    def test_frame_missing_frame_b64(self, client):
        with client.websocket_connect("/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "frame"}))
            resp = ws.receive_json()
            assert "error" in resp

    def test_recognizer_not_loaded_closes_ws(self, client):

        saved = get_action_model()
        set_action_model(None)
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS
            ) as ws:
                ws.send_text(json.dumps({"type": "frame", "task": "action", "frame_b64": "abc"}))
                ws.receive_json()
        set_action_model(saved)

    def test_heartbeat_returns_ok(self, client):
        with client.websocket_connect("/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "heartbeat", "task": "action"}))
            resp = ws.receive_json()
            assert resp == {"status": "ok"}

    def test_heartbeat_multiple(self, client):
        """Multiple heartbeats in a row should all return ok."""
        with client.websocket_connect("/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS) as ws:
            for _ in range(3):
                ws.send_text(json.dumps({"type": "heartbeat", "task": "action"}))
                resp = ws.receive_json()
                assert resp == {"status": "ok"}

    def test_heartbeat_interleaved_with_frames(self, client):
        """Heartbeat should work between frame requests."""
        with client.websocket_connect("/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS) as ws:
            ws.send_text(
                json.dumps({"type": "frame", "task": "action", "frame_b64": _make_frame_b64()})
            )
            ws.receive_json()

            ws.send_text(json.dumps({"type": "heartbeat", "task": "action"}))
            resp = ws.receive_json()
            assert resp == {"status": "ok"}

            ws.send_text(
                json.dumps({"type": "frame", "task": "action", "frame_b64": _make_frame_b64()})
            )
            resp = ws.receive_json()
            assert "detected_classes" in resp

    def test_ws_without_api_key_rejected(self, client):
        with pytest.raises(Exception):
            with client.websocket_connect("/hal/api/dl/action-analysis/ws") as ws:
                ws.send_text(json.dumps({"type": "config", "task": "action", "whitelist": None}))
                ws.receive_json()


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "images"


def _load_image_b64(path: Path) -> str:
    """Read an image from disk and return its base64-encoded JPEG string."""
    frame = cv2.imread(str(path))
    if frame is None:
        pytest.skip(f"Could not load image: {path}")
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


@pytest.fixture(scope="session")
def person_drinking_b64() -> str:
    return _load_image_b64(FIXTURES_DIR / "person_drinking.jpg")


class TestActionPerformance:
    def test_drinking_action_detected(self, client, person_drinking_b64: str) -> None:
        """Send 8 frames of a person drinking and assert a drinking-related action is detected."""
        with client.websocket_connect(
            "/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "config",
                        "task": "action",
                        "whitelist": [
                            "drinking",
                            "drinking beer",
                            "drinking shots",
                            "tasting beer",
                        ],
                        "threshold": 0.1,
                    }
                )
            )
            ws.receive_json()

            last_resp: dict = {}
            for _ in range(16):
                ws.send_text(
                    json.dumps(
                        {
                            "type": "frame",
                            "task": "action",
                            "frame_b64": person_drinking_b64,
                        }
                    )
                )
                last_resp = ws.receive_json()

            assert "detected_classes" in last_resp
            detected_names = [
                det["class_name"] for det in last_resp["detected_classes"]
            ]
            assert len(detected_names) > 0, (
                f"Expected at least one drinking-related action, got empty. Full response: {last_resp}"
            )
