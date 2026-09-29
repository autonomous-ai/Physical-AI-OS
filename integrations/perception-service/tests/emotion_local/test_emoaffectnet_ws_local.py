"""Emotion endpoint tests with local Emo-AffectNet; skipped until exported (`uv run export-emoaffectnet`)."""

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
from core.perception.face.utils import FaceDetectorFactory
from core.perception.facial_emotion.constants import RESOURCES_DIR
from core.perception.facial_emotion.perception import EmotionPerception
from core.perception.facial_emotion.utils import EmotionRecognizerFactory
from core.utils.files import get_default_model_path
from dlserver.utils.state import get_emotion_model, set_emotion_model

EMOAFFECTNET_EMOTIONS: list[str] = (
    (RESOURCES_DIR / "emoaffectnet_classes.txt").read_text().strip().split("\n")
)

TEST_API_KEY = "test-secret-key"
os.environ["DL_API_KEY"] = TEST_API_KEY
os.environ["EMOTION_RECOGNITION_MODEL"] = "emoaffectnet"

EMOAFFECTNET_MODEL_PATH = get_default_model_path(ModelEnum.EMOAFFECTNET_ONNX)

pytestmark = pytest.mark.skipif(
    EMOAFFECTNET_MODEL_PATH is None or not EMOAFFECTNET_MODEL_PATH.exists(),
    reason="Emo-AffectNet ONNX not found — run `uv run export-emoaffectnet` first",
)

FIXTURES_DIR: Path = Path(__file__).resolve().parent.parent / "fixtures" / "images"


def _load_image_b64(name: str) -> str:
    img = cv2.imread(str(FIXTURES_DIR / name))
    assert img is not None, f"Failed to load {FIXTURES_DIR / name}"
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return base64.b64encode(buf.tobytes()).decode()


def _make_face_frame_b64(width: int = 320, height: int = 240) -> str:
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    center = (width // 2, height // 2)
    cv2.ellipse(frame, center, (50, 65), 0, 0, 360, (200, 180, 170), -1)
    cv2.circle(frame, (center[0] - 20, center[1] - 15), 5, (40, 40, 40), -1)
    cv2.circle(frame, (center[0] + 20, center[1] - 15), 5, (40, 40, 40), -1)
    cv2.ellipse(frame, (center[0], center[1] + 25), (15, 8), 0, 0, 180, (40, 40, 80), -1)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


@pytest.fixture(scope="session")
def model():
    from core.enums import EmotionRecognizerEnum
    from core.enums.face import FaceDetectorEnum

    emotion_factory = EmotionRecognizerFactory(
        model_name=EmotionRecognizerEnum.EMOAFFECTNET, model_path=EMOAFFECTNET_MODEL_PATH
    )
    face_factory = FaceDetectorFactory(model_name=FaceDetectorEnum.YUNET)
    m = EmotionPerception(
        emotion_recognizer_factory=emotion_factory, face_detector_factory=face_factory
    )
    asyncio.run(m.start())
    return m


@pytest.fixture()
def client(model):
    import config
    import server

    config.settings.dl_api_key = TEST_API_KEY
    set_emotion_model(model)
    return TestClient(server.app)


AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}


class TestEmoAffectNetLabels:
    def test_labels_endpoint_matches_classes_file(self, client):
        resp = client.get("/hal/api/dl/emotion-labels", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["labels"] == EMOAFFECTNET_EMOTIONS


class TestEmoAffectNetWebSocket:
    def test_frame_with_face_returns_emotion_fields(self, client):
        with client.websocket_connect(
            "/hal/api/dl/emotion-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps(
                    {"type": "frame", "task": "emotion", "frame_b64": _make_face_frame_b64()}
                )
            )
            resp = ws.receive_json()
            assert "detections" in resp
            for det in resp["detections"]:
                assert det["emotion"] in EMOAFFECTNET_EMOTIONS
                assert 0.0 <= det["confidence"] <= 1.0
                assert len(det["bbox"]) == 4
                # Emo-AffectNet static model has no valence/arousal head.
                assert det.get("valence") is None
                assert det.get("arousal") is None

    def test_heartbeat_returns_ok(self, client):
        with client.websocket_connect(
            "/hal/api/dl/emotion-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({"type": "heartbeat", "task": "emotion"}))
            assert ws.receive_json() == {"status": "ok"}


class TestEmoAffectNetHTTPRecognize:
    def test_recognize_returns_known_label(self, client):
        face = _make_face_frame_b64()
        resp = client.post(
            "/hal/api/dl/emotion-recognize",
            headers=AUTH_HEADERS,
            json={"image_b64": face, "threshold": 0.0},
        )
        assert resp.status_code == 200
        for det in resp.json()["detections"]:
            assert det["emotion"] in EMOAFFECTNET_EMOTIONS
            assert 0.0 <= det["confidence"] <= 1.0
