"""Integration tests against a remote DL backend (object detection), per ready detector.

Requires DL_BACKEND_URL and DL_API_KEY.
"""

import base64
import json
import os
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np
import pytest
import websockets
from dotenv import load_dotenv

load_dotenv(override=True)

DL_BACKEND_URL: str = os.getenv("DL_BACKEND_URL", "")
DL_API_KEY: str = os.getenv("DL_API_KEY", "")
FIXTURES_DIR: Path = Path(__file__).resolve().parent.parent / "fixtures" / "images"

pytestmark = pytest.mark.skipif(
    not DL_BACKEND_URL, reason="DL_BACKEND_URL not set — skipping remote API tests"
)

ALL_DETECTORS: list[str] = ["yoloworld", "owlv2"]


def _http_url(path: str) -> str:
    return f"{DL_BACKEND_URL}{path}"


def _ws_url(path: str) -> str:
    return DL_BACKEND_URL.replace("http://", "ws://").replace("https://", "wss://") + path


AUTH_HEADERS: dict[str, str] = {"X-API-Key": DL_API_KEY}


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
def test_image(person_image: tuple[str, int, int]) -> tuple[str, int, int]:
    """Alias for person_image."""
    return person_image


@pytest.fixture(scope="session")
def test_image_b64(person_image: tuple[str, int, int]) -> str:
    """Just the b64 string for tests that don't need dimensions."""
    return person_image[0]


