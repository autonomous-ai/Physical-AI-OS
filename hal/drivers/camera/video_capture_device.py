import logging
import os
import threading
import time
from typing import cast, override

import cv2
import numpy as np
import numpy.typing as npt

from .base import IDevice
from .models import VideoCaptureDeviceInfo, VideoCaptureDeviceResponse

_resolve_logger = logging.getLogger("CameraResolve")


def _norm_device_name(s: str) -> str:
    """Lowercase and strip non-alphanumerics so 'OPENAICAM', 'openaicam' and the by-id
    mangling ('usb-SunplusIT_Inc_OPENAICAM-video-index0') all compare equal on the parts
    that matter.
    """
    return "".join(c for c in s.lower() if c.isalnum())


def resolve_camera_device_id(
    name: str | None,
    fallback_index: int,
    by_id_dir: str = "/dev/v4l/by-id",
    sysfs_dir: str = "/sys/class/video4linux",
):
    """Resolve a camera device id from a hardware name, mirroring how audio
    devices are picked by name instead of a bare index.
    """
    if not name:
        return fallback_index
    if name.startswith("/"):
        if os.path.exists(name):
            _resolve_logger.info(
                "Camera resolved by path %r (%s)", name, os.path.realpath(name)
            )
            return name
        _resolve_logger.warning(
            "Camera path %r does not exist — falling back to index %d",
            name,
            fallback_index,
        )
        return fallback_index
    needle = _norm_device_name(name)
    try:
        if os.path.isdir(by_id_dir):
            for entry in sorted(os.listdir(by_id_dir)):
                if not entry.endswith("video-index0"):
                    continue
                if needle in _norm_device_name(entry):
                    path = os.path.join(by_id_dir, entry)
                    _resolve_logger.info(
                        "Camera resolved by name %r -> %s (%s)",
                        name,
                        path,
                        os.path.realpath(path),
                    )
                    return path
        if os.path.isdir(sysfs_dir):
            nodes = [
                e
                for e in os.listdir(sysfs_dir)
                if e.startswith("video") and e[5:].isdigit()
            ]
            for node in sorted(nodes, key=lambda e: int(e[5:])):
                try:
                    with open(os.path.join(sysfs_dir, node, "name")) as f:
                        node_name = f.read().strip()
                except OSError:
                    continue
                if needle not in _norm_device_name(node_name):
                    continue
                try:
                    with open(os.path.join(sysfs_dir, node, "index")) as f:
                        if f.read().strip() != "0":
                            continue
                except OSError:
                    pass
                _resolve_logger.info(
                    "Camera resolved by name %r -> /dev/%s (%s)", name, node, node_name
                )
                return f"/dev/{node}"
    except OSError:
        _resolve_logger.exception("Camera name resolution failed for %r", name)
    _resolve_logger.warning(
        "Camera name %r matched no video device — falling back to index %d",
        name,
        fallback_index,
    )
    return fallback_index


class VideoCaptureDeviceBase(
    IDevice[VideoCaptureDeviceInfo, VideoCaptureDeviceResponse]
):
    # True when device_id must be resolved to a V4L2 node index before construction (UVC
    # webcams).
    requires_v4l2_index: bool = True

    def __init__(
        self,
        device_info: VideoCaptureDeviceInfo,
        name: str | None = None,
    ):
        super().__init__(device_info, name)

        self._fps: int | None = device_info.fps
        self._max_width: int | None = device_info.max_width
        self._max_height: int | None = device_info.max_height
        self._rotate: float | None = device_info.rotate
        self._auto_exposure: str | None = device_info.auto_exposure
        self._exposure: int | None = device_info.exposure
        self._gain: int | None = device_info.gain
        self._brightness: int | None = device_info.brightness

    def capture(
        self, need_description: bool = False
    ) -> VideoCaptureDeviceResponse | None:
        """Capture the image (sync mode)"""
        raise NotImplementedError("capture method is not implemented")


