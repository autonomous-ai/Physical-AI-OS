"""Camera route handlers -- all /camera/* endpoints."""

import os
import time
import threading
from functools import wraps

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse

import hal.app_state as state
from hal import privacy
from hal.models import CameraInfoResponse, CameraZoomRequest, StatusResponse
from hal.config import CAMERA_WIDTH, CAMERA_HEIGHT

router = APIRouter(tags=["Camera"])
_snapshot_lock = threading.Lock()


def _serialized_snapshot(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        # Serialize temporary starts/stops, without holding the privacy lock:
        # physical and manual disable must remain responsive during capture.
        with _snapshot_lock:
            return fn(*args, **kwargs)
    return wrapped


# Lazy import -- cv2 may not be available
cv2 = None
try:
    import cv2
except ImportError:
    pass


def _camera_info_payload() -> dict:
    """Build the CameraInfoResponse dict from current device + state."""
    available = state.camera_capture is not None and cv2 is not None
    cap = state.camera_capture
    actual_w = getattr(cap, "actual_width", None) if available else None
    actual_h = getattr(cap, "actual_height", None) if available else None
    actual_fps = getattr(cap, "actual_fps", None) if available else None
    return {
        "available": available,
        # `available` only says a capture object exists. It stays True when the
        # USB camera never enumerated (no /dev/video*, every open failed) because
        # the capture object is still created; the only truthful signal for
        # "can this device see" is whether a frame ever arrived — the same test
        # GET /health uses for its `camera` flag.
        "has_frame": bool(available and cap.last_frame is not None),
        # Prefer the device-negotiated mode; fall back to configured values
        # until the capture loop has reported (e.g. camera disabled at boot).
        "width": actual_w if actual_w else (CAMERA_WIDTH if available else None),
        "height": actual_h if actual_h else (CAMERA_HEIGHT if available else None),
        "fps": actual_fps,
        "disabled": state._camera_disabled,
        "manual_override": state._camera_manual_override,
        "zoom": getattr(cap, "zoom", 1.0) if available else 1.0,
    }


@router.get("/camera", response_model=CameraInfoResponse)
def get_camera_info():
    """Get camera availability, negotiated resolution, FPS, zoom."""
    return _camera_info_payload()


@router.post("/camera/zoom", response_model=CameraInfoResponse)
def set_camera_zoom(req: CameraZoomRequest):
    """Set digital zoom factor (1.0 = no zoom, applies to all frame consumers).

    Side effect: zoom > 1 narrows the FOV seen by sensing (face recog, motion,
    pose, emotion) and tracking. Use for focusing on a small subject (e.g.
    laptop screen in a video call); set back to 1.0 to restore wide view.
    """
    if not state.camera_capture:
        raise HTTPException(503, "Camera not available")
    state.camera_capture.zoom = req.zoom
    state.logger.info("Camera zoom set to %.2f", req.zoom)
    return _camera_info_payload()


@router.post("/camera/disable", response_model=StatusResponse)
@privacy.serialized
def disable_camera():
    """Stop the camera capture loop (manual). Sets manual override."""
    if not state.camera_capture:
        raise HTTPException(503, "Camera not available")
    if privacy.camera_muted:
        privacy.camera_before = True
        state._camera_manual_override = True
        state._persist_camera_state()
        return {"status": "already_disabled"}
    if state._camera_disabled:
        return {"status": "already_disabled"}
    state._camera_disabled = True
    state._camera_manual_override = True
    state.camera_capture.stop()
    state._persist_camera_state()
    state.logger.info("Camera disabled by user (manual override set)")
    return {"status": "ok"}


@router.post("/camera/enable", response_model=StatusResponse)
@privacy.serialized
def enable_camera():
    """Restart the camera capture loop (manual). Clears manual override."""
    if privacy.camera_muted:
        raise HTTPException(409, "Privacy switch is on -- flip the switch to enable camera")
    if not state.camera_capture:
        raise HTTPException(503, "Camera not available")
    if not state._camera_disabled:
        return {"status": "already_enabled"}
    state._camera_disabled = False
    state._camera_manual_override = False
    state.camera_capture.start()
    state._persist_camera_state()
    state.logger.info("Camera re-enabled by user (manual override cleared)")
    return {"status": "ok"}


@router.get("/camera/snapshot")
@_serialized_snapshot
def camera_snapshot(
    save: bool = False,
    width: int | None = Query(default=None, ge=1, le=4096, description="Resize output width (preserves aspect ratio). Capped at source width — never upscales."),
    height: int | None = Query(default=None, ge=1, le=4096, description="Resize output height (preserves aspect ratio). Capped at source height — never upscales."),
    quality: int = Query(default=85, ge=1, le=100, description="JPEG quality 1-100."),
):
    """Capture a single JPEG frame from the camera (freezes servos for stability).

    Optional resize: pass width and/or height to downscale the output. Aspect
    ratio is preserved; if both given, the frame is fit inside the requested
    box. Upscaling above source is not allowed (just blurs without detail) —
    requests above source are clamped.
    """
    if privacy.camera_muted or (state._camera_disabled and state._camera_manual_override):
        raise HTTPException(409, "Camera is disabled by the user or privacy switch")
    if not state.camera_capture or cv2 is None:
        raise HTTPException(503, "Camera not available")

    # Lazy import: video_capture_device imports cv2 at module level, and this
    # route module must stay importable on cv2-less devices (server.py imports
    # it unconditionally). Guarded by the cv2 check above.
    from hal.drivers.camera.video_capture_device import capture_still

    was_disabled = state._camera_disabled
    if was_disabled:
        state.camera_capture.start()

    try:
        # Freezes servos (animation loop + tracker worker, when the device has
        # them) and waits for a frame captured after the arm went quiet, using
        # frame/bus-write timestamps instead of a blind sleep. Zero added
        # latency when the servos are already still. animation_service is None
        # on servo-less devices — capture_still then just grabs the frame.
        frame = capture_still(
            state.camera_capture,
            state.animation_service,
            settle_s=0.3,
            timeout_s=2.5,
        )
        if frame is None:
            # Distinguish "hardware never delivered a frame" from a transient
            # miss. lamp-0c4e 2026-09-16: the USB camera was not enumerated at
            # all, every capture failed since boot, but the bare 500 read as a
            # hiccup and the agent retried through a second endpoint — another
            # 6s LLM hop for the same failure. Say it is not retryable.
            if getattr(state.camera_capture, "last_frame_ts", 0.0) == 0.0:
                raise HTTPException(
                    503,
                    "Camera hardware is not delivering frames (no frame since HAL "
                    "start) -- the camera is not connected or not detected; "
                    "retrying will not help",
                )
            raise HTTPException(500, "Failed to capture frame")
    finally:
        if was_disabled and state._camera_disabled:
            state.camera_capture.stop()

    if privacy.camera_muted or (state._camera_disabled and state._camera_manual_override):
        raise HTTPException(409, "Camera is disabled by the user or privacy switch")

    if width is not None or height is not None:
        src_h, src_w = frame.shape[:2]
        # Compute target scale honoring aspect ratio, clamped so we never
        # upscale (digital upscale adds no detail, only blur).
        scale_w = (width / src_w) if width else 1.0
        scale_h = (height / src_h) if height else 1.0
        if width and height:
            scale = min(scale_w, scale_h)
        else:
            scale = scale_w if width else scale_h
        scale = min(scale, 1.0)
        if scale < 1.0:
            new_w = max(1, int(src_w * scale))
            new_h = max(1, int(src_h * scale))
            frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)

    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])

    if privacy.camera_muted or (state._camera_disabled and state._camera_manual_override):
        raise HTTPException(409, "Camera is disabled by the user or privacy switch")
    if not save:
        return Response(content=buf.tobytes(), media_type="image/jpeg")

    os.makedirs(state._SNAPSHOT_DIR, exist_ok=True)
    filename = f"snap_{int(time.time() * 1000)}.jpg"
    filepath = os.path.join(state._SNAPSHOT_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(buf.tobytes())
    state._snapshot_paths.append(filepath)

    while len(state._snapshot_paths) > state._SNAPSHOT_MAX:
        oldest = state._snapshot_paths.pop(0)
        try:
            os.remove(oldest)
        except OSError:
            pass

    return JSONResponse({"path": filepath})


@router.get("/camera/stream")
def camera_stream():
    """MJPEG stream from the camera."""
    if not state.camera_capture or cv2 is None or state._camera_disabled or privacy.camera_muted:
        raise HTTPException(503, "Camera disabled" if state._camera_disabled else "Camera not available")

    stream_fps = float(os.environ.get("HAL_CAMERA_STREAM_FPS", "10"))
    stream_width = int(os.environ.get("HAL_CAMERA_STREAM_WIDTH", "320"))
    stream_quality = int(os.environ.get("HAL_CAMERA_STREAM_JPEG_QUALITY", "65"))
    min_interval_s = 1.0 / stream_fps if stream_fps > 0 else 0.0

    def generate():
        state.camera_capture.acquire_consumer()
        try:
            last_sent_s = 0.0
            while not state._camera_disabled and not privacy.camera_muted:
                if min_interval_s > 0:
                    now_s = time.time()
                    elapsed_s = now_s - last_sent_s
                    if elapsed_s < min_interval_s:
                        time.sleep(min(0.01, min_interval_s - elapsed_s))
                        continue

                frame = state.camera_capture.last_frame
                if frame is None:
                    time.sleep(0.05)
                    continue

                # Draw tracking bbox overlay if active
                if state.tracker_service and state.tracker_service.is_tracking:
                    ts = state.tracker_service.status
                    bbox = ts.get("bbox")
                    if bbox:
                        x, y, w, h = bbox
                        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
                        label = ts.get("target") or "tracking"
                        cv2.putText(frame, label, (x, y - 8),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

                if stream_width and frame.shape[1] > stream_width:
                    scale = stream_width / float(frame.shape[1])
                    frame = cv2.resize(
                        frame,
                        None,
                        fx=scale,
                        fy=scale,
                        interpolation=cv2.INTER_AREA,
                    )

                _, buf = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(stream_quality)]
                )
                if privacy.camera_muted:
                    break
                last_sent_s = time.time()
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
                )
        finally:
            state.camera_capture.release_consumer()

    return StreamingResponse(
        generate(), media_type="multipart/x-mixed-replace; boundary=frame"
    )