@pytest.fixture(scope="session")
def random_frame_b64() -> str:
    """Generate a random noise frame once."""
    frame = np.random.randint(0, 255, (240, 320, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


@pytest.fixture(scope="session")
def ready_detectors() -> set[str]:
    """Query the remote server once and cache the set of ready detector names."""
    if not DL_BACKEND_URL:
        return set()
    try:
        resp = httpx.get(
            _http_url("/api/dl/object-detect/models"),
            headers=AUTH_HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return set()
        models: list[dict[str, Any]] = resp.json().get("models", [])
        return {m["name"] for m in models if m.get("ready")}
    except Exception:
        return set()


def _skip_if_not_ready(detector: str, ready_detectors: set[str]) -> None:
    if detector not in ready_detectors:
        pytest.skip(f"Detector '{detector}' not ready on remote")


class TestObjectDetectionHTTP:
    def test_list_models(self) -> None:
        resp = httpx.get(
            _http_url("/api/dl/object-detect/models"),
            headers=AUTH_HEADERS,
            timeout=10,
        )
        assert resp.status_code == 200
        body: dict[str, Any] = resp.json()
        assert "models" in body

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_compat_endpoint_returns_list(
        self, detector: str, ready_detectors: set[str], test_image_b64: str,
    ) -> None:
        """Backward-compat flat-list endpoint (go2 format)."""
        _skip_if_not_ready(detector, ready_detectors)
        resp = httpx.post(
            _http_url(f"/api/dl/{detector}"),
            json={"image_b64": test_image_b64, "classes": ["person", "chair"]},
            headers=AUTH_HEADERS,
            timeout=30,
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_wrapped_endpoint_returns_detections(
        self, detector: str, ready_detectors: set[str], test_image_b64: str,
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        resp = httpx.post(
            _http_url(f"/api/dl/object-detect/{detector}"),
            json={"image_b64": test_image_b64, "classes": ["person"]},
            headers=AUTH_HEADERS,
            timeout=30,
        )
        assert resp.status_code == 200
        body: dict[str, Any] = resp.json()
        assert "detections" in body
        assert isinstance(body["detections"], list)

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_detection_item_fields(
        self, detector: str, ready_detectors: set[str],
        test_image: tuple[str, int, int],
    ) -> None:
        """Each detection has class_name, xywh (pixel coords), and confidence."""
        _skip_if_not_ready(detector, ready_detectors)
        img_b64, img_w, img_h = test_image
        resp = httpx.post(
            _http_url(f"/api/dl/{detector}"),
            json={"image_b64": img_b64, "classes": ["person"]},
            headers=AUTH_HEADERS,
            timeout=30,
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

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_detect_without_classes(
        self, detector: str, ready_detectors: set[str], test_image_b64: str,
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        resp = httpx.post(
            _http_url(f"/api/dl/{detector}"),
            json={"image_b64": test_image_b64},
            headers=AUTH_HEADERS,
            timeout=30,
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_unknown_detector_returns_503(self, random_frame_b64: str) -> None:
        resp = httpx.post(
            _http_url("/api/dl/nonexistent"),
            json={"image_b64": random_frame_b64},
            headers=AUTH_HEADERS,
            timeout=10,
        )
        assert resp.status_code == 503


def _detect(detector: str, b64: str, classes: list[str]) -> list[dict[str, Any]]:
    resp = httpx.post(
        _http_url(f"/api/dl/object-detect/{detector}"),
        json={"image_b64": b64, "classes": classes},
        headers=AUTH_HEADERS,
        timeout=30,
    )
    assert resp.status_code == 200
    return resp.json().get("detections", [])


class TestObjectDetectionPerformance:
    """Evaluate detection quality on known images via remote API."""

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_person_drinking_detects_person(
        self, detector: str, ready_detectors: set[str],
        person_image: tuple[str, int, int],
    ) -> None:
        """person_drinking.jpg must detect at least one 'person' with high confidence."""
        _skip_if_not_ready(detector, ready_detectors)
        b64, img_w, img_h = person_image
        dets = _detect(detector, b64, ["person"])

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
        self, detector: str, ready_detectors: set[str],
        office_image: tuple[str, int, int],
    ) -> None:
        """Office header has ~6 people — should detect multiple persons."""
        _skip_if_not_ready(detector, ready_detectors)
        b64, img_w, img_h = office_image
        dets = _detect(detector, b64, ["person"])

        persons = [d for d in dets if d["class_name"] == "person"]
        assert len(persons) >= 3, (
            f"[{detector}] Expected >=3 persons in office, got {len(persons)}"
        )
        for det in persons:
            x, y, w, h = det["xywh"]
            assert 0 <= x <= img_w and 0 <= y <= img_h
            assert 0 < w <= img_w and 0 < h <= img_h
            assert 0 < det["confidence"] <= 1.0

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_office_detects_furniture(
        self, detector: str, ready_detectors: set[str],
        office_image: tuple[str, int, int],
    ) -> None:
        """Office header has chairs, monitors, laptops — should detect some."""
        _skip_if_not_ready(detector, ready_detectors)
        b64, img_w, img_h = office_image
        classes = ["chair", "monitor", "laptop", "desk", "table"]
        dets = _detect(detector, b64, classes)

        assert len(dets) >= 1, (
            f"[{detector}] No furniture/equipment detected in office image"
        )
        for det in dets:
            x, y, w, h = det["xywh"]
            assert 0 <= x <= img_w and 0 <= y <= img_h
            assert 0 < w <= img_w and 0 < h <= img_h
            assert 0 < det["confidence"] <= 1.0

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_no_person_in_fire_image(
        self, detector: str, ready_detectors: set[str],
        fire_image: tuple[str, int, int],
    ) -> None:
        """fire-1.jpg should not detect 'person' (no people in fire scenes)."""
        _skip_if_not_ready(detector, ready_detectors)
        b64, _, _ = fire_image
        dets = _detect(detector, b64, ["person"])

        persons = [d for d in dets if d["class_name"] == "person" and d["confidence"] > 0.5]
        assert len(persons) == 0, (
            f"[{detector}] False positive: detected {len(persons)} person(s) in fire image"
        )


class TestObjectDetectionWebSocket:
    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    @pytest.mark.asyncio
    async def test_heartbeat(
        self, detector: str, ready_detectors: set[str],
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        async with websockets.connect(
            _ws_url(f"/api/dl/object-detection/{detector}/ws"),
            additional_headers=AUTH_HEADERS,
        ) as ws:
            await ws.send(json.dumps({"type": "heartbeat", "task": "object"}))
            resp: dict[str, Any] = json.loads(await ws.recv())
            assert resp == {"status": "ok"}

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    @pytest.mark.asyncio
    async def test_frame_returns_detections(
        self, detector: str, ready_detectors: set[str], test_image_b64: str,
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        async with websockets.connect(
            _ws_url(f"/api/dl/object-detection/{detector}/ws"),
            additional_headers=AUTH_HEADERS,
        ) as ws:
            await ws.send(json.dumps({
                "type": "frame", "task": "object",
                "frame_b64": test_image_b64,
            }))
            resp: dict[str, Any] = json.loads(await ws.recv())
            assert "detections" in resp
            assert isinstance(resp["detections"], list)

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    @pytest.mark.asyncio
    async def test_config_update(
        self, detector: str, ready_detectors: set[str],
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        async with websockets.connect(
            _ws_url(f"/api/dl/object-detection/{detector}/ws"),
            additional_headers=AUTH_HEADERS,
        ) as ws:
            await ws.send(json.dumps({
                "type": "config", "task": "object",
                "classes": ["person", "dog"],
                "threshold": 0.5,
            }))
            resp: dict[str, Any] = json.loads(await ws.recv())
            assert resp["status"] == "config_updated"

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    @pytest.mark.asyncio
    async def test_invalid_json(
        self, detector: str, ready_detectors: set[str],
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        async with websockets.connect(
            _ws_url(f"/api/dl/object-detection/{detector}/ws"),
            additional_headers=AUTH_HEADERS,
        ) as ws:
            await ws.send("not json")
            resp: dict[str, Any] = json.loads(await ws.recv())
            assert "error" in resp
