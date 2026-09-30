"""Turning toward the lamp as a way of addressing it."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

import hal.config as config

logger = logging.getLogger(__name__)

_stop = threading.Event()
_thread: Optional[threading.Thread] = None

# (monotonic timestamp, |head yaw| in degrees, face height in pixels, |horizontal offset
# of the face from frame centre| as a fraction, 0 at the centre and 1 at the edge).
_samples: Deque[Tuple[float, float, float, float]] = deque()
_samples_lock = threading.Lock()

_last_grant_t: float = 0.0
_last_face_t: float = 0.0
_last_subject_t: float = 0.0
_last_repoint_t: float = 0.0
_last_dy_frac: Optional[float] = None
_last_dy_from_face: bool = False
_last_pitch_t: float = 0.0
_repoint_pending_t: float = 0.0
_repoint_subject_t_before: float = 0.0
# Last time a face near enough to be at the desk (user_check.near_enough) was seen, and
# last time only a smaller one was. The repoint verdict is judged on these (#545).
_last_near_face_t: float = 0.0
_last_far_face_t: float = 0.0
# A repoint that landed on a body is waiting for its climb; 0.0 when none is.
_repoint_climb_t: float = 0.0
_blind_pitch_steps: int = 0
_climb_gave_up: bool = False
# A VAD-confirmed utterance found no recent usable face. The watcher consumes
# this request and restores the remembered pose without blocking the mic loop;
# the completed utterance gets one final gaze check after the camera settles.
_speech_repoint_requested = threading.Event()
_speech_repoint_requested_t: float = 0.0

_dy_samples: Deque[Tuple[float, float, bool]] = deque()
_dy_lock = threading.Lock()

_last_frame: Any = None
_last_box: Any = None

# Must begin with "sensing_" to be servable by the monitor snapshot route.
SNAPSHOT_CATEGORY: str = "sensing_gaze"


def _save_snapshot(label: str) -> Optional[str]:
    """Write the frame this correction was computed from, annotated. Never raises."""
    if not getattr(config, "GAZE_SNAPSHOT_ENABLED", True) or _last_frame is None:
        return None
    try:
        import os

        from hal.drivers.tracking.look_debug import encode_annotated

        # both_axes: this loop servos on dy, so the horizontal pair is the
        # one that shows the error being corrected. The aim's view stays
        # vertical-only — it can only move yaw.
        jpg = encode_annotated(_last_frame, _last_box, label, both_axes=True)
        if jpg is None:
            return None
        root = getattr(config, "SNAPSHOT_PERSIST_DIR", "/var/lib/hal/snapshots")
        directory = os.path.join(root, SNAPSHOT_CATEGORY)
        os.makedirs(directory, exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S") + ".jpg"
        path = os.path.join(directory, name)
        with open(path, "wb") as f:
            f.write(jpg)
        keep = int(getattr(config, "GAZE_SNAPSHOT_KEEP", 40) or 0)
        if keep > 0:
            stale = sorted(
                (f for f in os.listdir(directory) if f.endswith(".jpg")),
                reverse=True,
            )[keep:]
            for old in stale:
                try:
                    os.unlink(os.path.join(directory, old))
                except OSError:
                    pass
        return path
    except Exception as e:
        logger.debug("[gaze] snapshot skipped: %s", e)
        return None


def record_dy(dy: Optional[float], from_face: bool,
              now: Optional[float] = None) -> None:
    """Fold one vertical-offset measurement into the pitch window."""
    if dy is None:
        return
    t = time.monotonic() if now is None else now
    with _dy_lock:
        _dy_samples.append((t, float(dy), bool(from_face)))
        cutoff = t - max(1.0, config.GAZE_PITCH_WINDOW_S)
        while _dy_samples and _dy_samples[0][0] < cutoff:
            _dy_samples.popleft()


_dx_samples: Deque[Tuple[float, float, bool]] = deque()
_dx_lock = threading.Lock()
_last_yaw_t: float = 0.0
_last_sweep_t: float = 0.0


def record_dx(dx: Optional[float], from_face: bool,
              now: Optional[float] = None) -> None:
    """Fold one horizontal-offset measurement into the pan window."""
    if dx is None:
        return
    t = time.monotonic() if now is None else now
    with _dx_lock:
        _dx_samples.append((t, float(dx), bool(from_face)))
        cutoff = t - max(1.0, config.GAZE_YAW_WINDOW_S)
        while _dx_samples and _dx_samples[0][0] < cutoff:
            _dx_samples.popleft()


def discard_dx_samples() -> None:
    """Drop the pan window — the offsets in it describe a pose already left."""
    with _dx_lock:
        _dx_samples.clear()


def _dx_estimate(now: Optional[float] = None):
    """``(median dx, n, span_s)`` over the pan window, or None."""
    t = time.monotonic() if now is None else now
    with _dx_lock:
        rows = [r for r in _dx_samples if r[0] >= t - config.GAZE_YAW_WINDOW_S]
    if len(rows) < config.GAZE_YAW_MIN_SAMPLES:
        return None
    span = rows[-1][0] - rows[0][0]
    if span < config.GAZE_YAW_WINDOW_S * 0.8:
        return None
    values = sorted(r[1] for r in rows)
    mid = len(values) // 2
    med = (values[mid] if len(values) % 2
           else (values[mid - 1] + values[mid]) / 2.0)
    return med, len(rows), span


def _dy_estimate(now: Optional[float] = None, *, prompt: bool = False):
    """``(median dy, from_face, n, span_s)`` over the window, or None."""
    t = time.monotonic() if now is None else now
    with _dy_lock:
        rows = [r for r in _dy_samples if r[0] >= t - config.GAZE_PITCH_WINDOW_S]
    if not rows:
        return None
    face_rows = [r for r in rows if r[2]]
    use = face_rows if face_rows else rows
    relaxed = prompt and not face_rows
    need = (config.GAZE_PITCH_PROMPT_MIN_SAMPLES if relaxed
            else config.GAZE_PITCH_MIN_SAMPLES)
    if len(use) < need:
        return None
    span = use[-1][0] - use[0][0]
    if relaxed:
        med = sorted(r[1] for r in use)[len(use) // 2]
        return med, False, len(use), span
    if span < config.GAZE_PITCH_WINDOW_S * 0.8:
        return None
    vals = sorted(v for _, v, _ in use)
    mid = len(vals) // 2
    med = vals[mid] if len(vals) % 2 else 0.5 * (vals[mid - 1] + vals[mid])
    return med, bool(face_rows), len(use), span


def head_yaw_deg(landmarks: Sequence[float]) -> Optional[float]:
    """Absolute head yaw in degrees from YuNet's five landmarks."""
    try:
        pts = [float(v) for v in landmarks[:10]]
    except (TypeError, ValueError):
        return None
    if len(pts) < 10 or not all(math.isfinite(v) for v in pts):
        return None

    (rx, ry), (lx, ly), (nx, ny) = (pts[0], pts[1]), (pts[2], pts[3]), (pts[4], pts[5])

    ex, ey = lx - rx, ly - ry
    eye_dist = math.hypot(ex, ey)
    if eye_dist < 1e-6:
        return None

    mid_x, mid_y = (rx + lx) / 2.0, (ry + ly) / 2.0
    offset = ((nx - mid_x) * ex + (ny - mid_y) * ey) / eye_dist

    ratio = offset / (eye_dist / 2.0)
    # Beyond the eyes the model has nothing left to say; clamp rather than let
    # asin raise on a landmark that drifted past its own eye.
    ratio = max(-1.0, min(1.0, ratio))
    return abs(math.degrees(math.asin(ratio)))