class LocalVideoCaptureDevice(VideoCaptureDeviceBase):
    runable: bool = True

    # ISP-freeze watchdog: reopen the device when it has delivered byte-identical frames
    # for this long.
    _FREEZE_REOPEN_S: float = 10.0

    _COLOR_CORRUPT_REOPEN_S: float = 12.0
    _COLOR_SAT_MIN: int = 100  # HSV saturation floor for an "extreme" pixel
    _COLOR_VAL_MIN: int = 60
    _COLOR_GREEN_FRAC: float = 0.10
    _COLOR_MAGENTA_FRAC: float = 0.008
    _COLOR_RED_FRAC: float = 0.15
    _COLOR_RED_MAGENTA_FRAC: float = 0.01
    _COLOR_MAGENTA_DOMINANT_FRAC: float = 0.15
    _COLOR_DEEP_MAGENTA_FRAC: float = 0.10

    # ISP fault escalation: a plain V4L2 reopen does NOT reset the camera firmware.
    _ISP_FAULT_ESCALATE_COUNT: int = 1
    _ISP_FAULT_WINDOW_S: float = 600.0
    # Never power-cycle more often than this — a physically dead camera must
    # not put the loop into an endless unbind/bind cycle.
    _USB_POWER_CYCLE_COOLDOWN_S: float = 600.0
    _USB_REBIND_DELAY_S: float = 3.0
    _USB_DEVNODE_TIMEOUT_S: float = 15.0
    # After the node re-enumerates, the ISP firmware still needs a few seconds to finish
    # booting.
    _USB_SETTLE_AFTER_BIND_S: float = 5.0

    def __init__(
        self,
        device_info: VideoCaptureDeviceInfo,
        name: str | None = None,
    ):
        super().__init__(device_info, name)

        self._last_response: VideoCaptureDeviceResponse | None = None
        # Monotonic timestamp of when _last_response was captured. Lets
        # consumers (snapshot, realtime look) prove a frame was grabbed AFTER
        # the servos went quiet instead of blindly sleeping (see capture_still).
        self._last_frame_monotonic: float = 0.0

        self._thread: threading.Thread | None = None
        self._lock: threading.Lock = threading.Lock()
        self._stopped: threading.Event = threading.Event()

        self._active_consumers: int = 0
        self._consumers_lock: threading.Lock = threading.Lock()

        self.zoom: float = 1.0

        self.actual_width: int | None = None
        self.actual_height: int | None = None
        self.actual_fps: float | None = None

        # ISP deep-stuck escalation state: monotonic timestamps of recent
        # ISP-fault reopens (freeze or color corruption, sliding window) and
        # of the last USB power-cycle (None = never; cooldown gate).
        self._isp_fault_times: list[float] = []
        self._last_usb_power_cycle: float | None = None

        self._logger: logging.Logger = logging.getLogger(self.__class__.__name__)

    @property
    def last_frame(self) -> npt.NDArray[np.uint8] | None:
        with self._lock:
            if self._last_response and self._last_response.frame is not None:
                return self._last_response.frame.copy()
            else:
                return None

    @property
    def last_frame_ts(self) -> float:
        """Monotonic capture time of last_frame (0.0 until the first frame)."""
        with self._lock:
            return self._last_frame_monotonic

    @property
    def last_frame_description(self) -> str | None:
        with self._lock:
            if self._last_response:
                return self._last_response.frame_description
            else:
                return None

    @property
    def last_response(self) -> VideoCaptureDeviceResponse | None:
        with self._lock:
            if self._last_response:
                return self._last_response.model_copy(deep=True)
            else:
                return None

    @last_response.setter
    def last_response(self, new_frame_info: VideoCaptureDeviceResponse | None):
        with self._lock:
            if new_frame_info:
                self._last_response = new_frame_info.model_copy(deep=True)
                self._last_frame_monotonic = time.monotonic()
            else:
                self._last_response = None
                self._last_frame_monotonic = 0.0

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

        # A previous loop thread that died — the camera vanished from USB and the open
        # path raised, or an unhandled error escaped — leaves a stale, non-None _thread.
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._video_capture_loop,
            name=f"{self.__class__.__name__} video capture loop",
            daemon=True,
        )
        self._thread.start()

    @staticmethod
    def _try_open(device_id):
        """Try opening camera with V4L2 backend, fallback to default."""
        cap = cv2.VideoCapture(device_id, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap = cv2.VideoCapture(device_id)
        return cap

    def _apply_camera_controls(self, video_capture):
        """Pin exposure (when configured) so auto-exposure can't throttle FPS."""
        if (self._auto_exposure or "auto") != "manual":
            try:
                video_capture.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3)
                if self._gain is not None:
                    video_capture.set(cv2.CAP_PROP_GAIN, float(self._gain))
                self._logger.info(
                    "Camera exposure: auto (auto_exposure=%.0f gain=%.0f)",
                    video_capture.get(cv2.CAP_PROP_AUTO_EXPOSURE),
                    video_capture.get(cv2.CAP_PROP_GAIN),
                )
            except Exception:
                self._logger.exception(
                    "Camera auto-exposure restore failed — continuing with camera state as-is"
                )
            return
        try:
            video_capture.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
            if self._exposure is not None:
                video_capture.set(cv2.CAP_PROP_EXPOSURE, float(self._exposure))
            if self._gain is not None:
                video_capture.set(cv2.CAP_PROP_GAIN, float(self._gain))
            if self._brightness is not None:
                video_capture.set(cv2.CAP_PROP_BRIGHTNESS, float(self._brightness))
            self._logger.info(
                "Camera exposure: manual (auto_exposure=%.0f exposure=%.0f gain=%.0f brightness=%.0f)",
                video_capture.get(cv2.CAP_PROP_AUTO_EXPOSURE),
                video_capture.get(cv2.CAP_PROP_EXPOSURE),
                video_capture.get(cv2.CAP_PROP_GAIN),
                video_capture.get(cv2.CAP_PROP_BRIGHTNESS),
            )
        except Exception:
            self._logger.exception(
                "Camera exposure control failed — continuing with camera defaults"
            )

    @classmethod
    def _looks_color_corrupt(cls, small: npt.NDArray[np.uint8]) -> bool:
        """Heuristic ISP color-corruption check on a subsampled BGR frame."""
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        hue = hsv[..., 0]
        extreme = (hsv[..., 1] >= cls._COLOR_SAT_MIN) & (
            hsv[..., 2] >= cls._COLOR_VAL_MIN
        )
        red_frac = float((extreme & ((hue <= 10) | (hue >= 170))).mean())
        green_frac = float((extreme & (hue >= 35) & (hue <= 85)).mean())
        magenta_frac = float((extreme & (hue >= 130) & (hue <= 175)).mean())
        deep_magenta_frac = float((extreme & (hue >= 155) & (hue <= 169)).mean())
        green_magenta_corrupt = (
            green_frac >= cls._COLOR_GREEN_FRAC
            and magenta_frac >= cls._COLOR_MAGENTA_FRAC
        )
        red_magenta_corrupt = (
            red_frac >= cls._COLOR_RED_FRAC
            and magenta_frac >= cls._COLOR_RED_MAGENTA_FRAC
        )
        magenta_palette_corrupt = (
            magenta_frac >= cls._COLOR_MAGENTA_DOMINANT_FRAC
            and deep_magenta_frac >= cls._COLOR_DEEP_MAGENTA_FRAC
        )
        return (
            green_magenta_corrupt
            or red_magenta_corrupt
            or magenta_palette_corrupt
        )

    def _recover_isp_fault(self, video_capture, device_id, now_mono, reason: str):
        """Recover from an ISP fault (freeze / color corruption)."""
        self._isp_fault_times = [
            t for t in self._isp_fault_times if now_mono - t < self._ISP_FAULT_WINDOW_S
        ]
        self._isp_fault_times.append(now_mono)
        n_faults = len(self._isp_fault_times)
        cooldown_ok = (
            self._last_usb_power_cycle is None
            or now_mono - self._last_usb_power_cycle >= self._USB_POWER_CYCLE_COOLDOWN_S
        )
        if n_faults >= self._ISP_FAULT_ESCALATE_COUNT and cooldown_ok:
            self._logger.warning(
                "Camera USB power-cycle (ISP fault — plain reopen won't clear "
                "an ISP wedge; %d fault(s) in %.0fs)",
                n_faults,
                self._ISP_FAULT_WINDOW_S,
            )
            try:
                video_capture.release()
            except Exception:
                self._logger.exception("Camera release failed before USB power-cycle")
            if self._usb_power_cycle(device_id):
                self._last_usb_power_cycle = time.monotonic()
                self._isp_fault_times.clear()
        return self._reopen_with_backoff(video_capture, device_id, reason)

    @staticmethod
    def _video_dev_node(device_id) -> str | None:
        """Map a capture device id to its /dev/video<N> node path."""
        if isinstance(device_id, int):
            return f"/dev/video{device_id}"
        if isinstance(device_id, str) and device_id.startswith("/dev/"):
            node = os.path.realpath(device_id)
            if os.path.basename(node).startswith("video"):
                return node
        return None

    def _resolve_usb_path(self, device_id) -> str | None:
        """Resolve the USB bus path (e.g. "1-1") behind /dev/video<N>."""
        sys_dev = self._resolve_usb_sysfs_dir(device_id)
        return os.path.basename(sys_dev) if sys_dev is not None else None

    def _resolve_usb_sysfs_dir(self, device_id) -> str | None:
        """Return the USB device sysfs directory behind a video device."""
        node = self._video_dev_node(device_id)
        if node is None:
            return None
        try:
            sys_dev = os.path.realpath(
                f"/sys/class/video4linux/{os.path.basename(node)}/device"
            )
            for _ in range(10):
                if os.path.isfile(os.path.join(sys_dev, "idVendor")):
                    return sys_dev
                parent = os.path.dirname(sys_dev)
                if parent == sys_dev:
                    break
                sys_dev = parent
        except OSError:
            self._logger.exception("USB path resolve failed for %s", node)
        return None

    def _disable_usb_autosuspend(self, device_id) -> None:
        """Keep a USB camera powered while HAL owns it."""
        sys_dev = self._resolve_usb_sysfs_dir(device_id)
        if sys_dev is None:
            return
        control_path = os.path.join(sys_dev, "power", "control")
        try:
            with open(control_path, "w") as f:
                f.write("on")
            self._logger.info(
                "Camera USB autosuspend disabled for %s", os.path.basename(sys_dev)
            )
        except OSError:
            # Runtime-PM is an optional kernel feature. Failure must never
            # stop a camera that otherwise works (including CSI cameras).
            self._logger.warning(
                "Could not disable USB autosuspend for camera at %s", sys_dev
            )

    def _usb_power_cycle(self, device_id) -> bool:
        """Power-cycle the camera's USB device via driver unbind/bind."""
        usb_path = self._resolve_usb_path(device_id)
        if usb_path is None:
            self._logger.warning(
                "Camera USB power-cycle skipped — no USB bus path for %r "
                "(non-USB camera?), falling back to plain reopen",
                device_id,
            )
            return False
        try:
            with open("/sys/bus/usb/drivers/usb/unbind", "w") as f:
                f.write(usb_path)
            self._stopped.wait(self._USB_REBIND_DELAY_S)
            with open("/sys/bus/usb/drivers/usb/bind", "w") as f:
                f.write(usb_path)
        except OSError:
            self._logger.exception(
                "Camera USB power-cycle failed for %s — falling back to plain reopen",
                usb_path,
            )
            return False
        node = self._video_dev_node(device_id)
        deadline = time.monotonic() + self._USB_DEVNODE_TIMEOUT_S
        while not self._stopped.is_set() and time.monotonic() < deadline:
            if node and os.path.exists(node):
                self._disable_usb_autosuspend(device_id)
                self._logger.info(
                    "Camera USB power-cycled (%s) — %s is back, settling %.0fs",
                    usb_path, node, self._USB_SETTLE_AFTER_BIND_S,
                )
                self._stopped.wait(self._USB_SETTLE_AFTER_BIND_S)
                return True
            self._stopped.wait(0.5)
        self._logger.warning(
            "Camera USB power-cycled (%s) but %s not back after %.0fs — "
            "reopen backoff will keep retrying",
            usb_path,
            node or device_id,
            self._USB_DEVNODE_TIMEOUT_S,
        )
        return True

    def _reopen_with_backoff(self, video_capture, device_id, reason: str):
        """Release and reopen the capture device, retrying with backoff."""
        try:
            video_capture.release()
        except Exception:
            self._logger.exception("Camera release failed during recovery")
        delay: float = 1.0
        while not self._stopped.is_set():
            cap = self._try_open(device_id)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                if self._max_width:
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._max_width)
                if self._max_height:
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._max_height)
                self._apply_camera_controls(cap)
                self._logger.info("Camera reopened (%s) — resuming loop", reason)
                return cap
            try:
                cap.release()
            except Exception:
                pass
            self._logger.warning(
                "Camera reopen failed (%s) — retrying in %.0fs", reason, delay
            )
            self._stopped.wait(delay)
            delay = min(delay * 2, 30.0)
        return None

    def _video_capture_loop(self):
        device_id = self.device_info.device_id

        if isinstance(device_id, str) and device_id.isdigit():
            device_id = int(device_id)

        self._disable_usb_autosuspend(device_id)

        video_capture = self._try_open(device_id)

        if not video_capture.isOpened():
            fallbacks = ["/dev/cam"] + [i for i in range(6) if i != device_id]
            for fb in fallbacks:
                if isinstance(fb, str) and not os.path.exists(fb):
                    continue
                self._logger.info("Camera fallback: trying %s", fb)
                video_capture = self._try_open(fb)
                if video_capture.isOpened():
                    self._logger.info("Camera fallback success: %s", fb)
                    break

        if not video_capture.isOpened():
            raise ValueError(
                f"Failed to open video capture device: {self.device_info.device_id}"
            )

        video_capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))

        if self._max_width:
            video_capture.set(cv2.CAP_PROP_FRAME_WIDTH, self._max_width)
        if self._max_height:
            video_capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self._max_height)

        self._apply_camera_controls(video_capture)

        w = int(video_capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(video_capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        device_fps = video_capture.get(cv2.CAP_PROP_FPS)
        self.actual_width = w
        self.actual_height = h
        self.actual_fps = device_fps if device_fps and device_fps > 0 else None
        self._logger.info(
            "Camera negotiated mode: %dx%d @ %.1f fps (requested %sx%s)",
            w, h, device_fps, self._max_width, self._max_height,
        )

        new_w = min(w, self._max_width) if self._max_width else w
        new_h = min(h, self._max_height) if self._max_height else h

        size_ratio = min(new_w / w, new_h / h)

        last_time_frame = time.time()
        skip_time = (
            1 / self._fps if self._fps is not None and self._fps < device_fps else 0
        )

        idle_interval = 2.0

        freeze_sig: bytes | None = None
        freeze_since: float = 0.0

        corrupt_since: float = 0.0
        last_color_check: float = 0.0

        self._logger.info("Starting video capture device loop")
        try:
            while not self._stopped.is_set():
                # Throttle when no active consumers — sleep BEFORE read to avoid
                # burning CPU on blocking video_capture.read() at device FPS
                with self._consumers_lock:
                    has_consumers = self._active_consumers > 0
                if not has_consumers:
                    elapsed = time.time() - last_time_frame
                    if elapsed < idle_interval:
                        self._stopped.wait(min(idle_interval - elapsed, 0.5))
                        continue
                    video_capture.grab()
                    video_capture.grab()

                ret, frame = video_capture.read()

                if not ret:
                    # USB cameras (e.g. HD USB Camera 32e4:9230 on OrangePi) hit
                    # autosuspend after ~2s idle; the wakeup outlasts a single 1s retry.
                    self._logger.warning("Camera read() failed, retrying in 1s...")
                    time.sleep(1)
                    ret, frame = video_capture.read()
                    if not ret:
                        self._logger.warning("Camera read still failing — reopening device")
                        video_capture = self._reopen_with_backoff(
                            video_capture, device_id, "read failure"
                        )
                        if video_capture is None:
                            break
                        continue

                small = np.ascontiguousarray(frame[::32, ::32])
                sig: bytes = small.tobytes()
                now_mono = time.monotonic()
                if sig == freeze_sig:
                    if now_mono - freeze_since >= self._FREEZE_REOPEN_S:
                        self._logger.warning(
                            "Camera frozen — identical frames for %.0fs, reopening device",
                            now_mono - freeze_since,
                        )
                        video_capture = self._recover_isp_fault(
                            video_capture, device_id, now_mono, "ISP freeze"
                        )
                        if video_capture is None:
                            break
                        freeze_sig = None
                        freeze_since = 0.0
                        corrupt_since = 0.0
                        continue
                else:
                    freeze_sig = sig
                    freeze_since = now_mono

                # Color-corruption watchdog (see _COLOR_CORRUPT_REOPEN_S): throttled to
                # ~1 check/s; requires uninterrupted corruption.
                if now_mono - last_color_check >= 1.0:
                    last_color_check = now_mono
                    if self._looks_color_corrupt(small):
                        if corrupt_since == 0.0:
                            corrupt_since = now_mono
                        elif now_mono - corrupt_since >= self._COLOR_CORRUPT_REOPEN_S:
                            self._logger.warning(
                                "Camera color corruption — posterized green/magenta "
                                "frames for %.0fs, reopening device",
                                now_mono - corrupt_since,
                            )
                            video_capture = self._recover_isp_fault(
                                video_capture, device_id, now_mono, "color corruption"
                            )
                            if video_capture is None:
                                break
                            freeze_sig = None
                            freeze_since = 0.0
                            corrupt_since = 0.0
                            continue
                    else:
                        corrupt_since = 0.0

                frame_ts = time.time()

                if frame_ts - last_time_frame < skip_time:
                    continue
                else:
                    last_time_frame = frame_ts

                if size_ratio < 1.0:
                    frame = cv2.resize(frame, None, fx=size_ratio, fy=size_ratio)

                if self._rotate is not None:
                    if self._rotate == 180.0:
                        frame = cv2.rotate(frame, cv2.ROTATE_180)
                    elif self._rotate == 90.0:
                        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
                    elif self._rotate == -90.0:
                        frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
                    else:
                        h, w = frame.shape[:2]
                        center = (w // 2, h // 2)
                        M = cv2.getRotationMatrix2D(center, self._rotate, 1.0)
                        frame = cv2.warpAffine(frame, M, (w, h))

                z = self.zoom
                if z > 1.0:
                    fh, fw = frame.shape[:2]
                    cw, ch = int(fw / z), int(fh / z)
                    x0 = (fw - cw) // 2
                    y0 = (fh - ch) // 2
                    frame = cv2.resize(
                        frame[y0:y0 + ch, x0:x0 + cw],
                        (fw, fh),
                        interpolation=cv2.INTER_LINEAR,
                    )

                frame = cast(npt.NDArray[np.uint8], frame)
                response = VideoCaptureDeviceResponse(frame=frame)

                for callback in self.callbacks:
                    callback(self.device_info, response)

                self.last_response = response
        finally:
            # video_capture is None when a reopen path (read-failure or ISP-fault
            # recovery) returned None because stop() was requested mid-retry — the loop
            # then breaks straight here.
            if video_capture is not None:
                video_capture.release()

    def acquire_consumer(self):
        """Register an active consumer (e.g. MJPEG stream) for full-FPS capture."""
        with self._consumers_lock:
            self._active_consumers += 1

    def release_consumer(self):
        """Unregister an active consumer — throttles capture when none remain."""
        with self._consumers_lock:
            self._active_consumers = max(0, self._active_consumers - 1)

    @override
    def stop(self):
        super().stop()
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def capture_still(
    cap,
    animation_service=None,
    settle_s: float = 0.3,
    timeout_s: float = 2.0,
) -> npt.NDArray[np.uint8] | None:
    """Grab a frame guaranteed to be captured while the servos were quiet.

    Best effort: on timeout returns the latest frame anyway (a possibly blurred answer
    beats none); returns None only when the camera never delivered a frame at all.
    """
    if cap is None:
        return None
    frozen = False
    if animation_service is not None:
        try:
            animation_service.freeze()
            frozen = True
        except Exception:
            pass
    cap.acquire_consumer()
    try:
        entry = time.monotonic()
        deadline = entry + max(timeout_s, 0.05)
        min_fresh = entry - 0.15
        while True:
            quiet_from = min_fresh
            if animation_service is not None:
                last_write = getattr(animation_service, "last_servo_write", 0.0)
                if last_write:
                    quiet_from = max(quiet_from, last_write + settle_s)
            frame_ts = getattr(cap, "last_frame_ts", 0.0)
            if frame_ts and frame_ts >= quiet_from:
                frame = cap.last_frame
                if frame is not None:
                    return frame
            if time.monotonic() >= deadline:
                return cap.last_frame
            time.sleep(0.03)
    finally:
        cap.release_consumer()
        if frozen:
            animation_service.unfreeze()
