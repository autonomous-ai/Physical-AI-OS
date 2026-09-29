"""Camera driver for Raspberry Pi CSI sensors driven by libcamera."""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from typing import override

import numpy as np
import numpy.typing as npt

from .models import VideoCaptureDeviceInfo, VideoCaptureDeviceResponse
from .video_capture_device import VideoCaptureDeviceBase

_SOI = b"\xff\xd8\xff"
_EOI = b"\xff\xd9"

_READ_CHUNK = 65536
# Discard a partial buffer that never terminates — a truncated frame must not
# grow without bound if the child wedges mid-write.
_MAX_BUFFER = 8 * 1024 * 1024
_RESTART_DELAY_S = 2.0
_STALL_RESTART_S = 10.0

logger = logging.getLogger(__name__)


class RpicamVideoCaptureDevice(VideoCaptureDeviceBase):
    """MJPEG-over-pipe capture from `rpicam-vid`, for CSI/libcamera sensors."""

    runable: bool = True
    requires_v4l2_index: bool = False

    _IDLE_FPS: int = 5
    _ACTIVE_FPS: int = 15

    def __init__(
        self,
        device_info: VideoCaptureDeviceInfo,
        name: str | None = None,
    ):
        super().__init__(device_info, name)

        self._last_response: VideoCaptureDeviceResponse | None = None
        self._last_frame_monotonic: float = 0.0

        self._thread: threading.Thread | None = None
        self._lock: threading.Lock = threading.Lock()
        self._stopped: threading.Event = threading.Event()
        self._proc: subprocess.Popen | None = None

        self._active_consumers: int = 0
        self._consumers_lock: threading.Lock = threading.Lock()
        self._rate_changed: threading.Event = threading.Event()

        self.zoom: float = 1.0
        self.actual_width: int | None = None
        self.actual_height: int | None = None
        self.actual_fps: float | None = None

        self._logger: logging.Logger = logging.getLogger(self.__class__.__name__)

    @property
    def last_frame(self) -> npt.NDArray[np.uint8] | None:
        with self._lock:
            if self._last_response and self._last_response.frame is not None:
                return self._last_response.frame.copy()
            return None

    @property
    def last_frame_ts(self) -> float:
        """Monotonic capture time of last_frame (0.0 until the first frame)."""
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

    @last_response.setter
    def last_response(self, new_frame_info: VideoCaptureDeviceResponse | None):
        with self._lock:
            if new_frame_info:
                self._last_response = new_frame_info.model_copy(deep=True)
                self._last_frame_monotonic = time.monotonic()
            else:
                self._last_response = None
                self._last_frame_monotonic = 0.0

    def acquire_consumer(self):
        """Register an active consumer (e.g. MJPEG stream) for full-FPS capture."""
        with self._consumers_lock:
            self._active_consumers += 1
            crossed = self._active_consumers == 1
        if crossed:
            self._rate_changed.set()

    def release_consumer(self):
        """Unregister an active consumer — throttles capture when none remain."""
        with self._consumers_lock:
            self._active_consumers = max(0, self._active_consumers - 1)
            crossed = self._active_consumers == 0
        if crossed:
            self._rate_changed.set()

    @override
    def capture(
        self, need_description: bool = False
    ) -> VideoCaptureDeviceResponse | None:
        if self._thread is None:
            msg = f"{self.__class__.__name__} has not started"
            self._logger.info(msg)
            raise RuntimeError(msg)
        return self.last_response

    @override
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._logger.info(f"{self.__class__.__name__} has already started")
            return
        if not shutil.which(self._binary()):
            raise RuntimeError(
                f"{self._binary()} not found — install rpicam-apps (or libcamera-apps)"
            )
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._capture_loop,
            name=f"{self.__class__.__name__} capture loop",
            daemon=True,
        )
        self._thread.start()

    @override
    def stop(self):
        super().stop()
        self._stopped.set()
        self._kill_child()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @staticmethod
    def _binary() -> str:
        return "rpicam-vid" if shutil.which("rpicam-vid") else "libcamera-vid"

    def _target_fps(self) -> int:
        with self._consumers_lock:
            return self._ACTIVE_FPS if self._active_consumers > 0 else self._IDLE_FPS

    def _spawn(self, fps: int) -> subprocess.Popen:
        width = self._max_width or 1280
        height = self._max_height or 720
        cmd = [
            self._binary(),
            "--codec", "mjpeg",
            "--width", str(width),
            "--height", str(height),
            "--framerate", str(fps),
            "-t", "0",
            "--nopreview",
            "-o", "-",
        ]
        if self._rotate in (90, 180, 270):
            cmd += ["--rotation", str(int(self._rotate))]
        self._logger.info("starting %s at %dx%d@%dfps", cmd[0], width, height, fps)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
        )
        self.actual_width, self.actual_height, self.actual_fps = width, height, float(fps)
        return proc

    def _kill_child(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _capture_loop(self) -> None:
        import cv2

        buf = bytearray()
        while not self._stopped.is_set():
            fps = self._target_fps()
            try:
                self._proc = self._spawn(fps)
            except Exception as e:
                self._logger.warning("spawn failed: %s", e)
                self._stopped.wait(_RESTART_DELAY_S)
                continue

            self._rate_changed.clear()
            buf.clear()
            last_frame_at = time.monotonic()

            while not self._stopped.is_set():
                if self._rate_changed.is_set() and self._target_fps() != fps:
                    self._logger.info("consumer count changed — restarting at new rate")
                    break
                assert self._proc is not None
                if self._proc.poll() is not None:
                    self._logger.warning(
                        "%s exited (rc=%s) — restarting", self._binary(), self._proc.returncode
                    )
                    break
                chunk = self._proc.stdout.read(_READ_CHUNK) if self._proc.stdout else b""
                if not chunk:
                    self._logger.warning("capture pipe closed — restarting")
                    break
                buf += chunk

                newest: bytes | None = None
                while True:
                    start = buf.find(_SOI)
                    if start < 0:
                        break
                    end = buf.find(_EOI, start + len(_SOI))
                    if end < 0:
                        break
                    newest = bytes(buf[start : end + len(_EOI)])
                    del buf[: end + len(_EOI)]

                if newest is not None:
                    frame = cv2.imdecode(np.frombuffer(newest, np.uint8), cv2.IMREAD_COLOR)
                    if frame is not None:
                        if self.zoom and self.zoom > 1.0:
                            frame = self._apply_zoom(cv2, frame, self.zoom)
                        self.last_response = VideoCaptureDeviceResponse(frame=frame)
                        last_frame_at = time.monotonic()
                elif len(buf) > _MAX_BUFFER:
                    self._logger.warning("no JPEG boundary in %d bytes — resyncing", len(buf))
                    buf.clear()

                if time.monotonic() - last_frame_at > _STALL_RESTART_S:
                    self._logger.warning("no frame for %.0fs — restarting", _STALL_RESTART_S)
                    break

            self._kill_child()
            if not self._stopped.is_set():
                self._stopped.wait(_RESTART_DELAY_S)

    @staticmethod
    def _apply_zoom(cv2, frame: npt.NDArray[np.uint8], zoom: float) -> npt.NDArray[np.uint8]:
        """Centre crop by `zoom` then scale back, so consumers see one FOV."""
        h, w = frame.shape[:2]
        cw, ch = int(w / zoom), int(h / zoom)
        x, y = (w - cw) // 2, (h - ch) // 2
        return cv2.resize(frame[y : y + ch, x : x + cw], (w, h), interpolation=cv2.INTER_LINEAR)