def landmarks_in_frame(landmarks: Sequence[float], width: float,
                       height: float) -> bool:
    """Whether every landmark the yaw is built from was actually SEEN."""
    try:
        pts = [float(v) for v in landmarks[:6]]
    except (TypeError, ValueError):
        return False
    if len(pts) < 6:
        return False
    for x, y in zip(pts[0::2], pts[1::2]):
        if not (math.isfinite(x) and math.isfinite(y)):
            return False
        if x < 0.0 or y < 0.0 or x > width or y > height:
            return False
    return True


def cone_for(edge_frac: float) -> float:
    """The acceptance cone that applies to a face this far off frame centre."""
    edge = max(0.0, min(1.0, edge_frac))
    scale = 1.0 + (max(1.0, config.GAZE_EDGE_CONE_SCALE) - 1.0) * edge
    return config.GAZE_MAX_YAW_DEG * scale


def facing_lamp(yaw_deg: Optional[float], face_px: float,
                edge_frac: float = 0.0) -> bool:
    """Whether one sample counts as the user facing the lamp.

    The size test comes first in spirit: below GAZE_MIN_FACE_PX the yaw is not a weak
    measurement, it is not a measurement at all, so it must never vote — not even to say
    "away".
    """
    if yaw_deg is None:
        return False
    return (
        face_px >= config.GAZE_MIN_FACE_PX
        and yaw_deg <= cone_for(edge_frac)
    )


def _prune(now: float) -> None:
    """Drop samples older than the buffer window. Caller holds the lock."""
    cutoff = now - max(1.0, config.GAZE_BUFFER_S)
    while _samples and _samples[0][0] < cutoff:
        _samples.popleft()


def record_sample(yaw_deg: Optional[float], face_px: float,
                  edge_frac: float = 0.0, now: Optional[float] = None) -> None:
    """Append one observation to the ring buffer."""
    t = time.monotonic() if now is None else now
    with _samples_lock:
        _samples.append(
            (t, float("inf") if yaw_deg is None else yaw_deg, face_px, edge_frac)
        )
        _prune(t)


def discard_samples() -> None:
    """Forget everything measured before the camera moved."""
    with _samples_lock:
        _samples.clear()
    with _dy_lock:
        _dy_samples.clear()


def snapshot() -> List[Tuple[float, float, float, float]]:
    with _samples_lock:
        return list(_samples)


def facing_ratio(now: Optional[float] = None) -> Tuple[float, int]:
    """``(fraction facing, samples examined)`` over the decision window.

    Speech is the trigger; the gesture it is evidence for happened just before it, never
    after, which is why nothing later than the trigger is ever consulted.
    """
    t = time.monotonic() if now is None else now
    samples = snapshot()
    if not samples:
        return 0.0, 0

    window = max(0.0, config.GAZE_WINDOW_S)
    if (t - samples[-1][0]) > window:
        return 0.0, 0

    # Only samples that actually MEASURED a head can vote, either way. Device-measured:
    # a user sitting still with a 93 px face, well centred, produced
    # [32,11,-,-,-,34,38,24] and scored 50% against a 60% bar.
    return _ratio_between(samples, t - window, t)


def _ratio_between(samples: List[Tuple[float, float, float, float]],
                   lo: float, hi: float) -> Tuple[float, int]:
    """``(fraction facing, samples examined)`` over ``[lo, hi]``."""
    measured = [
        s for s in samples
        if lo <= s[0] <= hi and s[1] != float("inf") and s[2] >= config.GAZE_MIN_FACE_PX
    ]
    if not measured:
        return 0.0, 0
    facing = sum(1 for _, yaw, px, edge in measured if facing_lamp(yaw, px, edge))
    return facing / float(len(measured)), len(measured)


def _headroom_from_person(frame: Any, detector: Any) -> Tuple[Optional[float], bool]:
    """``(vertical offset to correct by, whether a person was seen at all)``."""
    if frame is None or detector is None:
        return None, False
    try:
        box = detector.detect(frame, "person", strict=False,
                              min_conf=config.LOOK_AIM_MIN_CONFIDENCE)
    except TypeError:
        try:
            box = detector.detect(frame, "person", strict=False)
        except Exception:
            return None, False
    except Exception:
        return None, False
    if box is None:
        return None, False
    _, y, _, h = box
    frame_h = float(frame.shape[0]) or 1.0
    if float(h) / frame_h < config.LOOK_AIM_MIN_PERSON_HEIGHT_FRAC:
        return None, False
    if float(y) > 2.0:
        return None, True
    return -0.5, True


# Every joint the pitch loop steers. All three are vertical, so all three are
# what a remembered (and therefore stale) posture must not be allowed to undo.
PITCH_JOINTS = ("base_pitch.pos", "elbow_pitch.pos", "wrist_pitch.pos")

_pitch_stalls: Dict[str, Tuple[float, float, float]] = {}


