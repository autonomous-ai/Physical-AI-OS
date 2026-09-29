"""Object detection endpoint tests against a local TestClient, per ready detector."""

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv(override=True)

TEST_API_KEY: str = "test-secret-key"
os.environ["DL_API_KEY"] = TEST_API_KEY
FIXTURES_DIR: Path = Path(__file__).parent.parent / "fixtures" / "images"
AUTH_HEADERS: dict[str, str] = {"X-API-Key": TEST_API_KEY}

ALL_DETECTORS: list[str] = ["yoloworld", "owlv2"]


def _load_image(name: str) -> tuple[str, int, int]:
    """Load a fixture image. Returns (b64, width, height)."""
    img = cv2.imread(str(FIXTURES_DIR / name))
    assert img is not None, f"Failed to load {FIXTURES_DIR / name}"
    h, w = img.shape[:2]
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return base64.b64encode(buf.tobytes()).decode(), w, h


@pytest.fixture(scope="session")
def person_image() -> tuple[str, int, int]:
    """person_drinking.jpg as (b64, width, height)."""
    return _load_image("person_drinking.jpg")


@pytest.fixture(scope="session")
def office_image() -> tuple[str, int, int]:
    """small-office-header.jpg as (b64, width, height)."""
    return _load_image("small-office-header.jpg")


@pytest.fixture(scope="session")
def fire_image() -> tuple[str, int, int]:
    """fire-1.jpg — house on fire, no people."""
    return _load_image("fire-1.jpg")


@pytest.fixture(scope="session")
def test_image_b64(person_image: tuple[str, int, int]) -> str:
    """Just the b64 string for tests that don't need dimensions."""
    return person_image[0]


@pytest.fixture(scope="session")
def test_image(person_image: tuple[str, int, int]) -> tuple[str, int, int]:
    """Alias for person_image for backwards compat."""
    return person_image


