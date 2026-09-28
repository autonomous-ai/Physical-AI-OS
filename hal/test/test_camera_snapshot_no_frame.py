"""GET /camera/snapshot returns 503 when no frame ever arrived, 500 for transient misses."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hal.app_state as state
from hal import privacy
from hal.routes import camera as camera_route


class _Capture:
    def __init__(self, last_frame_ts: float):
        self.last_frame_ts = last_frame_ts
        self.last_frame = None
        self.actual_width = None
        self.actual_height = None
        self.actual_fps = None
        self.zoom = 1.0

    def start(self):
        pass


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(camera_route.router)
    monkeypatch.setattr(privacy, "camera_muted", False, raising=False)
    monkeypatch.setattr(state, "_camera_disabled", False, raising=False)
    monkeypatch.setattr(state, "_camera_manual_override", False, raising=False)
    monkeypatch.setattr(state, "animation_service", None, raising=False)
    monkeypatch.setattr(camera_route, "cv2", object(), raising=False)
    monkeypatch.setattr(
        "hal.drivers.camera.video_capture_device.capture_still",
        lambda *a, **k: None,
    )
    return TestClient(app)


def test_no_frame_since_start_is_503_not_retryable(client, monkeypatch):
    monkeypatch.setattr(state, "camera_capture", _Capture(last_frame_ts=0.0), raising=False)
    r = client.get("/camera/snapshot")
    assert r.status_code == 503
    assert "not delivering frames" in r.json()["detail"]
    info = client.get("/camera").json()
    assert info["available"] is True and info["has_frame"] is False


def test_transient_miss_stays_500(client, monkeypatch):
    monkeypatch.setattr(state, "camera_capture", _Capture(last_frame_ts=12.5), raising=False)
    r = client.get("/camera/snapshot")
    assert r.status_code == 500
    assert r.json()["detail"] == "Failed to capture frame"
