"""Servo route handlers — all /servo/* endpoints.

Thin delegates: each handler validates the HTTP request, then calls one method
on state.animation_service (which satisfies the MotionService protocol from
hal/drivers/motors/base.py). No driver internals (.robot, .bus, .bus_lock,
raw encoder values) leak into this file.
"""

import csv
import io
import math
import os
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, File, Form, UploadFile

import hal.app_state as state
from hal.safety.policy import min_move_duration
from hal.models import (
    ServoAimRequest,
    ServoDemoResponse,
    ServoSearchRequest,
    ServoSearchResponse,
    ServoAimResponse,
    ServoNudgeRequest,
    ServoMoveRequest,
    ServoMoveResponse,
    ServoPlayResponse,
    ServoPositionResponse,
    ServoRequest,
    ServoStateResponse,
    ServoStatusResponse,
    ServoTrackRequest,
    ServoTrackResponse,
    StatusResponse,
)
from hal.presets import (
    AIM_PRESETS,
    SERVO_CMD_PLAY,
)

router = APIRouter(tags=["Servo"])


def _sleep_servo_locked() -> bool:
    """True for the whole sleep, not just after torque-off.

    Gating on `_sleep_servo_released` alone left the window between the sleepy
    emotion arriving and the auto-release timer firing wide open: a late
    /servo/play from the agent would drive the bus while release() was ramping
    to gravity-rest, so torque got cut mid-pose and the body slammed. Sleep is
    a terminal state — nothing external touches the servos until a wake
    emotion clears `_sleeping`. The wake path itself resumes the motion
    service in-process (see routes/emotion.py), not through these routes, so
    widening the gate cannot lock the device out of waking."""
    return state._sleeping

# --- Constants ---

_SERVO_JOINT_FIELD_RE = re.compile(r"^[A-Za-z0-9_]+\.pos$")
_MAX_SERVO_RECORDING_UPLOAD_BYTES = 2 * 1024 * 1024  # 2MB
_MAX_SERVO_RECORDING_ROWS = 20000


def _sanitize_recording_name(name: str) -> str:
    name = (name or "").strip()
    name = re.sub(r"[^a-zA-Z0-9_-]+", "_", name)
    name = name.strip("_- ")
    if not name:
        raise ValueError("empty recording name")
    return name[:64]


def _svc():
    """Return the animation service or raise 503."""
    svc = state.animation_service
    if not svc:
        raise HTTPException(503, "Servo not available")
    return svc


def _svc_connected():
    """Return the animation service, checking it is connected, or raise 503."""
    svc = _svc()
    if not svc.is_connected:
        raise HTTPException(503, "Servo robot not connected")
    return svc


# --- Endpoints ---


@router.get("/servo", response_model=ServoStateResponse)
def get_servo_state():
    """Get available recordings and current animation state."""
    svc = _svc()
    return {
        "available_recordings": svc.get_available_recordings(),
        "current": svc._current_recording,
        "motion_mode": svc.motion_mode,
    }


