"""Host webcam backend for the laptop simulation mode (`HAL_SIM_MEDIA=host`)."""

from __future__ import annotations

import logging
import platform
import threading
import time

import cv2
import numpy as np

from .models import VideoCaptureDeviceInfo, VideoCaptureDeviceResponse
from .video_capture_device import VideoCaptureDeviceBase

logger = logging.getLogger("hal.camera.host")


class HostCameraUnavailable(RuntimeError):
    """The host webcam could not be opened — permission, absence, or in use."""


_WARMUP_ATTEMPTS = 40
_WARMUP_GAP_S = 0.05


def _read_warm(cap) -> "np.ndarray | None":
    """Read until the capture session delivers, or the warm-up budget runs out."""
    for _ in range(_WARMUP_ATTEMPTS):
        ok, frame = cap.read()
        if ok and frame is not None:
            return frame
        time.sleep(_WARMUP_GAP_S)
    return None


def _backends() -> list[int]:
    """OpenCV backends to try, most native for this OS first."""
    if platform.system() == "Darwin":
        return [cv2.CAP_AVFOUNDATION, cv2.CAP_ANY]
    if platform.system() == "Linux":
        return [cv2.CAP_V4L2, cv2.CAP_ANY]
    return [cv2.CAP_ANY]


def probe_host_camera(device_id: int | str) -> None:
    """Open, read a frame, release. Raises HostCameraUnavailable on failure.

    Called before the simulator commits to host media so the fallback to the virtual
    device happens with a reason a human can act on, instead of a stream that silently
    never delivers a frame.
    """
    last = "camera did not open"
    for backend in _backends():
        cap = None
        try:
            cap = cv2.VideoCapture(device_id, backend)
            if not cap.isOpened():
                last = "camera did not open"
                continue
            if _read_warm(cap) is None:
                last = "camera opened but delivered no frame"
                continue
            return
        except Exception as e:  # pragma: no cover - defensive
            last = str(e)
        finally:
            if cap is not None:
                cap.release()
    if platform.system() == "Darwin":
        raise HostCameraUnavailable(
            f"{last}. On macOS grant camera access to the terminal app running "
            f"HAL: System Settings > Privacy & Security > Camera, then restart "
            f"`make sim SIM_MEDIA=host`. Also close any app already holding the "
            f"webcam (Zoom, Photo Booth, FaceTime)."
        )
    raise HostCameraUnavailable(last)


class HostVideoCaptureDevice(VideoCaptureDeviceBase):
    """The developer machine's webcam, with the production capture surface."""

    runable = True
    # The host webcam is addressed by OpenCV index, not by a /dev/video node
    # resolved from a hardware name — the by-id/sysfs probe does not exist on
    # macOS and would only log a misleading failure.
    requires_v4l2_index = False

    _IDLE_INTERVAL_S = 0.2

    _READ_GRACE_S = 3.0

    def __init__(self, device_info: VideoCaptureDeviceInfo, name: str | None = None):
        super().__init__(device_info, name)
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_response: VideoCaptureDeviceResponse | None = None
        self._last_frame_monotonic = 0.0
        self._active_consumers = 0
        self._consumers_lock = threading.Lock()
        self.zoom = 1.0
        self.actual_width: int | None = None
        self.actual_height: int | None = None
        self.actual_fps: float | None = None

    @property
    def last_frame(self) -> np.ndarray | None:
        with self._lock:
            if self._last_response and self._last_response.frame is not None:
                return self._last_response.frame.copy()
            return None

    @property
    def last_frame_ts(self) -> float:
        with self._lock:
            return self._last_frame_monotonic

    @property
    def last_frame_description(self) -> str | None:
        with self._lock:
            return self._last_response.frame_description if self._last_response else None

    @property
    def last_response(self) -> VideoCaptureDeviceResponse | None:
        with self._lock:
            return self._last_response.model_copy(deep=True) if self._last_response else None

    def capture(self, need_description: bool = False):
        if self._thread is None:
            raise RuntimeError("HostVideoCaptureDevice has not started")
        return self.last_response

    def acquire_consumer(self) -> None:
        with self._consumers_lock:
            self._active_consumers += 1

    def release_consumer(self) -> None:
        with self._consumers_lock:
            self._active_consumers = max(0, self._active_consumers - 1)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # Fail loud here (not in the loop thread): the caller decides whether to
        # fall back to the virtual device, and it can only do that if the
        # failure reaches it.
        probe_host_camera(self.device_info.device_id)
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._loop, name="HostVideoCaptureDevice loop", daemon=True
        )
        self._thread.start()
        self.running = True

    def stop(self) -> None:
        self._stopped.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        self.running = False

    def _open(self):
        for backend in _backends():
            cap = cv2.VideoCapture(self.device_info.device_id, backend)
            if not cap.isOpened():
                cap.release()
                continue
            if self._max_width:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._max_width)
            if self._max_height:
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._max_height)
            if self._fps:
                cap.set(cv2.CAP_PROP_FPS, self._fps)
            self.actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None
            self.actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            self.actual_fps = fps if fps > 0 else float(self._fps or 15)
            logger.info(
                "Host webcam opened (device=%s, %sx%s @ %.1ffps)",
                self.device_info.device_id,
                self.actual_width,
                self.actual_height,
                self.actual_fps,
            )
            return cap
        return None

    def _apply_zoom(self, frame: np.ndarray) -> np.ndarray:
        z = self.zoom
        if z is None or z <= 1.0:
            return frame
        h, w = frame.shape[:2]
        cw, ch = int(w / z), int(h / z)
        x0, y0 = (w - cw) // 2, (h - ch) // 2
        crop = frame[y0:y0 + ch, x0:x0 + cw]
        return cv2.resize(crop, (w, h), interpolation=cv2.INTER_LINEAR)

    def _loop(self) -> None:
        cap = None
        last_good = 0.0
        try:
            while not self._stopped.is_set():
                if cap is None:
                    cap = self._open()
                    if cap is None:
                        logger.warning("Host webcam unavailable; retrying in 2s")
                        self._stopped.wait(2.0)
                        continue
                    last_good = time.monotonic()
                ok, frame = cap.read()
                if not ok or frame is None:
                    if time.monotonic() - last_good < self._READ_GRACE_S:
                        self._stopped.wait(_WARMUP_GAP_S)
                        continue
                    logger.warning(
                        "Host webcam delivered no frame for %.0fs; reopening",
                        self._READ_GRACE_S,
                    )
                    cap.release()
                    cap = None
                    self._stopped.wait(0.5)
                    continue
                last_good = time.monotonic()
                frame = self._apply_zoom(frame)
                response = VideoCaptureDeviceResponse(
                    frame=frame, frame_description="Host webcam frame"
                )
                with self._lock:
                    self._last_response = response
                    self._last_frame_monotonic = time.monotonic()
                for callback in list(self.callbacks):
                    try:
                        callback(self.device_info, response)
                    except Exception as e:  # pragma: no cover - consumer fault
                        logger.warning("Camera callback failed: %s", e)
                with self._consumers_lock:
                    busy = self._active_consumers > 0
                if not busy:
                    self._stopped.wait(self._IDLE_INTERVAL_S)
        finally:
            if cap is not None:
                cap.release()
            self.running = False