def _pitch_travel_limits(now: float) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Narrowed travel for joints that recently failed to arrive."""
    travel_min: Dict[str, float] = {}
    travel_max: Dict[str, float] = {}
    for joint, (stopped_at, direction, until) in list(_pitch_stalls.items()):
        if now >= until:
            del _pitch_stalls[joint]
            logger.info("[gaze] %s rested, allowed back into the correction", joint)
            continue
        if direction > 0:
            travel_max[joint] = stopped_at - config.GAZE_PITCH_STALL_BACKOFF_DEG
        else:
            travel_min[joint] = stopped_at + config.GAZE_PITCH_STALL_BACKOFF_DEG
    return travel_min, travel_max


def _verify_landed(svc: Any, target: Dict[str, float], now: float,
                   bench: bool = True) -> Dict[str, float]:
    """Read the pose back and bench any joint that did not get where it was sent."""
    # Wait for the arm to STOP, not for a fixed interval.
    try:
        after = svc.get_positions()
    except Exception as e:
        logger.debug("[gaze] could not verify the correction landed: %s", e)
        return {}
    # Poll until it ARRIVES, or until the cap. Deliberately not "until it stops moving".
    deadline = time.monotonic() + config.GAZE_PITCH_SETTLE_S
    while True:
        if all(
            abs(after.get(j, want) - want) <= config.GAZE_PITCH_LAND_TOL_DEG
            for j, want in target.items()
        ):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
        try:
            after = svc.get_positions()
        except Exception:
            break
    short: Dict[str, float] = {}
    for joint, want in target.items():
        got = after.get(joint)
        if got is None:
            continue
        gap = float(want) - float(got)
        if abs(gap) <= config.GAZE_PITCH_LAND_TOL_DEG:
            _pitch_stalls.pop(joint, None)
            continue
        short[joint] = float(got)
        # `bench` is off for the pan loop: _pitch_stalls feeds the PITCH travel
        # limits, and filing base_yaw or wrist_roll in there would be a category
        # error even though nothing would read it.
        if bench:
            _pitch_stalls[joint] = (
                float(got), 1.0 if gap > 0 else -1.0,
                now + config.GAZE_PITCH_STALL_REST_S,
            )
    if short and bench:
        logger.warning(
            "[gaze] correction did not land: %s — resting %s for %.0fs and "
            "correcting with the other joints",
            ", ".join(
                f"{j.split('.')[0]} asked {target[j]:+.1f} reached {at:+.1f}"
                for j, at in sorted(short.items())
            ),
            " and ".join(sorted(j.split(".")[0] for j in short)),
            config.GAZE_PITCH_STALL_REST_S,
        )
    return short
def _conversation_open() -> bool:
    """Whether a wake-word follow-up window is currently open."""
    try:
        import hal.app_state as state

        voice = getattr(state, "voice_service", None)
        check = getattr(voice, "conversation_focus_active", None)
        return bool(check()) if callable(check) else False
    except Exception as e:
        logger.debug("[gaze] conversation check skipped: %s", e)
        return False


_conversation_was_open: Optional[bool] = None


def _note_conversation_edge() -> None:
    """Log the moment the framing window opens or closes."""
    global _conversation_was_open
    open_now = _conversation_open()
    if open_now == _conversation_was_open:
        return
    first = _conversation_was_open is None
    _conversation_was_open = open_now
    if first:
        return
    logger.info(
        "[gaze] conversation %s — framing %s",
        "open" if open_now else "closed",
        "live" if open_now else "released (idle has the arm)",
    )


def following_a_face(svc: Any) -> bool:
    """Whether the servo writes are a tracking session pursuing the user.

    Tracking writes the arm every frame to keep a person centred, so `last_servo_write`
    is never stale while it runs.
    """
    return bool(svc is not None and getattr(svc, "_tracking_active", False))


def idle_breathing(svc: Any) -> bool:
    """Whether the only thing writing the servos is the idle loop looping."""
    if svc is None or not getattr(svc, "_idle_settled", False):
        return False
    current = getattr(svc, "_current_recording", None)
    return current is not None and current == getattr(svc, "idle_recording", None)


_skips_logged: set = set()

_pitch_quiet_logged: Dict[str, float] = {}
PITCH_QUIET_LOG_EVERY_S: float = 20.0


def _pitch_quiet(reason: str, now: float, detail: str = "") -> None:
    """Say why no correction fired. Throttled per reason."""
    last = _pitch_quiet_logged.get(reason, 0.0)
    if (now - last) < PITCH_QUIET_LOG_EVERY_S:
        return
    _pitch_quiet_logged[reason] = now
    logger.info("[gaze] no pitch correction: %s%s", reason, f" ({detail})" if detail else "")


_sweep_quiet_logged: Dict[str, float] = {}
_sweep_wait_minute: int = -1


def _sweep_quiet(reason: str, now: float, detail: str = "") -> None:
    """Say why the watcher is not looking around. Throttled per reason."""
    last = _sweep_quiet_logged.get(reason, 0.0)
    if (now - last) < PITCH_QUIET_LOG_EVERY_S:
        return
    _sweep_quiet_logged[reason] = now
    logger.info("[gaze] not looking around: %s%s", reason, f" ({detail})" if detail else "")


_repoint_quiet_logged: Dict[str, float] = {}


def _repoint_quiet(reason: str, now: float, detail: str = "") -> None:
    """Say why the lamp did not turn to the remembered bearing."""
    last = _repoint_quiet_logged.get(reason, 0.0)
    if (now - last) < PITCH_QUIET_LOG_EVERY_S:
        return
    _repoint_quiet_logged[reason] = now
    logger.info("[gaze] no repoint: %s%s", reason, f" ({detail})" if detail else "")


_yaw_quiet_logged: Dict[str, float] = {}


def _yaw_quiet(reason: str, now: float, detail: str = "") -> None:
    """Say why no pan fired. Throttled per reason, exactly as _pitch_quiet is."""
    last = _yaw_quiet_logged.get(reason, 0.0)
    if (now - last) < PITCH_QUIET_LOG_EVERY_S:
        return
    _yaw_quiet_logged[reason] = now
    logger.info("[gaze] no pan: %s%s", reason, f" ({detail})" if detail else "")


def _skip(reason: str) -> str:
    """Log the FIRST time sampling is skipped for each distinct reason.

    A watcher that quietly records nothing looks identical to a user who is never there,
    and the difference only shows up as a gate that never opens.
    """
    if reason not in _skips_logged:
        _skips_logged.add(reason)
        logger.info("[gaze] not sampling: %s (logged once)", reason)
    return reason


def _note_face_size(face_h: float, frame_h: float, now: float) -> None:
    """Stamp the near- or far-face clock for one detected face."""
    global _last_near_face_t, _last_far_face_t
    from hal.drivers.tracking import user_check

    if user_check.near_enough(face_h / (frame_h or 1.0)):
        _last_near_face_t = now
    else:
        _last_far_face_t = now


def _sample_once() -> Optional[str]:  # noqa: C901
    """One camera observation folded into the buffer."""
    global _last_face_t

    import hal.app_state as state

    from hal.drivers.tracking import aim, detection, frame_utils

    if getattr(state, "_camera_disabled", False):
        return _skip("camera disabled for privacy")
    cap = getattr(state, "camera_capture", None)
    svc = getattr(state, "animation_service", None)
    if cap is None:
        return _skip("no camera capture on this device")
    if svc is None:
        return _skip("no animation service")
    # Skip only while the head is actually MOVING — a yaw measured mid-swing describes
    # the lamp's motion, not the user's intent. And not every servo write is a move.
    last_write = getattr(svc, "last_servo_write", 0.0)
    if isinstance(last_write, (int, float)) and last_write > 0:
        if (time.monotonic() - float(last_write)) < aim.FRAME_SETTLE_S:
            if not (idle_breathing(svc) or following_a_face(svc)):
                return "head still settling from a move"

    # Non-blocking: a live look must never wait behind a background sample.
    if not aim._detector_lock_use.acquire(blocking=False):
        return "detector busy with a live look"
    try:
        # svc deliberately NOT passed. _grab_frame's settle logic exists for the aim,
        # which must not correct from a pre-move frame, so it waits for a frame captured
        # after `last_servo_write`.
        frame = aim._grab_frame(cap, None)
        if frame is None:
            return _skip("no frame from the camera")
        # Detect on a downscaled copy. Head ORIENTATION survives shrinking — the
        # landmarks move together and the yaw ratio is scale-invariant — so the
        # resolution buys nothing here, unlike a bbox the aim must centre.
        small, _ = frame_utils.downscale(frame)
        frame_or_small = small
        detector = aim.get_detector()
        face = detection.detect_face_with_landmarks(small)
    finally:
        aim._detector_lock_use.release()

    global _last_dy_frac, _last_dy_from_face, _last_frame, _last_box, _last_subject_t
    if face is None:
        # No face — but that is exactly the state where the camera is most likely
        # pointing too low, and correcting it needs SOME vertical reference.
        _last_dy_frac, saw_person = _headroom_from_person(frame_or_small, detector)
        _last_dy_from_face = False
        if saw_person:
            _last_subject_t = time.monotonic()
        if _last_dy_frac is None:
            _pitch_quiet(
                "nothing measurable in frame", time.monotonic(),
                "a person is visible but not clipped by the top edge, so their "
                "head is already in frame" if saw_person
                else "no face and no person at all",
            )
        record_dy(_last_dy_frac, False)
        _last_frame, _last_box = frame_or_small, None
        record_sample(None, 0.0, 0.0)
        return None
    (fx, fy, fw, fh), landmarks = face
    frame_w = float(small.shape[1]) or 1.0
    frame_h = float(small.shape[0]) or 1.0
    _last_dy_frac = ((fy + fh / 2.0) - frame_h / 2.0) / frame_h
    _last_dy_from_face = True
    record_dy(_last_dy_frac, True)
    record_dx(((fx + fw / 2.0) - frame_w / 2.0) / frame_w, True)
    _last_frame, _last_box = small, (fx, fy, fw, fh)
    _note_face_size(float(fh), frame_h, time.monotonic())

    edge = min(1.0, abs((fx + fw / 2.0) - frame_w / 2.0) / (frame_w / 2.0))
    if float(fh) >= config.GAZE_MIN_FACE_PX and edge <= config.GAZE_WELL_FRAMED_EDGE:
        # Big enough to measure AND not about to leave the frame.
        _last_face_t = _last_subject_t = time.monotonic()
    # A face whose eyes sit outside the frame is a face this camera is pointed
    # too low at, not a face turned away. Record it as unmeasured rather than
    # letting the clamp report it as a profile and vote against facing.
    yaw = (
        head_yaw_deg(landmarks)
        if landmarks_in_frame(landmarks, frame_w, frame_h)
        else None
    )
    record_sample(yaw, float(fh), edge)
    return None


def _maybe_yaw(now: float) -> None:
    """Turn toward a face sitting off to one side."""
    global _last_yaw_t
    if not config.GAZE_YAW_ENABLED:
        return
    if now - _last_yaw_t < config.GAZE_PITCH_COOLDOWN_S:
        return

    est = _dx_estimate(now)
    if est is None:
        with _dx_lock:
            have = len(_dx_samples)
        _yaw_quiet(
            "window not filled", now,
            f"{have} samples, need {config.GAZE_YAW_MIN_SAMPLES} "
            f"spanning {config.GAZE_YAW_WINDOW_S * 0.8:.0f}s",
        )
        return
    dx, n_dx, span_dx = est
    if abs(dx) <= config.GAZE_YAW_DEAD_ZONE_FRAC:
        _yaw_quiet(
            "already centred enough", now,
            f"dx={dx * 100:+.0f}% within +/-{config.GAZE_YAW_DEAD_ZONE_FRAC * 100:.0f}%",
        )
        return
    if not _conversation_open():
        _yaw_quiet("no conversation open", now, f"dx={dx * 100:+.0f}%")
        return

    import hal.app_state as state

    from hal.drivers.tracking import servo_follow

    svc = getattr(state, "animation_service", None)
    if svc is None or getattr(svc, "_tracking_active", False):
        _yaw_quiet("body owned by something else", now)
        return
    if getattr(svc, "_music_playing", False):
        _yaw_quiet("music has the body", now)
        return
    try:
        current = svc.get_positions()
    except Exception as e:
        logger.debug("[gaze] pan skipped, no pose: %s", e)
        return

    # dx > 0 means the face sits RIGHT of centre, and increasing either pan
    # joint turns the camera right — device-verified by capture on both joints,
    # so the step carries dx's sign unchanged.
    step = dx * config.GAZE_YAW_DEG_PER_FRAME
    step = max(-config.GAZE_YAW_MAX_STEP_DEG,
               min(config.GAZE_YAW_MAX_STEP_DEG, step))
    target = servo_follow.distribute_yaw(current, step)
    moved = max(
        (abs(target[j] - float(current.get(j, 0.0))) for j in target),
        default=0.0,
    )
    if moved < 0.5:
        _yaw_quiet(
            "already at the limit of travel", now,
            " ".join(
                f"{j.split('.')[0]} {float(current.get(j, 0.0)):+.1f}->{target[j]:+.1f}"
                for j in sorted(target)
            ),
        )
        return

    try:
        from hal.drivers.tracking import aim

        duration = aim.min_move_duration(
            state.safety_policy, target, current, config.GAZE_YAW_MOVE_S,
        )
        with aim.servo_ownership():
            svc.move_and_hold(target, duration=duration)
            short = _verify_landed(svc, target, now, bench=False)
        landed = dict(target)
        landed.update(short)
        discard_dx_samples()
        _last_yaw_t = now
        logger.info(
            "[gaze] face %.0f%% %s of centre (median of %d over %.1fs) — "
            "pan %+.1f deg via %s",
            abs(dx) * 100.0, "right" if dx > 0 else "left", n_dx, span_dx, step,
            ", ".join(
                f"{j.split('.')[0]} {float(current.get(j, 0.0)):+.1f}->{landed[j]:+.1f}"
                + ("" if j not in short else f" (asked {target[j]:+.1f})")
                for j in sorted(target)
                if abs(landed[j] - float(current.get(j, 0.0))) >= 0.05 or j in short
            ) or "nothing",
        )
    except Exception as e:
        logger.debug("[gaze] pan move skipped: %s", e)


def _who_is_in_frame() -> Tuple[str, float]:
    """``(label, age_s)`` of whoever face perception currently believes is here."""
    try:
        import hal.app_state as state

        label, age = state.face_user()
        return (label or ""), float(age or 0.0)
    except Exception as e:
        logger.debug("[gaze] face identity lookup skipped: %s", e)
        return "", 0.0


def _check_speech(stage: str, *, request_repoint: bool) -> bool:
    """Decide whether the current utterance was addressed to the lamp."""
    global _last_grant_t, _speech_repoint_requested_t
    try:
        if not config.GAZE_WAKE_ENABLED:
            return False
        # A noise-only capture may never reach on_speech_end() because it has
        # no transcript. Starting a fresh VAD-confirmed utterance supersedes
        # that abandoned request rather than letting it leak into this one.
        if request_repoint:
            _speech_repoint_requested_t = 0.0
            _speech_repoint_requested.clear()

        ratio, considered = facing_ratio()
        samples = snapshot()
        latest = samples[-1] if samples else None
        yaw_txt = "none" if latest is None or latest[1] == float("inf") else f"{latest[1]:.1f}"
        px_txt = "0" if latest is None else f"{latest[2]:.0f}"
        edge_txt = "0.00" if latest is None else f"{latest[3]:.2f}"
        measured_enough = considered >= config.GAZE_MIN_SAMPLES
        facing_now = ratio >= config.GAZE_MIN_FACING_RATIO

        would = measured_enough and facing_now
        # Observed, never acted on — see _who_is_in_frame and F21.
        who, who_age = _who_is_in_frame()

        now = time.monotonic()
        cooling = would and (now - _last_grant_t) < config.GAZE_COOLDOWN_S
        verdict = (
            "WOULD_WAKE" if would and not cooling
            else "cooldown" if cooling
            else "blind" if not measured_enough
            else "skip"
        )
        # The trail is what makes a refusal diagnosable: a single number cannot
        # distinguish "never looked" from "looked, but the detector blinked".
        trail = ",".join(
            "-" if yaw == float("inf") else f"{yaw:.0f}"
            for _, yaw, _, _ in samples[-8:]
        )
        logger.info(
            "[gaze] %s: yaw=%s face=%spx edge=%s facing=%.0f%%/%.0f%% of %d "
            "who=%s trail=[%s] -> %s%s",
            stage, yaw_txt, px_txt, edge_txt, ratio * 100.0,
            config.GAZE_MIN_FACING_RATIO * 100.0, considered,
            f"{who}({who_age:.0f}s)" if who else "?",
            trail, verdict,
            " (shadow)" if config.GAZE_WAKE_SHADOW else "",
        )
        if not would:
            # Do not turn toward somebody who was measured facing away: that is
            # precisely the signal that the nearby speech may be for somebody else.
            if request_repoint and considered < config.GAZE_MIN_SAMPLES:
                _speech_repoint_requested_t = now
                _speech_repoint_requested.set()
                logger.info(
                    "[gaze] %s: no usable face evidence; requesting remembered-pose reacquire",
                    stage,
                )
            return False
        if cooling or config.GAZE_WAKE_SHADOW:
            return False

        import hal.app_state as state

        voice = getattr(state, "voice_service", None)
        if voice is None or not hasattr(voice, "grant_wakeword_focus"):
            return False
        source = "gaze" if stage == "speech-start" else "gaze-reacquired"
        try:
            granted = bool(
                voice.grant_wakeword_focus(
                    source, timeout_s=config.GAZE_WAKE_FOCUS_S
                )
            )
        except TypeError:
            logger.debug("[gaze] voice service has no timeout_s — full window")
            granted = bool(voice.grant_wakeword_focus(source))
        if granted:
            _last_grant_t = now
        return granted
    except Exception as e:
        logger.debug("[gaze] %s check skipped: %s", stage, e)
        return False


def on_speech_start() -> bool:
    """Check the pre-speech gaze window when VAD confirms speech."""
    return _check_speech("speech-start", request_repoint=True)


def release_reacquire_hold_if_pending() -> bool:
    """Release a reacquire hold at the end of capture, transcript or not."""
    global _speech_repoint_requested_t
    if _speech_repoint_requested_t <= 0.0:
        return False
    _speech_repoint_requested_t = 0.0
    _speech_repoint_requested.clear()
    _release_reacquire_hold()
    return True


def _release_reacquire_hold() -> None:
    """Hand the body back to idle after a speech-triggered reacquire."""
    from hal.drivers.tracking import body

    body.release_to_idle("reacquire hold released")


def on_speech_end() -> bool:
    """Retry one utterance after a voice-triggered pose reacquire."""
    global _speech_repoint_requested_t
    if _speech_repoint_requested_t <= 0.0:
        return False
    _speech_repoint_requested_t = 0.0
    _speech_repoint_requested.clear()
    result = _check_speech("speech-end", request_repoint=False)
    # The utterance is over: whatever the check decided, the hold that pointed
    # the lamp for it must not outlive it.
    _release_reacquire_hold()
    return result


def _consume_speech_repoint(now: float) -> None:
    """Run a pending VAD request from the watcher, never from the mic thread."""
    if not _speech_repoint_requested.is_set():
        return
    _speech_repoint_requested.clear()
    if not _maybe_repoint(now, force=True):
        logger.info(
            "[gaze] speech-start reacquire unavailable — see the [gaze] no repoint line for why"
        )


def _return_to_known_height(now: float) -> None:
    """Go back to a height a face was last seen from, and stop climbing."""
    global _last_pitch_t
    import hal.app_state as state

    from hal.drivers.tracking import face_height

    svc = getattr(state, "animation_service", None)
    if svc is None or getattr(svc, "_tracking_active", False):
        return
    try:
        current = svc.get_positions()
    except Exception:
        return
    target = face_height.height_target(current)
    discard_samples()
    _last_pitch_t = now
    if target is None:
        logger.info("[gaze] climb gave up, and no earlier height is remembered")
        return
    try:
        from hal.drivers.tracking import aim

        duration = aim.min_move_duration(
            state.safety_policy, target, current, config.GAZE_PITCH_MOVE_S,
        )
        with aim.servo_ownership():
            svc.move_and_hold(target, duration=duration)
        logger.info(
            "[gaze] climb gave up; back to the last height a face was seen from (%s)",
            " ".join(f"{j.split('.')[0]}={v:+.1f}" for j, v in sorted(target.items())),
        )
    except Exception as e:
        logger.debug("[gaze] could not restore the known height: %s", e)


def _maybe_pitch(now: float, *, prompt: bool = False) -> None:
    """Raise or lower the camera so a face sits inside the frame, not above it."""
    global _last_pitch_t, _blind_pitch_steps, _climb_gave_up

    if not (config.GAZE_PITCH_ENABLED and config.GAZE_WAKE_ENABLED):
        return
    est = _dy_estimate(now, prompt=prompt)
    if est is None:
        with _dy_lock:
            have = len(_dy_samples)
        _pitch_quiet(
            "window not filled", now,
            f"{have} samples, need {config.GAZE_PITCH_MIN_SAMPLES} "
            f"spanning {config.GAZE_PITCH_WINDOW_S * 0.8:.0f}s",
        )
        return
    dy, from_face, n_dy, span_dy = est
    if abs(dy) <= config.GAZE_PITCH_DEAD_ZONE_FRAC:
        _pitch_quiet(
            "already centred enough", now,
            f"dy={dy * 100:+.0f}% within +/-{config.GAZE_PITCH_DEAD_ZONE_FRAC * 100:.0f}%",
        )
        return
    if (now - _last_pitch_t) < config.GAZE_PITCH_COOLDOWN_S:
        _pitch_quiet("cooling down", now)
        return
    if not prompt and not _conversation_open():
        _pitch_quiet("no conversation open", now, f"dy={dy * 100:+.0f}%")
        return
    # The fallback is a guess, not a measurement — a clipped torso says "the head is up
    # there somewhere" and never how far.
    searching = not from_face
    if searching and _blind_pitch_steps >= config.GAZE_FACE_SEARCH_MAX_STEPS:
        if not _climb_gave_up:
            _climb_gave_up = True
            logger.info(
                "[gaze] climb budget spent: %d/%d steps and still no face",
                _blind_pitch_steps, config.GAZE_FACE_SEARCH_MAX_STEPS,
            )
            _return_to_known_height(now)
        else:
            _pitch_quiet(
                "climb budget spent", now,
                "waiting for a real face before climbing again",
            )
        return

    import hal.app_state as state

    from hal.drivers.tracking import servo_follow

    svc = getattr(state, "animation_service", None)
    if svc is None or getattr(svc, "_tracking_active", False):
        _pitch_quiet("body owned by something else", now)
        return
    if getattr(svc, "_music_playing", False):
        _pitch_quiet("music has the body", now)
        return

    try:
        current = svc.get_positions()
    except Exception as e:
        logger.debug("[gaze] pitch skipped, no pose: %s", e)
        return

    # A face ABOVE centre (dy < 0) needs the camera tilted UP, and up is the DECREASING
    # direction on this joint — the opposite of what this loop assumed.
    if searching:
        step = -abs(config.GAZE_FACE_SEARCH_STEP_DEG)
    else:
        step = dy * config.GAZE_PITCH_DEG_PER_FRAME
        step = max(-config.GAZE_PITCH_MAX_STEP_DEG,
                   min(config.GAZE_PITCH_MAX_STEP_DEG, step))
    # Spread the correction over all three pitch joints instead of spending it all on
    # the wrist.
    travel_min, travel_max = _pitch_travel_limits(now)
    target = servo_follow.distribute_pitch(current, step, travel_min, travel_max)
    moved = max(
        (abs(target[j] - float(current.get(j, 0.0))) for j in target),
        default=0.0,
    )
    if moved < 0.5:
        _pitch_quiet(
            "already at the limit of travel", now,
            " ".join(
                f"{j.split('.')[0]} {float(current.get(j, 0.0)):+.1f}->{target[j]:+.1f}"
                for j in sorted(target)
            ),
        )
        return

    try:
        from hal.drivers.tracking import aim

        duration = aim.min_move_duration(
            state.safety_policy, target, current, config.GAZE_PITCH_MOVE_S,
        )
        # Own the body for the move. Without this the correction competes with whatever
        # else is writing the arm, and something always is.
        with aim.servo_ownership():
            svc.move_and_hold(target, duration=duration)
            # Verify inside the lock: once ownership drops, an emotion can re-pose
            # the head and the read would measure that instead.
            short = _verify_landed(svc, target, now)
        landed = dict(target)
        landed.update(short)
        discard_samples()
        _last_pitch_t = now
        _blind_pitch_steps = 0 if from_face else _blind_pitch_steps + 1
        if from_face:
            _climb_gave_up = False
            from hal.drivers.tracking import face_height

            face_height.record(landed if not short else svc.get_positions())
        logger.info(
            "[gaze] %s %.0f%% %s centre (median of %d over %.1fs) — "
            "tilt %+.1f deg via %s%s",
            "face" if from_face else "head (from torso)",
            abs(dy) * 100.0, "above" if dy < 0 else "below", n_dy, span_dy,
            step,
            # Reports where each joint ENDED UP. The old line printed the
            # target, so six corrections in a row could claim
            # "elbow_pitch +12.3->+25.8" while the elbow never left +12.3.
            ", ".join(
                f"{j.split('.')[0]} {float(current.get(j, 0.0)):+.1f}->{landed[j]:+.1f}"
                + ("" if j not in short else f" (asked {target[j]:+.1f})")
                for j in sorted(target)
                if abs(landed[j] - float(current.get(j, 0.0))) >= 0.05
                or j in short
            ) or "nothing",
            "" if from_face
            else f" [climb {_blind_pitch_steps}/{config.GAZE_FACE_SEARCH_MAX_STEPS}]",
        )
        # Last, deliberately: this is debug output, and it must not be able to skip the
        # bookkeeping above.
        _save_snapshot(
            f"{'face' if from_face else 'torso'} dy={dy * 100:+.0f}% "
            f"(median of {n_dy} over {span_dy:.1f}s) tilt {step:+.1f} deg via "
            + " ".join(
                f"{j.split('.')[0]}{float(current.get(j, 0.0)):+.1f}->{landed[j]:+.1f}"
                for j in sorted(target)
                if abs(landed[j] - float(current.get(j, 0.0))) >= 0.05
            )
        )
    except Exception as e:
        logger.debug("[gaze] pitch move skipped: %s", e)


def _maybe_repoint(now: float, *, force: bool = False) -> bool:
    """Turn toward the remembered bearing when nobody has been visible.

    Guarded by a long absence, a long cooldown, a confidence floor, and by never
    touching the body while anything else owns it.
    """
    global _last_repoint_t

    if not (config.GAZE_REPOINT_ENABLED and config.GAZE_WAKE_ENABLED):
        _repoint_quiet("disabled", now)
        return False
    if not force and (now - _last_face_t) < config.GAZE_REPOINT_AFTER_S:
        return False
    if (now - _last_face_t) < config.GAZE_REPOINT_SKIP_IF_FACE_S:
        _repoint_quiet(
            "a face is already in frame", now,
            f"seen {now - _last_face_t:.1f}s ago",
        )
        return False
    # Voice may bypass the long *absence* delay, but never the movement cooldown.
    if not force and (now - _last_repoint_t) < config.GAZE_REPOINT_COOLDOWN_S:
        _repoint_quiet(
            "cooling down", now,
            f"{now - _last_repoint_t:.0f}s of {config.GAZE_REPOINT_COOLDOWN_S:.0f}s",
        )
        return False
    import hal.app_state as state

    from hal.drivers.tracking import aim, user_bearing

    svc = getattr(state, "animation_service", None)
    if svc is None or getattr(svc, "_tracking_active", False):
        _repoint_quiet(
            "no body, or something else owns it", now,
            "no animation service" if svc is None else "tracking active",
        )
        return False
    if getattr(svc, "_music_playing", False):
        _repoint_quiet("music has the body", now)
        return False

    est = user_bearing.read_estimate()
    if est is None:
        _repoint_quiet("no bearing remembered yet", now)
        return False
    if est.confidence < config.GAZE_REPOINT_MIN_CONFIDENCE:
        _repoint_quiet(
            "bearing not trusted enough", now,
            f"confidence {est.confidence:.2f} < {config.GAZE_REPOINT_MIN_CONFIDENCE:.2f}",
        )
        return False

    try:
        current = svc.get_positions()
        target, step = aim._bearing_step_target(svc, est, current)
        if target is None:
            _last_repoint_t = now  # already there; do not re-check every sample
            logger.info(
                "[gaze] %s: already at remembered bearing %+.1f",
                "speech-start reacquire" if force else "repoint",
                est.bearing_deg,
            )
            return True
        duration = aim.min_move_duration(
            state.safety_policy, target, current, aim.MOVE_DURATION_S
        )
        with aim.servo_ownership():
            svc.move_and_hold(target, duration=duration)
        discard_samples()
        _last_repoint_t = now
        # Owe the estimate a verdict. Deliberately not measured here: a synchronous
        # detect would need the detector lock and a settle, inside a watcher whose whole
        # design is never to make a live look wait.
        global _repoint_pending_t, _repoint_subject_t_before
        _repoint_pending_t = now
        _repoint_subject_t_before = _last_subject_t
        if force:
            logger.info(
                "[gaze] speech-start reacquire: turning %+.1f deg to remembered "
                "bearing %+.1f (conf=%.2f)",
                step, est.bearing_deg, est.confidence,
            )
        else:
            logger.info(
                "[gaze] nobody visible for %.0fs — turning %+.1f deg to the "
                "remembered bearing %+.1f (conf=%.2f)",
                now - _last_face_t, step, est.bearing_deg, est.confidence,
            )
        return True
    except Exception as e:
        _repoint_quiet("it raised", now, str(e))
        return False


def _score_repoint(hit: bool, why: str, now: float) -> None:
    """Hand the bearing one verdict, and look around after a miss."""
    try:
        from hal.drivers.tracking import user_bearing

        dropped = user_bearing.record_prediction(hit)
    except Exception as e:
        logger.debug("[gaze] repoint scoring skipped: %s", e)
        return
    logger.info(
        "[gaze] repoint %s — %s%s", why,
        "bearing confirmed" if hit else "counted against the bearing",
        " (estimate dropped)" if dropped else "",
    )
    if not hit:
        _maybe_sweep(now, confirmed_miss=True)


def _verify_repoint(now: float) -> None:
    """Tell the bearing whether turning to it actually found the user.

    Judged on a FACE near enough to be at the desk, never on a body (#545): a
    co-worker's back must not confirm the bearing. A body only starts the climb, and
    the verdict waits for what the climb turns up.
    """
    global _repoint_pending_t, _repoint_climb_t

    if _repoint_climb_t > 0.0:
        _finish_repoint_climb(now)
        return
    if _repoint_pending_t <= 0.0:
        return
    if (now - _repoint_pending_t) < config.GAZE_REPOINT_VERIFY_S:
        return
    _repoint_pending_t = 0.0
    since = _repoint_subject_t_before
    if _last_near_face_t > since:
        _score_repoint(True, "found a face near enough to be them", now)
        return
    if _last_subject_t > since:
        logger.info("[gaze] repoint found a body, no near face yet — climbing before "
                    "judging the bearing")
        _repoint_climb_t = now
        _maybe_pitch(now, prompt=True)
        return
    _score_repoint(
        False,
        "found only a far face" if _last_far_face_t > since else "found nobody",
        now,
    )


def _finish_repoint_climb(now: float) -> None:
    """Judge a body-only repoint on the face its climb turned up."""
    global _repoint_climb_t

    since = _repoint_subject_t_before
    if _last_near_face_t > since:
        _repoint_climb_t = 0.0
        _score_repoint(True, "climb found a face near enough to be them", now)
        return
    if (now - _repoint_climb_t) < config.GAZE_REPOINT_CLIMB_TIMEOUT_S:
        # Prompted: the ordinary climb only runs inside a conversation.
        _maybe_pitch(now, prompt=True)
        return
    _repoint_climb_t = 0.0
    if _last_far_face_t > since:
        _score_repoint(False, "climb found only a far face", now)
    else:
        # No face at all proves nothing either way. A torso-only miss once deleted
        # correct bearings while the user sat in front of the lamp.
        logger.info("[gaze] repoint climb found no face in %.0fs — not scored",
                    config.GAZE_REPOINT_CLIMB_TIMEOUT_S)


def _maybe_sweep(now: float, *, confirmed_miss: bool = False) -> None:
    """Look around when nobody can be found."""
    global _last_sweep_t, _sweep_wait_minute
    if not config.GAZE_SWEEP_ENABLED:
        return
    if not confirmed_miss and (now - _last_face_t) < config.GAZE_SWEEP_AFTER_S:
        _sweep_quiet(
            "somebody was here recently", now,
            f"{now - _last_face_t:.0f}s of {config.GAZE_SWEEP_AFTER_S:.0f}s",
        )
        return

    try:
        from hal.drivers.tracking import user_bearing

        lost = user_bearing.read_estimate() is None
    except Exception:
        lost = False
    cooldown = (config.GAZE_SWEEP_COOLDOWN_LOST_S if lost
                else config.GAZE_SWEEP_COOLDOWN_S)
    if _last_sweep_t > 0.0 and (now - _last_sweep_t) < cooldown:
        elapsed_min = int((now - _last_sweep_t) / 60.0)
        if elapsed_min != _sweep_wait_minute:
            _sweep_wait_minute = elapsed_min
            logger.info(
                "[gaze] not looking around — last sweep was %d min ago (waiting %.0f%s)",
                elapsed_min, cooldown / 60.0,
                ", no bearing" if lost else "",
            )
        return
    _sweep_wait_minute = -1
    _last_sweep_t = now
    try:
        from hal.drivers.tracking.search import search_for_subject

        res = search_for_subject(for_user=True)
    except Exception as e:
        logger.warning("[gaze] look-around unavailable: %s", e)
        return
    logger.info("[gaze] looked around: %s after %d look(s)",
                res.reason, res.looks_visited)
    if res.found:
        discard_samples()
        discard_dx_samples()
        try:
            from hal.drivers.tracking import bearing_sampler

            if bearing_sampler.sample_now():
                logger.info("[gaze] learned a bearing from the look-around")
        except Exception as e:
            logger.debug("[gaze] could not sample after the look-around: %s", e)


def _loop() -> None:
    interval = 1.0 / max(0.5, config.GAZE_SAMPLE_FPS)
    counted = 0
    # Blocked turns BY REASON. One total says evidence is being lost; it does not say
    # which gate is losing it, and the two are fixed in different places.
    blocked: Dict[str, int] = {}
    counted_from = 0.0
    if _stop.wait(10.0):
        return

    import hal.app_state as state

    from hal.drivers.tracking import aim

    cap = getattr(state, "camera_capture", None)
    if cap is None:
        logger.info("[gaze] no camera on this device — watcher stopping")
        return

    with aim._camera_consumer(cap):
        while not _stop.is_set():
            try:
                skipped = _sample_once()
                now = time.monotonic()
                if skipped is None:
                    counted += 1
                else:
                    blocked[skipped] = blocked.get(skipped, 0) + 1
                if counted_from <= 0.0:
                    counted_from = now
                elif (now - counted_from) >= 60.0:
                    elapsed = now - counted_from
                    why = ", ".join(
                        f"{n / elapsed:.1f}/s {reason}"
                        for reason, n in sorted(
                            blocked.items(), key=lambda kv: -kv[1]
                        )
                    ) or "nothing blocked"
                    logger.info(
                        "[gaze] sampling at %.1f/s (asked %.1f/s); blocked: %s",
                        counted / elapsed, config.GAZE_SAMPLE_FPS, why,
                    )
                    counted, blocked, counted_from = 0, {}, now
                _note_conversation_edge()
                _maybe_pitch(now)
                # Pan after tilt, never in the same breath: both own the body
                # and both clear their own window, so running them together
                # would have each measuring a pose the other has just left.
                _maybe_yaw(now)
                # Neither the sweep nor the repoint is driven from here any more. Both
                # move the body a long way, and doing that because the room merely looks
                # empty is the lamp searching for someone who never asked for it.
                _consume_speech_repoint(now)
                _verify_repoint(now)
            except Exception as e:  # a background watcher must never take HAL down
                logger.debug("[gaze] sample skipped: %s", e)
            if _stop.wait(interval):
                return


def start() -> None:
    """Begin watching. Idempotent, and a no-op unless the feature is armed."""
    global _thread
    if not config.GAZE_WAKE_ENABLED:
        return
    if not config.WAKEWORD_ENABLED:
        logger.info("[gaze] not starting: wake word disabled, nothing to gate")
        return
    if _thread is not None and _thread.is_alive():
        return
    global _last_face_t, _last_subject_t, _last_repoint_t
    _last_face_t = _last_subject_t = _last_repoint_t = time.monotonic()
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="gaze-watcher")
    _thread.start()
    logger.info(
        "[gaze] watching at %.1f fps: yaw<=%.0fdeg and face>=%dpx for %.0f%% "
        "of the last %.1fs%s",
        config.GAZE_SAMPLE_FPS, config.GAZE_MAX_YAW_DEG,
        config.GAZE_MIN_FACE_PX, config.GAZE_MIN_FACING_RATIO * 100.0,
        config.GAZE_WINDOW_S,
        " (SHADOW — logging only, gate not opened)" if config.GAZE_WAKE_SHADOW else "",
    )


def stop() -> None:
    _stop.set()


def reset_for_test() -> None:
    """Clear buffered samples and the cooldown between tests."""
    globals()["_conversation_was_open"] = None
    global _last_grant_t, _last_face_t, _last_repoint_t
    global _speech_repoint_requested_t
    with _samples_lock:
        _samples.clear()
    with _dy_lock:
        _dy_samples.clear()
    _last_grant_t = 0.0
    _last_face_t = 0.0
    _last_repoint_t = 0.0
    globals()["_repoint_pending_t"] = 0.0
    globals()["_repoint_subject_t_before"] = 0.0
    globals()["_last_near_face_t"] = 0.0
    globals()["_last_far_face_t"] = 0.0
    globals()["_repoint_climb_t"] = 0.0
    _last_dy_frac = None
    globals()["_last_frame"] = None
    globals()["_last_box"] = None
    _last_pitch_t = 0.0
    _blind_pitch_steps = 0
    globals()["_climb_gave_up"] = False
    _last_dy_from_face = False
    _pitch_stalls.clear()
    discard_dx_samples()
    globals()["_last_yaw_t"] = 0.0
    globals()["_last_sweep_t"] = 0.0
    globals()["_sweep_wait_minute"] = -1
    _speech_repoint_requested_t = 0.0
    _speech_repoint_requested.clear()