@router.post("/servo/upload", response_model=StatusResponse)
async def upload_servo_recording(
    file: UploadFile = File(...),
    recording_name: Optional[str] = Form(None),
):
    """Upload a servo recording CSV and make it available in GET /servo."""
    svc = _svc()

    orig_filename = file.filename or "recording.csv"
    if orig_filename.lower().endswith(".csv") is False:
        raise HTTPException(400, "upload must be a .csv file")

    rec_name = recording_name or Path(orig_filename).stem
    try:
        rec_name = _sanitize_recording_name(rec_name)
    except ValueError as e:
        raise HTTPException(400, str(e))

    content = await file.read()
    if len(content) == 0:
        raise HTTPException(400, "empty csv")
    if len(content) > _MAX_SERVO_RECORDING_UPLOAD_BYTES:
        raise HTTPException(
            413, f"csv too large (max {_MAX_SERVO_RECORDING_UPLOAD_BYTES} bytes)"
        )

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "csv must be utf-8 text")

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []

    if "timestamp" not in fieldnames:
        raise HTTPException(400, 'missing required column "timestamp"')

    joint_fields = [f for f in fieldnames if f != "timestamp"]
    if not joint_fields:
        raise HTTPException(400, "missing joint columns (expected *.pos fields)")

    invalid_joint_fields = [f for f in joint_fields if not _SERVO_JOINT_FIELD_RE.match(f)]
    if invalid_joint_fields:
        raise HTTPException(
            400, f"invalid joint columns: {invalid_joint_fields}. Expected <name>.pos"
        )

    valid_joints = svc.get_joint_names() or None
    if valid_joints is not None:
        unknown = [j for j in joint_fields if j not in valid_joints]
        if unknown:
            raise HTTPException(
                400,
                f"unknown joint columns: {unknown}. Valid: {sorted(valid_joints)}",
            )

    actions: list[dict[str, float]] = []
    for row_idx, row in enumerate(reader):
        if len(actions) >= _MAX_SERVO_RECORDING_ROWS:
            raise HTTPException(
                400, f"too many rows (max {_MAX_SERVO_RECORDING_ROWS})"
            )

        ts_val = row.get("timestamp")
        try:
            _ = float(ts_val)
        except Exception:
            raise HTTPException(400, f"invalid timestamp at row {row_idx + 2}")

        action: dict[str, float] = {}
        for joint in joint_fields:
            v = row.get(joint)
            if v is None or v == "":
                raise HTTPException(400, f"missing value for {joint} at row {row_idx + 2}")
            try:
                action[joint] = float(v)
            except Exception:
                raise HTTPException(400, f"invalid float for {joint} at row {row_idx + 2}")

        actions.append(action)

    recordings_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "recordings")
    Path(recordings_dir).mkdir(parents=True, exist_ok=True)
    csv_path = os.path.join(recordings_dir, f"{rec_name}.csv")

    with open(csv_path, "w", newline="") as f:
        f.write(text if text.endswith("\n") else text + "\n")

    try:
        svc.add_recording(rec_name, actions)
    except Exception:
        pass

    return {"status": "ok"}


@router.post("/servo/play", response_model=ServoPlayResponse, response_model_exclude_none=True)
def play_recording(req: ServoRequest):
    """Play a pre-recorded servo animation by name.

    Both refusals answer "ignored", not "ok": the caller must be able to tell a
    body that moved from one that did not.
    """
    state.logger.debug("POST /servo/play recording=%s", req.recording)
    if _sleep_servo_locked():
        state.logger.info("servo/play ignored -- device is sleeping")
        return {"status": "ignored", "reason": "sleeping"}
    svc = _svc()
    if svc.is_suppressed:
        # INFO, not DEBUG: at the default log level this refusal left no trace.
        state.logger.info("servo/play ignored -- %s mode active", svc.motion_mode)
        return {"status": "ignored", "reason": svc.motion_mode}
    svc.ensure_running()
    t0 = time.perf_counter()
    svc.dispatch(SERVO_CMD_PLAY, req.recording)
    state.logger.debug("servo dispatch took %.1fms", (time.perf_counter() - t0) * 1000)
    return {"status": "ok"}


@router.post("/servo/resume", response_model=StatusResponse)
def resume_servos():
    """Exit zero-hold mode and resume normal animation loop (plays idle)."""
    if _sleep_servo_locked():
        state.logger.info("servo/resume blocked -- device is sleeping")
        return {"status": "ok"}
    svc = _svc()
    svc.resume()
    return {"status": "ok"}


@router.post("/servo/hold", response_model=StatusResponse)
def hold_servos():
    """Hold current pose -- suppress idle/ambient animations, torque stays ON."""
    svc = _svc()
    svc.hold(explicit=True)
    return {"status": "ok"}