@pytest.fixture(scope="session")
def random_frame_b64() -> str:
    """Generate a random noise frame once."""
    frame = np.random.randint(0, 255, (240, 320, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


@pytest.fixture(scope="session")
def models() -> dict[str, Any]:
    """Build and start all object detection models."""
    from core.enums.object import ObjectDetectorEnum
    from core.perception.object.perception import ObjectPerception
    from core.perception.object.utils import ObjectDetectorFactory

    detector_configs: list[tuple[str, ObjectDetectorEnum]] = [
        ("yoloworld", ObjectDetectorEnum.YOLO_WORLD),
        ("owlv2", ObjectDetectorEnum.OWLV2),
    ]

    perceptions: dict[str, ObjectPerception] = {}
    for name, enum in detector_configs:
        try:
            factory = ObjectDetectorFactory(model_name=enum, use_onnx=True)
            perception = ObjectPerception(object_detector_factory=factory)
            asyncio.run(perception.start())
            perceptions[name] = perception
        except Exception as e:
            pytest.skip(f"Failed to start {name}: {e}")

    if not perceptions:
        pytest.skip("No object detectors available")

    return perceptions


@pytest.fixture(scope="session")
def client(models: dict[str, Any]) -> TestClient:
    """Create TestClient with manually injected models."""
    import config
    import server

    from dlserver.utils.state import set_object_models

    config.settings.dl_api_key = TEST_API_KEY
    set_object_models(models)
    return TestClient(server.app)


@pytest.fixture(scope="session")
def ready_detectors(models: dict[str, Any]) -> set[str]:
    """Return the set of successfully started detector names."""
    return set(models.keys())


def _skip_if_not_ready(
    detector: str,
    ready_detectors: set[str],
) -> None:
    if detector not in ready_detectors:
        pytest.skip(f"Detector '{detector}' not ready")


class TestObjectDetectionHTTP:
    def test_list_models(self, client: TestClient) -> None:
        resp = client.get("/api/dl/object-detect/models", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        body: dict[str, Any] = resp.json()
        assert "models" in body

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_compat_endpoint_returns_list(
        self,
        client: TestClient,
        detector: str,
        ready_detectors: set[str],
        test_image_b64: str,
    ) -> None:
        resp = client.post(
            f"/api/dl/{detector}",
            json={"image_b64": test_image_b64, "classes": ["person", "chair"]},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_wrapped_endpoint_returns_detections(
        self,
        client: TestClient,
        detector: str,
        ready_detectors: set[str],
        test_image_b64: str,
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        resp = client.post(
            f"/api/dl/object-detect/{detector}",
            json={"image_b64": test_image_b64, "classes": ["person"]},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        body: dict[str, Any] = resp.json()
        assert "detections" in body
        assert isinstance(body["detections"], list)

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_detect_without_classes_uses_defaults(
        self,
        client: TestClient,
        detector: str,
        ready_detectors: set[str],
        test_image_b64: str,
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        resp = client.post(
            f"/api/dl/{detector}",
            json={"image_b64": test_image_b64},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_detection_item_fields(
        self,
        client: TestClient,
        detector: str,
        ready_detectors: set[str],
        test_image: tuple[str, int, int],
    ) -> None:
        """Detection items have class_name, xywh (pixel coords), and confidence."""
        _skip_if_not_ready(detector, ready_detectors)
        img_b64, img_w, img_h = test_image
        resp = client.post(
            f"/api/dl/{detector}",
            json={"image_b64": img_b64, "classes": ["person"]},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        detections: list[dict[str, Any]] = resp.json()
        for det in detections:
            assert "class_name" in det
            assert "xywh" in det
            assert "confidence" in det
            assert len(det["xywh"]) == 4
            x, y, w, h = det["xywh"]
            assert 0 <= x <= img_w, f"x={x} out of [0, {img_w}]"
            assert 0 <= y <= img_h, f"y={y} out of [0, {img_h}]"
            assert 0 < w <= img_w, f"w={w} out of (0, {img_w}]"
            assert 0 < h <= img_h, f"h={h} out of (0, {img_h}]"
            assert 0 < det["confidence"] <= 1.0

        assert any(d["class_name"] == "person" for d in detections), (
            f"No 'person' detected by {detector} on person_drinking.jpg"
        )

    def test_unknown_detector_returns_503(
        self,
        client: TestClient,
        random_frame_b64: str,
    ) -> None:
        resp = client.post(
            "/api/dl/nonexistent",
            json={"image_b64": random_frame_b64},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 503


def _detect(
    client: TestClient,
    detector: str,
    b64: str,
    classes: list[str],
) -> list[dict[str, Any]]:
    resp = client.post(
        f"/api/dl/object-detect/{detector}",
        json={"image_b64": b64, "classes": classes},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    return resp.json().get("detections", [])


class TestObjectDetectionPerformance:
    """Evaluate detection quality on known images."""

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_person_drinking_detects_person(
        self,
        client: TestClient,
        detector: str,
        ready_detectors: set[str],
        person_image: tuple[str, int, int],
    ) -> None:
        """person_drinking.jpg must detect at least one 'person' with high confidence."""
        _skip_if_not_ready(detector, ready_detectors)
        b64, img_w, img_h = person_image
        dets = _detect(client, detector, b64, ["person"])

        persons = [d for d in dets if d["class_name"] == "person"]
        assert len(persons) >= 1, f"[{detector}] No person detected"

        best = max(persons, key=lambda d: d["confidence"])
        assert best["confidence"] > 0.2, (
            f"[{detector}] Best person confidence too low: {best['confidence']:.3f}"
        )
        _, _, w, h = best["xywh"]
        assert w > img_w * 0.1, f"[{detector}] Person bbox too narrow: w={w:.0f}"
        assert h > img_h * 0.1, f"[{detector}] Person bbox too short: h={h:.0f}"

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_office_detects_people(
        self,
        client: TestClient,
        detector: str,
        ready_detectors: set[str],
        office_image: tuple[str, int, int],
    ) -> None:
        """Office header has ~6 people — should detect multiple persons."""
        _skip_if_not_ready(detector, ready_detectors)
        b64, img_w, img_h = office_image
        dets = _detect(client, detector, b64, ["person"])

        persons = [d for d in dets if d["class_name"] == "person"]
        assert len(persons) >= 3, f"[{detector}] Expected >=3 persons in office, got {len(persons)}"
        for det in persons:
            x, y, w, h = det["xywh"]
            assert 0 <= x <= img_w and 0 <= y <= img_h
            assert 0 < w <= img_w and 0 < h <= img_h
            assert 0 < det["confidence"] <= 1.0

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_office_detects_furniture(
        self,
        client: TestClient,
        detector: str,
        ready_detectors: set[str],
        office_image: tuple[str, int, int],
    ) -> None:
        """Office header has chairs, monitors, laptops — should detect some."""
        _skip_if_not_ready(detector, ready_detectors)
        b64, img_w, img_h = office_image
        classes = ["chair", "monitor", "laptop", "desk", "table"]
        dets = _detect(client, detector, b64, classes)

        assert len(dets) >= 1, f"[{detector}] No furniture/equipment detected in office image"
        for det in dets:
            x, y, w, h = det["xywh"]
            assert 0 <= x <= img_w and 0 <= y <= img_h
            assert 0 < w <= img_w and 0 < h <= img_h
            assert 0 < det["confidence"] <= 1.0

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_no_person_in_fire_image(
        self,
        client: TestClient,
        detector: str,
        ready_detectors: set[str],
        fire_image: tuple[str, int, int],
    ) -> None:
        """fire-1.jpg should not detect 'person' (no people in fire scenes)."""
        _skip_if_not_ready(detector, ready_detectors)
        b64, _, _ = fire_image
        dets = _detect(client, detector, b64, ["person"])

        persons = [d for d in dets if d["class_name"] == "person" and d["confidence"] > 0.5]
        assert len(persons) == 0, (
            f"[{detector}] False positive: detected {len(persons)} person(s) in fire image"
        )


class TestObjectDetectionWebSocket:
    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_heartbeat(
        self,
        client: TestClient,
        detector: str,
        ready_detectors: set[str],
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        with client.websocket_connect(
            f"/api/dl/object-detection/{detector}/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({"type": "heartbeat", "task": "object"}))
            resp: dict[str, Any] = ws.receive_json()
            assert resp == {"status": "ok"}

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_frame_returns_detections(
        self,
        client: TestClient,
        detector: str,
        ready_detectors: set[str],
        test_image_b64: str,
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        with client.websocket_connect(
            f"/api/dl/object-detection/{detector}/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "frame",
                        "task": "object",
                        "frame_b64": test_image_b64,
                    }
                )
            )
            resp: dict[str, Any] = ws.receive_json()
            assert "detections" in resp
            assert isinstance(resp["detections"], list)

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_config_update(
        self,
        client: TestClient,
        detector: str,
        ready_detectors: set[str],
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        with client.websocket_connect(
            f"/api/dl/object-detection/{detector}/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "config",
                        "task": "object",
                        "classes": ["person", "dog"],
                        "threshold": 0.5,
                    }
                )
            )
            resp: dict[str, Any] = ws.receive_json()
            assert resp["status"] == "config_updated"

    def test_unknown_detector_closes_ws(self, client: TestClient) -> None:
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/api/dl/object-detection/nonexistent/ws", headers=AUTH_HEADERS
            ) as ws:
                ws.send_text(json.dumps({"type": "heartbeat", "task": "object"}))
                ws.receive_json()

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_invalid_json(
        self,
        client: TestClient,
        detector: str,
        ready_detectors: set[str],
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        with client.websocket_connect(
            f"/api/dl/object-detection/{detector}/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text("not json")
            resp: dict[str, Any] = ws.receive_json()
            assert "error" in resp
