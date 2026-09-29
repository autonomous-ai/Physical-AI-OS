"""Tests for action recognition with the person detector (YOLO) cropping before X3D."""

import asyncio
import base64
import json
import os
from pathlib import Path

import cv2
import cv2.typing as cv2t
import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.models.action import ActionPerceptionSessionConfig
from core.perception.action import ActionPerception
from core.perception.person.predictors import PersonDetector, YOLOPersonDetector
from core.perception.person.utils import PersonDetectorFactory
from dlserver.utils.state import set_action_model

TEST_API_KEY = "test-secret-key"
os.environ["DL_API_KEY"] = TEST_API_KEY

from core.enums.files import ModelEnum
from core.utils.files import get_default_model_path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
PERSON_DRINKING_IMG = FIXTURES_DIR / "images" / "person_drinking.jpg"
X3D_MODEL_PATH = get_default_model_path(ModelEnum.X3D_ONNX)

pytestmark = pytest.mark.skipif(
    X3D_MODEL_PATH is None,
    reason="Model enum not found in CDN_PATHS",
)


@pytest.fixture(scope="session")
def person_frame() -> cv2t.MatLike:
    """Load the person drinking image once for the entire test session."""
    frame = cv2.imread(str(PERSON_DRINKING_IMG))
    assert frame is not None, f"Failed to load {PERSON_DRINKING_IMG}"
    return frame


@pytest.fixture(scope="session")
def person_frame_b64() -> str:
    """Encode person drinking image as base64 once."""
    return base64.b64encode(PERSON_DRINKING_IMG.read_bytes()).decode()


@pytest.fixture(scope="session")
def empty_frame_b64() -> str:
    """Black frame with no person — person detector should return empty."""
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


@pytest.fixture(scope="session")
def person_detector() -> PersonDetector:
    det = YOLOPersonDetector(model_path="yolo11n.pt")
    det.start()
    return det


@pytest.fixture(scope="session")
def model_with_detector() -> ActionPerception:
    from core.enums import HumanActionRecognizerEnum
    from core.enums.person import PersonDetectorEnum
    from core.perception.action.utils import ActionRecognizerFactory

    action_factory = ActionRecognizerFactory(
        model_name=HumanActionRecognizerEnum.X3D, model_path=X3D_MODEL_PATH
    )
    person_factory = PersonDetectorFactory(
        model_name=PersonDetectorEnum.YOLO, model_path="yolo11n.pt"
    )
    m = ActionPerception(
        action_recognizer_factory=action_factory,
        person_detector_factory=person_factory,
        default_config=ActionPerceptionSessionConfig(frame_interval=0),
    )
    asyncio.run(m.start())
    return m


@pytest.fixture(scope="session")
def model_without_detector() -> ActionPerception:
    from core.enums import HumanActionRecognizerEnum
    from core.perception.action.utils import ActionRecognizerFactory

    factory = ActionRecognizerFactory(
        model_name=HumanActionRecognizerEnum.X3D, model_path=X3D_MODEL_PATH
    )
    m = ActionPerception(
        action_recognizer_factory=factory,
        default_config=ActionPerceptionSessionConfig(frame_interval=0),
    )
    asyncio.run(m.start())
    return m


@pytest.fixture()
def client_with_detector(model_with_detector: ActionPerception) -> TestClient:
    import config
    import server

    config.settings.dl_api_key = TEST_API_KEY
    set_action_model(model_with_detector)
    return TestClient(server.app)


@pytest.fixture()
def client_without_detector(model_without_detector: ActionPerception) -> TestClient:
    import config
    import server

    config.settings.dl_api_key = TEST_API_KEY
    set_action_model(model_without_detector)
    return TestClient(server.app)


AUTH_HEADERS: dict[str, str] = {"X-API-Key": TEST_API_KEY}


class TestPersonDetector:
    def test_detect_person_in_image(
        self, person_detector: PersonDetector, person_frame: cv2t.MatLike,
    ) -> None:
        """Should detect at least one person (normalized bbox) in the drinking image."""
        detections = person_detector.predict([person_frame])[0]
        assert detections.bbox_xyxy.shape[0] >= 1
        for conf, bbox in zip(detections.confidence, detections.bbox_xyxy):
            x1, y1, x2, y2 = bbox
            assert 0.0 <= x1 <= 1.0
            assert 0.0 <= y1 <= 1.0
            assert 0.0 <= x2 <= 1.0
            assert 0.0 <= y2 <= 1.0
            assert x2 > x1
            assert y2 > y1
            assert conf > 0.3

    def test_crop_largest_person(
        self, person_detector: PersonDetector, person_frame: cv2t.MatLike,
    ) -> None:
        """Should return a non-empty crop of the person."""
        crop = person_detector.extract_largest_crop([person_frame])[0]
        assert crop is not None
        assert crop.shape[0] > 0
        assert crop.shape[1] > 0

    def test_no_person_in_empty_frame(self, person_detector: PersonDetector) -> None:
        """Black frame should return no detections."""
        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        crop = person_detector.extract_largest_crop([frame])[0]
        assert crop is None


class TestActionWithPersonDetector:
    def test_frame_with_person_returns_detections(
        self, client_with_detector: TestClient, person_frame_b64: str,
    ) -> None:
        """Person drinking image should produce action detections."""
        with client_with_detector.websocket_connect(
            "/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            resp = None
            for _ in range(16):
                ws.send_text(
                    json.dumps({"type": "frame", "task": "action", "frame_b64": person_frame_b64})
                )
                resp = ws.receive_json()
            assert resp is not None
            assert "detected_classes" in resp
            assert isinstance(resp["detected_classes"], list)
            assert "drinking" in resp["detected_classes"][0]["class_name"]

    def test_empty_frame_returns_empty_detections(
        self, client_with_detector: TestClient, empty_frame_b64: str,
    ) -> None:
        """Black frame (no person) should return empty detected_classes."""
        with client_with_detector.websocket_connect(
            "/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({"type": "frame", "task": "action", "frame_b64": empty_frame_b64}))
            resp = ws.receive_json()
            assert "detected_classes" in resp
            assert resp["detected_classes"] == []

    def test_person_drinking_detected(
        self, client_with_detector: TestClient, person_frame_b64: str,
    ) -> None:
        """After multiple frames of person drinking, 'drinking' should appear."""
        with client_with_detector.websocket_connect(
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

            for _ in range(8):
                ws.send_text(
                    json.dumps({"type": "frame", "task": "action", "frame_b64": person_frame_b64})
                )
                resp = ws.receive_json()

            detected_names = [d["class_name"] for d in resp["detected_classes"]]
            assert len(detected_names) > 0, (
                f"Expected at least one drinking-related action, got empty. Full response: {resp}"
            )

    def test_detector_vs_no_detector_both_work(
        self,
        client_with_detector: TestClient,
        client_without_detector: TestClient,
        person_frame_b64: str,
    ) -> None:
        """Both paths (with and without person detector) should return valid responses."""
        for client in [client_with_detector, client_without_detector]:
            with client.websocket_connect(
                "/hal/api/dl/action-analysis/ws", headers=AUTH_HEADERS
            ) as ws:
                for _ in range(8):
                    ws.send_text(
                        json.dumps({"type": "frame", "task": "action", "frame_b64": person_frame_b64})
                    )
                    resp = ws.receive_json()
                assert "detected_classes" in resp