@router.post("/servo/move", response_model=ServoMoveResponse)
def move_servo(req: ServoMoveRequest):
    """Send joint positions to servo motors with smooth interpolation."""
    if _sleep_servo_locked():
        state.logger.info("servo/move blocked -- device is sleeping")
        return {"status": "ok", "requested": req.positions, "clamped": req.positions, "duration": req.duration}
    svc = _svc_connected()
    valid_joints = svc.get_joint_names()
    unknown = [j for j in req.positions if j not in valid_joints]
    if unknown:
        raise HTTPException(
            400, f"Unknown joints: {unknown}. Valid: {sorted(valid_joints)}"
        )

    # Safety gate (SAFETY.md motion.max_speed) — stretch the duration so no
    # joint exceeds the ceiling. A declared speed bound requires a known pose.
    current = {}
    try:
        current = svc.get_positions()
    except Exception as e:
        state.logger.warning("move: could not read current pose for speed clamp: %s", e)
    policy = state.safety_policy
    if policy and policy.motion and policy.motion.max_speed is not None:
        try:
            known_pose = all(math.isfinite(float(current[joint])) for joint in req.positions)
        except (KeyError, TypeError, ValueError, OverflowError):
            known_pose = False
        if not known_pose:
            raise HTTPException(503, "Cannot enforce motion speed limit without a valid current pose")
    eff_duration = min_move_duration(policy, req.positions, current, req.duration)

    errors = {}

    try:
        svc.move_and_hold(req.positions, duration=eff_duration)
    except Exception as e:
        errors["move"] = str(e)

    try:
        obs = svc.get_positions()
        for joint, target in req.positions.items():
            actual = obs.get(joint)
            if actual is not None:
                error = abs(actual - target)
                if error > 5.0:
                    errors[joint] = (
                        f"position error {error:.1f} deg (target={target:.1f}, actual={actual:.1f})"
                    )
    except Exception as e:
        errors["read_position"] = str(e)

    return {
        "status": "error" if "move" in errors else "ok",
        "requested": req.positions,
        "clamped": req.positions,
        "duration": eff_duration,
        "errors": errors if errors else None,
    }


@router.post("/servo/zero", response_model=StatusResponse)
def zero_servos():
    """Move all servos to 0 deg and hold (torque stays ON)."""
    svc = _svc_connected()
    svc.zero_pose()
    return {"status": "ok"}


@router.post("/servo/release", response_model=StatusResponse)
def release_servos():
    """Move servos to idle position then disable torque (safe release)."""
    svc = _svc_connected()
    # Stop the vision tracker FIRST — it drives the servo bus from its own
    # worker thread, so if it's live when we cut torque the arm re-engages.
    # TODO(reachy): tracker_service reaches into .robot/.bus_lock — port to
    # MotionService accessors when vision tracking goes multi-device.
    if state.tracker_service and state.tracker_service.is_tracking:
        try:
            state.logger.info("release: stopping vision tracker before torque-off")
            state.tracker_service.stop()
        except Exception as e:
            state.logger.warning(f"tracker stop before release failed: {e}")
    errors = svc.release()
    if errors:
        state.logger.warning(f"Servo release errors (offline?): {errors}")
    return {"status": "ok"}


@router.post("/servo/stop", response_model=StatusResponse)
def stop_servos():
    """Deterministic stop: abort motion in flight and HOLD. Torque stays ON.

    Not /servo/release, which travels to a rest pose and then cuts torque — a
    stop that moves first is wrong for anything with legs or wheels
    (ROBOT-SPEC / COMPATIBILITY rule 6). Not safety-gated either: `motion.
    stop_always` says a stop is never clamped, delayed or refused, so this
    route reads no bound and takes no arguments.
    """
    # Policy execution is currently dry-run only, but the stop relationship is
    # part of the interface now: the future executor must be cancelled before
    # the hardware hold is requested.  This call never generates a target.
    if state.policy_service is not None:
        try:
            state.policy_service.stop()
        except Exception as e:
            state.logger.warning("stop: policy cancellation failed: %s", e)

    # The tracker drives the bus from its own worker thread. Stop it first or it
    # keeps writing goals and the "stop" holds nothing — same ordering the
    # release path needs, and for the same reason.
    if state.tracker_service and state.tracker_service.is_tracking:
        try:
            state.logger.info("stop: halting vision tracker")
            state.tracker_service.stop()
        except Exception as e:
            state.logger.warning(f"tracker stop during halt failed: {e}")
    svc = _svc_connected()
    svc.halt()
    return {"status": "ok"}


@router.get("/servo/position", response_model=ServoPositionResponse)
def get_servo_position():
    """Read current servo joint positions."""
    svc = _svc_connected()
    try:
        positions = svc.get_positions()
        return {"positions": positions}
    except Exception as e:
        raise HTTPException(500, f"Failed to read position: {e}")


