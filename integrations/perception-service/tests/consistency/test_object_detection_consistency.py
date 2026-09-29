"""Consistency tests: local TestClient object detection matches the remote API within tolerance.

Requires DL_BACKEND_URL and DL_API_KEY.
"""

import asyncio
import base64
import os
from pathlib import Path
from typing import Any

import cv2
import httpx
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv(override=True)

DL_BACKEND_URL: str = os.getenv("DL_BACKEND_URL", "")
DL_API_KEY: str = os.getenv("DL_API_KEY", "")
TEST_API_KEY: str = "test-secret-key"
os.environ["DL_API_KEY"] = TEST_API_KEY

FIXTURES_DIR: Path = Path(__file__).parent.parent / "fixtures" / "images"
REMOTE_HEADERS: dict[str, str] = {"X-API-Key": DL_API_KEY}
LOCAL_HEADERS: dict[str, str] = {"X-API-Key": TEST_API_KEY}

ALL_DETECTORS: list[str] = ["yoloworld", "owlv2"]

pytestmark = pytest.mark.skipif(
    not DL_BACKEND_URL, reason="DL_BACKEND_URL not set — skipping consistency tests"
)

COORD_TOLERANCE: float = 2.0  # pixel coords
CONF_TOLERANCE: float = 0.05


@pytest.fixture(scope="session")
def fixture_images() -> list[tuple[str, str, int, int]]:
    """Load all fixture images once. Returns list of (name, b64, width, height)."""
    images: list[tuple[str, str, int, int]] = []
    for path in sorted(FIXTURES_DIR.glob("*")):
        if path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        frame = cv2.imread(str(path))
        if frame is None:
            continue
        h, w = frame.shape[:2]
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        b64 = base64.b64encode(buf.tobytes()).decode()
        images.append((path.name, b64, w, h))
    assert len(images) > 0, f"No fixture images found in {FIXTURES_DIR}"
    return images


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
def local_client(models: dict[str, Any]) -> TestClient:
    """Create TestClient with manually injected models."""
    import config
    import server

    from dlserver.utils.state import set_object_models

    config.settings.dl_api_key = TEST_API_KEY
    set_object_models(models)
    return TestClient(server.app)


@pytest.fixture(scope="session")
def local_ready(models: dict[str, Any]) -> set[str]:
    """Ready detectors on the local server."""
    return set(models.keys())


@pytest.fixture(scope="session")
def remote_ready() -> set[str]:
    """Ready detectors on the remote server."""
    try:
        resp = httpx.get(
            f"{DL_BACKEND_URL}/api/dl/object-detect/models",
            headers=REMOTE_HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return set()
        return {m["name"] for m in resp.json().get("models", []) if m.get("ready")}
    except Exception:
        return set()


def _skip_if_not_ready(
    detector: str, local_ready: set[str], remote_ready: set[str],
) -> None:
    if detector not in local_ready:
        pytest.skip(f"Detector '{detector}' not ready on local")
    if detector not in remote_ready:
        pytest.skip(f"Detector '{detector}' not ready on remote")


def _local_detect(
    client: TestClient, detector: str, b64: str, classes: list[str],
) -> list[dict[str, Any]]:
    resp = client.post(
        f"/api/dl/object-detect/{detector}",
        json={"image_b64": b64, "classes": classes},
        headers=LOCAL_HEADERS,
    )
    assert resp.status_code == 200
    return resp.json().get("detections", [])


def _remote_detect(
    detector: str, b64: str, classes: list[str],
) -> list[dict[str, Any]]:
    resp = httpx.post(
        f"{DL_BACKEND_URL}/api/dl/object-detect/{detector}",
        json={"image_b64": b64, "classes": classes},
        headers=REMOTE_HEADERS,
        timeout=30,
    )
    assert resp.status_code == 200
    return resp.json().get("detections", [])


@pytest.mark.parametrize("detector", ALL_DETECTORS)
class TestObjectDetectionConsistency:
    """Verify local and remote servers return matching detections."""

    def test_detection_count_matches(
        self,
        detector: str,
        local_ready: set[str],
        remote_ready: set[str],
        local_client: TestClient,
        fixture_images: list[tuple[str, str, int, int]],
    ) -> None:
        """Same image should produce the same number of detections."""
        _skip_if_not_ready(detector, local_ready, remote_ready)
        classes = ["person", "chair", "table"]

        for img_name, b64, _, _ in fixture_images:
            local_dets = _local_detect(local_client, detector, b64, classes)
            remote_dets = _remote_detect(detector, b64, classes)

            assert len(local_dets) == len(remote_dets), (
                f"[{detector}/{img_name}] Count mismatch: "
                f"local={len(local_dets)}, remote={len(remote_dets)}"
            )

    def test_detection_values_match(
        self,
        detector: str,
        local_ready: set[str],
        remote_ready: set[str],
        local_client: TestClient,
        fixture_images: list[tuple[str, str, int, int]],
    ) -> None:
        """Same image should produce matching class names, coords, confidence."""
        _skip_if_not_ready(detector, local_ready, remote_ready)
        classes = ["person"]

        for img_name, b64, _, _ in fixture_images:
            local_dets = _local_detect(local_client, detector, b64, classes)
            remote_dets = _remote_detect(detector, b64, classes)

            local_sorted = sorted(local_dets, key=lambda d: d["confidence"], reverse=True)
            remote_sorted = sorted(remote_dets, key=lambda d: d["confidence"], reverse=True)

            for i, (ld, rd) in enumerate(zip(local_sorted, remote_sorted)):
                assert ld["class_name"] == rd["class_name"], (
                    f"[{detector}/{img_name}#{i}] Class: "
                    f"local={ld['class_name']}, remote={rd['class_name']}"
                )
                assert abs(ld["confidence"] - rd["confidence"]) < CONF_TOLERANCE, (
                    f"[{detector}/{img_name}#{i}] Confidence: "
                    f"local={ld['confidence']:.4f}, remote={rd['confidence']:.4f}"
                )
                for j, axis in enumerate(["x", "y", "w", "h"]):
                    assert abs(ld["xywh"][j] - rd["xywh"][j]) < COORD_TOLERANCE, (
                        f"[{detector}/{img_name}#{i}] {axis}: "
                        f"local={ld['xywh'][j]:.1f}, remote={rd['xywh'][j]:.1f}"
                    )

    def test_person_detected_consistently(
        self,
        detector: str,
        local_ready: set[str],
        remote_ready: set[str],
        local_client: TestClient,
    ) -> None:
        """Both local and remote must detect 'person' in person_drinking.jpg."""
        _skip_if_not_ready(detector, local_ready, remote_ready)

        frame = cv2.imread(str(FIXTURES_DIR / "person_drinking.jpg"))
        assert frame is not None
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        b64 = base64.b64encode(buf.tobytes()).decode()

        local_dets = _local_detect(local_client, detector, b64, ["person"])
        remote_dets = _remote_detect(detector, b64, ["person"])

        local_names = {d["class_name"] for d in local_dets}
        remote_names = {d["class_name"] for d in remote_dets}

        assert "person" in local_names, f"[{detector}] Local missed person"
        assert "person" in remote_names, f"[{detector}] Remote missed person"