@router.get("/servo/status", response_model=ServoStatusResponse)
def get_servo_status():
    """Ping each servo and return per-joint online/offline status with angle."""
    svc = _svc_connected()
    try:
        servos = svc.joint_status()
        return {"servos": servos}
    except Exception as e:
        raise HTTPException(500, f"Failed to get servo status: {e}")


@router.get("/servo/aim")
def list_aim_directions():
    """List available aim directions."""
    return {"directions": list(AIM_PRESETS.keys())}


@router.post("/servo/aim", response_model=ServoAimResponse)
def aim_servo(req: ServoAimRequest):
    """Aim the device head to a named direction."""
    if _sleep_servo_locked():
        return {"status": "ok", "direction": req.direction, "positions": {}}
    svc = _svc_connected()
    try:
        current = svc.get_positions()
    except Exception as e:
        raise HTTPException(500, f"Failed to read current position: {e}")
    try:
        positions = svc.aim(req.direction, req.duration, current, state.safety_policy)
        return {"status": "ok", "direction": req.direction, "positions": positions}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Servo aim failed: {e}")


@router.post("/servo/search", response_model=ServoSearchResponse)
def search_for_user(req: Optional[ServoSearchRequest] = None):
    """Sweep for a subject and report what was found, with a frame to show.

    Deliberately NOT what the look-aim does. The aim runs inside a live turn
    under a deadline; this takes seconds, so it is only entered when the user
    asked for it ("where are you?", "find my cup") or accepted an offer after a
    failed look.

    Seeded from the remembered bearing and expanding outward, so the likely
    place is checked first.

    Meant to be called with curl DURING a turn, not from a [HW:...] marker:
    markers fire after the reply is already written, so a marker-driven search
    can never speak its own result. The response body is the answer, and
    `image_path` is what the user is shown.
    """
    from hal.drivers.tracking.search import search_for_subject

    # An empty body is the common case — every existing caller sends nothing.
    req = req or ServoSearchRequest()
    res = search_for_subject(target=req.target, exhaustive=req.exhaustive)
    where = (f" at yaw {res.found_at_yaw:+.0f}"
             if res.found and res.found_at_yaw is not None else "")
    return {
        "status": "ok",
        "message": (f"{res.reason}{where} after {res.looks_visited} look(s) "
                    f"across {res.bearings_visited} bearing(s)"),
        "found": res.found,
        "target": req.target,
        "kind": res.kind,
        "found_at_yaw": res.found_at_yaw,
        "found_at_roll": res.found_at_roll,
        "centred": res.centred,
        "image_path": res.image_path,
        "looks_visited": res.looks_visited,
        "bearings_visited": res.bearings_visited,
    }


@router.post("/servo/demo", response_model=ServoDemoResponse)
def range_demo_route():
    """Perform a narrated tour of the movement range.

    A DEMO, not a search: nothing is detected and nothing is reported. It exists
    because "show me how far you can move" used to land on the `scan` emotion — a
    54 deg canned recording narrated as a full turn — for want of anywhere else
    to go.

    Returns as soon as the demo starts. The performance runs on its own thread
    and speaks for itself through the filler pools, so the agent's five-second
    marker budget is never in play.
    """
    if _sleep_servo_locked():
        state.logger.info("servo/demo ignored -- device is sleeping")
        return {"status": "ok", "started": False, "waypoints": 0,
                "reason": "sleeping"}
    svc = _svc_connected()
    if svc.is_suppressed:
        state.logger.info("servo/demo ignored -- %s mode active", svc.motion_mode)
        return {"status": "ok", "started": False, "waypoints": 0,
                "reason": svc.motion_mode}

    from hal.drivers.motors.range_demo import start

    return {"status": "ok", **start(svc)}


@router.get("/servo/bearing")
def get_user_bearing():
    """Inspect the remembered user bearing.

    Exists so the estimate can be checked without SSH-ing to the device. The
    thing to look for is `bearing_deg` settling near where the user actually
    sits — an estimate mirrored about zero means the yaw sign is inverted, which
    is silent otherwise because this value is open-loop.
    """
    from hal.drivers.tracking import user_bearing

    est = user_bearing.read_estimate()
    if est is None:
        # None, not 0.0 — dead ahead is a real bearing, "unknown" must differ.
        return {"status": "ok", "known": False}
    return {
        "status": "ok",
        "known": True,
        "bearing_deg": est.bearing_deg,
        "confidence": est.confidence,
        "samples": est.samples,
        "age_s": round(est.age_s, 1),
        # The full remembered posture. An empty pose on a known bearing means
        # the estimate predates the pose schema and has not been re-sighted yet,
        # so a search will restore direction but not head height.
        "pose": est.pose,
    }


@router.post("/servo/bearing/reset", response_model=StatusResponse)
def reset_user_bearing():
    """Forget where the user usually is — "I moved you".

    The escape hatch for a relocated lamp. Automatic detection needs several
    failed predictions before it acts, which is right for avoiding false
    positives but slow when the user already KNOWS the lamp moved.
    """
    from hal.drivers.tracking import user_bearing

    user_bearing.clear()
    return {"status": "ok", "message": "user bearing cleared"}


@router.post("/servo/nudge", response_model=ServoAimResponse)
def nudge_servo(req: ServoNudgeRequest):
    """Move servo by relative degrees from current position."""
    if _sleep_servo_locked():
        return {"status": "ok", "direction": f"nudge yaw={req.yaw} pitch={req.pitch}", "positions": {}}
    svc = _svc_connected()
    try:
        current = svc.get_positions()
        positions = svc.nudge(req.yaw, req.pitch, req.duration, current, state.safety_policy)
        return {"status": "ok", "direction": f"nudge yaw={req.yaw} pitch={req.pitch}", "positions": positions}
    except Exception as e:
        raise HTTPException(500, f"Servo nudge failed: {e}")


@router.post("/servo/track", response_model=ServoTrackResponse)
def start_tracking(req: ServoTrackRequest):
    """Start tracking an object by bounding box. Servo follows the object in real-time."""
    if _sleep_servo_locked():
        state.logger.info("servo/track blocked -- device is sleeping")
        return {"status": "ok", "tracking": False, "target": None, "bbox": None, "confidence": None}
    if not state.tracker_service:
        raise HTTPException(503, "Tracker service not available")
    if not state.animation_service:
        raise HTTPException(503, "Servo not available")
    if not state.camera_capture:
        raise HTTPException(503, "Camera not available")

    bbox = tuple(req.bbox) if req.bbox else None
    # TODO(reachy): tracker_service receives animation_service and reaches into
    # .robot/.bus_lock internally — port to MotionService accessors when vision
    # tracking goes multi-device.
    ok = state.tracker_service.start(
        bbox=bbox,
        target_label=req.target,
        camera_capture=state.camera_capture,
        animation_service=state.animation_service,
    )
    if not ok:
        raise HTTPException(400, state.tracker_service.last_error or "Failed to initialize tracker")

    s = state.tracker_service.status
    return {
        "status": "ok",
        "tracking": True,
        "target": s.get("target"),
        "bbox": s.get("bbox"),
        "confidence": s.get("confidence"),
    }


@router.post("/servo/track/stop", response_model=ServoTrackResponse)
def stop_tracking():
    """Stop the current tracking session."""
    if not state.tracker_service:
        raise HTTPException(503, "Tracker service not available")

    state.tracker_service.stop()
    return {"status": "ok", "tracking": False}


@router.get("/servo/track", response_model=ServoTrackResponse)
def get_tracking_status():
    """Get current tracking status."""
    if not state.tracker_service:
        raise HTTPException(503, "Tracker service not available")

    s = state.tracker_service.status
    return {
        "status": "ok",
        "tracking": s["tracking"],
        "target": s["target"],
        "bbox": s["bbox"],
        "confidence": s.get("confidence"),
    }


@router.post("/servo/track/update", response_model=ServoTrackResponse)
def update_tracking_bbox(req: ServoTrackRequest):
    """Re-initialize tracker with a new bounding box."""
    if not state.tracker_service:
        raise HTTPException(503, "Tracker service not available")
    if not state.tracker_service.is_tracking:
        raise HTTPException(400, "No active tracking session")

    bbox = tuple(req.bbox)
    ok = state.tracker_service.update_bbox(bbox, camera_capture=state.camera_capture)
    if not ok:
        raise HTTPException(400, "Failed to re-initialize tracker")

    s = state.tracker_service.status
    return {
        "status": "ok",
        "tracking": True,
        "target": s.get("target"),
        "bbox": list(bbox),
    }
