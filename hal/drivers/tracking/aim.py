"""One-shot aim for visual questions ("what am I holding?", "look at this")."""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Tuple

import hal.config as config
from hal.safety.policy import min_move_duration
from hal.drivers.tracking import body, look_debug

logger = logging.getLogger(__name__)
_filler_interaction = ContextVar("look_filler_interaction", default="")


@contextlib.contextmanager
def filler_ownership(interaction_id: str):
    """Pin metric ownership to the look request, including delayed fillers."""
    token = _filler_interaction.set(interaction_id)
    try:
        yield
    finally:
        _filler_interaction.reset(token)

CENTRE_DEADBAND_FRAC: float = 0.06
AIM_GAIN: float = 0.85
# Optimistic move duration. The safety policy stretches it when the move would
# exceed SAFETY.md max_speed, so this is a floor, not a promise.
MOVE_DURATION_S: float = 0.25
MAX_ITERATIONS: int = 6
# Recording a bearing needs a TIGHTER centre than framing does. The aim stops at
# CENTRE_DEADBAND_FRAC because that frames the subject well enough, but at that offset
# the servo position is not the bearing.
RECORD_DEADBAND_FRAC: float = 0.02
RECENT_SIGHTING_S: float = 4.0
RECENT_SIGHTING_YAW_TOL_DEG: float = 25.0
# Priority 3 — the remembered-bearing fallback. `0dc1b667` removed the hops: the lens
# sees ~110 deg, so anyone between here and there is already in frame before the head
# moves at all, and each hop cost a detect plus a settle.
MAX_BEARING_STEPS: int = 3
MIN_BEARING_CONFIDENCE: float = 0.2

_last_seen_mono: float = 0.0
_last_seen_yaw: float = 0.0

_detector_lock = threading.Lock()
# Held around inference. The detector is a single shared model and concurrent
# detect() calls on it are not safe; the background bearing sampler takes this
# non-blocking and simply skips a cycle rather than making a look wait.
_detector_lock_use = threading.Lock()
_shared_detector: Any = None


def get_detector() -> Any:
    """Process-wide detector, built once. None if it cannot be constructed."""
    global _shared_detector
    with _detector_lock:
        if _shared_detector is None:
            try:
                from hal.drivers.tracking.detection import ObjectDetector

                t0 = time.monotonic()
                _shared_detector = ObjectDetector()
                logger.info(
                    "[look-aim] detector built in %.0fms (reused from now on)",
                    (time.monotonic() - t0) * 1000,
                )
            except Exception as e:
                logger.warning("[look-aim] detector unavailable: %s", e)
                return None
        return _shared_detector

_abort_evt = threading.Event()


def request_abort() -> None:
    """Ask an in-flight aim to stop as soon as it can."""
    _abort_evt.set()


@dataclass
class AimResult:
    """Outcome of one aim attempt. Never raises — the capture must proceed."""

    aimed: bool
    reason: str
    iterations: int = 0
    yaw_moved_deg: float = 0.0
    final_dx_frac: Optional[float] = None
    bearing_steps: int = 0
    # Absolute servo yaw before and after — the only way to tell from a trace
    # whether the head actually MOVED, as opposed to deciding it should have.
    start_yaw: Optional[float] = None
    end_yaw: Optional[float] = None
    bearing_consulted: Optional[dict] = None
    steps: list = field(default_factory=list)
    # Size of the LAST correction issued. The caller scales the capture settle
    # to it: an aim that exits right after a 30 deg swing leaves the arm still
    # ringing, and a flat settle photographs the ring as motion blur.
    last_move_deg: float = 0.0


# How long after a servo write a frame is trusted to show the new pose.
FRAME_SETTLE_S: float = 0.35
# Cap on waiting for that fresh frame — better a slightly stale measurement
# than a stalled aim.
FRAME_WAIT_S: float = 0.6


def _grab_frame(cap: Any, svc: Any = None, require_fresh: bool = False) -> Optional[Any]:
    """A frame captured AFTER the last servo write, not just the newest one held."""
    try:
        quiet_from = 0.0
        last_write = getattr(svc, "last_servo_write", 0.0) if svc is not None else 0.0
        if isinstance(last_write, (int, float)) and last_write > 0:
            quiet_from = float(last_write) + FRAME_SETTLE_S
        deadline = time.monotonic() + FRAME_WAIT_S
        while time.monotonic() < deadline:
            ts = getattr(cap, "last_frame_ts", 0.0)
            if not isinstance(ts, (int, float)):
                ts = 0.0
            if ts >= quiet_from:
                frame = cap.last_frame
                if frame is not None:
                    return frame
            time.sleep(0.03)
        if require_fresh:
            return None
        return cap.last_frame
    except Exception as e:
        logger.debug("[look-aim] frame grab failed: %s", e)
        return None


def prewarm() -> None:
    """Build the detector AND run one throwaway inference, off the critical path."""
    try:
        import numpy as np

        t0 = time.monotonic()
        det = get_detector()
        blank = np.zeros((480, 640, 3), dtype=np.uint8)
        det.detect(blank, "person", strict=False)
        logger.info("[look-aim] detector prewarmed in %.0fms", (time.monotonic() - t0) * 1000.0)
    except Exception as e:
        # Never fatal: a cold detector only costs latency on the first look.
        logger.debug("[look-aim] prewarm skipped: %s", e)


@contextlib.contextmanager
def _camera_consumer(cap: Any):
    """Hold the camera at full FPS for the whole aim."""
    held = False
    try:
        cap.acquire_consumer()
        held = True
    except Exception as e:
        logger.debug("[look-aim] consumer acquire failed: %s", e)
    try:
        yield
    finally:
        if held:
            try:
                cap.release_consumer()
            except Exception:
                pass


def _is_near_enough(box: Tuple[int, int, int, int], frame: Any, target: str) -> bool:
    """Is this box a person close enough to be the one talking to us?"""
    try:
        _x, _y, _w, h = box
        frame_h = float(frame.shape[0])
        if frame_h <= 0:
            return True
        floor = (
            config.LOOK_AIM_MIN_FACE_HEIGHT_FRAC if target == "face"
            else config.LOOK_AIM_MIN_PERSON_HEIGHT_FRAC
        )
        return (float(h) / frame_h) >= floor
    except Exception:
        return True  # never let the filter itself lose a subject


def _nearest_person(detector: Any, frame: Any):
    """The closest person big enough to be the asker, or None.

    Caller holds `_detector_lock_use`.
    """
    getter = getattr(detector, "detect_candidates", None)
    if not callable(getter):
        return None
    try:
        candidates = getter(
            frame, "person", strict=False,
            min_conf=config.LOOK_AIM_MIN_CONFIDENCE,
        )
        if not isinstance(candidates, (list, tuple)) or not candidates:
            return None
        near = [(b, c) for b, c in candidates if _is_near_enough(b, frame, "person")]
    except Exception as e:
        logger.debug("[look-aim] person candidates failed: %s", e)
        return None
    if not near:
        logger.debug(
            "[look-aim] %d person box(es), none near enough to be the asker",
            len(candidates),
        )
        return None
    box, conf = max(near, key=lambda bc: (bc[0][3], bc[1]))
    if len(near) > 1:
        logger.info(
            "[look-aim] %d people in frame — taking the nearest (h=%dpx conf=%.2f) "
            "over %s",
            len(near), box[3], conf,
            ", ".join(f"h={b[3]}px conf={c:.2f}" for b, c in near if b is not box),
        )
    return box, "person", conf


def _sweep_for_subject() -> bool:
    """Look around for the subject. True if the sweep stopped on one."""
    try:
        from hal.drivers.tracking.search import search_for_subject

        res = search_for_subject()
        logger.info("[look-aim] looked around: %s after %d look(s)",
                    res.reason, res.looks_visited)
        return bool(res.found)
    except Exception as e:
        # A sweep that cannot run must not sink the aim — the caller still needs
        # an answer, even if it is "I could not find you".
        logger.warning("[look-aim] look-around unavailable: %s", e)
        return False


def _detect_subject(detector: Any, frame: Any):
    """Nearest plausible person box preferred, face as fallback.

    Returns (box, target, confidence); confidence is None for detectors that do not
    report one.
    """
    with _detector_lock_use:
        nearest = _nearest_person(detector, frame)
        if nearest is not None:
            return nearest
        for target in ("person", "face"):
            try:
                box = detector.detect(
                    frame, target, strict=False,
                    min_conf=config.LOOK_AIM_MIN_CONFIDENCE,
                )
            except TypeError:
                box = detector.detect(frame, target, strict=False)
            except Exception as e:
                logger.debug("[look-aim] detect(%s) failed: %s", target, e)
                continue
            if box is None:
                continue
            if not _is_near_enough(box, frame, target):
                logger.debug(
                    "[look-aim] ignoring far %s: box height %dpx of %dpx",
                    target, box[3], frame.shape[0],
                )
                continue
            return box, target, getattr(detector, "last_confidence", None)
    return None, "", None


@contextlib.contextmanager
def servo_ownership():
    """Own the body for one aim + capture, so nothing else can move the head.

    Ownership is REFCOUNTED — acquire_body/release_body, which are already mutex-guarded
    — and never saved and restored.
    """
    svc = None
    try:
        import hal.app_state as state

        svc = getattr(state, "animation_service", None)
        if svc is not None:
            svc.acquire_body()
    except Exception as e:  # never block a capture over the lock
        logger.debug("[look-aim] servo ownership unavailable: %s", e)
        svc = None
    try:
        yield
    finally:
        if svc is not None:
            try:
                svc.release_body()
            except Exception as e:
                logger.warning("[look-aim] failed to release servo ownership: %s", e)


def _say(pool: str) -> None:
    """Speak one phrase from a named filler pool, best-effort.

    Fire-and-forget: the aim must never wait on speech, and a muted speaker is handled
    downstream by the speak path.
    """
    try:
        import requests

        payload = {"pool": pool}
        owner = _filler_interaction.get()
        if owner:
            payload["owner"] = owner
        requests.post(config.OS_SENSING_FILLER_URL, json=payload, timeout=1.0)
    except Exception as e:
        logger.debug("[look-aim] filler '%s' skipped: %s", pool, e)


def _yaw_of(svc: Any) -> Optional[float]:
    try:
        return round(float(svc.get_positions().get("base_yaw.pos", 0.0)), 2)
    except Exception:
        return None


def _note_sighting(svc: Any) -> None:
    """Remember that a subject was confirmed at the current pose."""
    global _last_seen_mono, _last_seen_yaw
    try:
        _last_seen_yaw = float(svc.get_positions().get("base_yaw.pos", 0.0))
    except Exception:
        return
    _last_seen_mono = time.monotonic()


def _recently_seen_here(svc: Any) -> bool:
    """True when a subject was confirmed at roughly this pose moments ago."""
    if _last_seen_mono <= 0.0:
        return False
    if time.monotonic() - _last_seen_mono > RECENT_SIGHTING_S:
        return False
    try:
        yaw = float(svc.get_positions().get("base_yaw.pos", 0.0))
    except Exception:
        return False
    return abs(yaw - _last_seen_yaw) <= RECENT_SIGHTING_YAW_TOL_DEG


def _step_toward_bearing(svc: Any, out: Optional[dict] = None) -> bool:
    """Take ONE bounded step toward the remembered bearing. True if it moved."""
    try:
        from hal.drivers.tracking import user_bearing
        import hal.app_state as state

        est = user_bearing.read_estimate()
        if out is not None:
            # Built defensively and separately: describing the estimate must
            # never be able to stop the lamp from USING it.
            try:
                out["bearing"] = None if est is None else {
                    "bearing_deg": getattr(est, "bearing_deg", None),
                    "confidence": getattr(est, "confidence", None),
                    "samples": getattr(est, "samples", None),
                    "age_s": getattr(est, "age_s", None),
                }
            except Exception:
                out["bearing"] = None
        if est is None:
            if out is not None:
                out["skipped"] = "no bearing recorded yet"
            return False
        if est.confidence < MIN_BEARING_CONFIDENCE:
            if out is not None:
                out["skipped"] = f"confidence {est.confidence:.2f} < {MIN_BEARING_CONFIDENCE}"
            return False
        current = svc.get_positions()
        target, step = _bearing_step_target(svc, est, current)
        if target is None:
            return False
        # Absolute move, not a relative nudge. The pitch SIGN was never validated for
        # the nudge path (see this module's docstring), which is why the aim has only
        # ever driven yaw.
        duration = min_move_duration(
            state.safety_policy, target, current, MOVE_DURATION_S
        )
        svc.move_and_hold(target, duration=duration)
        logger.info(
            "[look-aim] no subject — stepping %+.1f deg toward remembered bearing "
            "%+.1f (conf=%.2f, restoring %d joints)",
            step, est.bearing_deg, est.confidence, len(target),
        )
        return True
    except Exception as e:
        logger.debug("[look-aim] bearing step skipped: %s", e)
        return False


# Self-calibration bounds for the measured degrees-per-dx_frac scale.
MIN_SCALE_DEG: float = 40.0
# 400 was too permissive: the device measured 302 at the frame edge, which asked
# for a 70 deg correction, got clamped to 45, and overshot the subject.
MAX_SCALE_DEG: float = 250.0
SCALE_SAFETY: float = 0.7
CALIB_MIN_MOVE_DEG: float = 3.0
CALIB_MIN_SHIFT_FRAC: float = 0.02
CALIB_ALPHA: float = 0.6
MAX_STEP_DEG: float = 45.0


CENTRE_DEADLINE_S: float = 4.0
CENTRE_MAX_MISSES: int = 3
CENTRE_PITCH_DEADBAND_FRAC: float = 0.10


@dataclass
class CentreResult:
    centred: bool
    reason: str
    iterations: int = 0
    yaw_total: float = 0.0
    dx_frac: Optional[float] = None
    box: Optional[tuple] = None
    frame: Any = None
    dy_frac: Optional[float] = None


def centre_on_box(svc: Any, cap: Any, probe: Callable[[Any], Optional[tuple]],
                  deadline_s: float = CENTRE_DEADLINE_S) -> CentreResult:
    """Turn and tilt the camera until `probe`'s box sits in the middle of the
    frame — both axes.

    A search hit must not score the remembered bearing.
    """
    import hal.app_state as state
    from hal.drivers.tracking import constants as C
    from hal.drivers.tracking import servo_follow

    _abort_evt.clear()

    t_end = time.monotonic() + deadline_s
    iterations = 0
    yaw_total = 0.0
    last_dx_frac: Optional[float] = None
    last_dy_frac: Optional[float] = None
    scale_deg: Optional[float] = None
    pending_calib: Optional[Tuple[float, float]] = None
    last_box: Optional[tuple] = None
    last_frame: Any = None
    misses = 0
    require_fresh = False

    def _result(centred: bool, reason: str) -> CentreResult:
        return CentreResult(centred, reason, iterations, yaw_total,
                            last_dx_frac, last_box, last_frame, last_dy_frac)

    def _yaw_ok() -> bool:
        return abs(last_dx_frac if last_dx_frac is not None else 1.0) <= CENTRE_DEADBAND_FRAC

    def _pitch_ok() -> bool:
        return abs(last_dy_frac if last_dy_frac is not None else 1.0) <= CENTRE_PITCH_DEADBAND_FRAC

    def _measure(frame: Any, box: tuple) -> None:
        nonlocal last_box, last_frame, last_dx_frac, last_dy_frac
        last_box, last_frame = box, frame
        x, y, w, h = box
        fh, fw = frame.shape[0], frame.shape[1]
        last_dx_frac = ((x + w / 2.0) - (fw / 2.0)) / fw
        last_dy_frac = ((y + h / 2.0) - (fh / 2.0)) / fh

    def _final_look(reason: str) -> CentreResult:
        """One more fresh frame after the LAST move, so the result describes where the lamp
        is pointing now rather than where it was pointing before it moved there.
        """
        if iterations > 0:
            frame = _grab_frame(cap, svc, require_fresh=True)
            if frame is not None:
                box = probe(frame)
                if box is not None:
                    _measure(frame, box)
                    logger.info("[centre] final look after %s: dx=%.1f%% dy=%.1f%%",
                                reason, last_dx_frac * 100.0, last_dy_frac * 100.0)
                    if _yaw_ok() and _pitch_ok():
                        return _result(True, "centred")
        return _result(_yaw_ok() and _pitch_ok(), reason)

    with _camera_consumer(cap):
        while iterations < MAX_ITERATIONS:
            if _abort_evt.is_set():
                return _result(False, "aborted")
            if time.monotonic() >= t_end:
                return _final_look("deadline")

            frame = _grab_frame(cap, svc, require_fresh=require_fresh or iterations > 0)
            require_fresh = False
            if frame is None:
                return _result(_yaw_ok() and _pitch_ok(), "no fresh frame")

            box = probe(frame)
            if box is None:
                misses += 1
                if misses >= CENTRE_MAX_MISSES:
                    return _result(False, "lost the subject")
                require_fresh = True
                continue
            misses = 0

            _measure(frame, box)

            # Learn the local degrees-per-dx_frac from what the previous step actually
            # achieved.
            if pending_calib is not None:
                prev_yaw, prev_dx = pending_calib
                measured = _measure_scale(_yaw_of(svc) - prev_yaw,
                                          prev_dx - last_dx_frac)
                if measured is not None:
                    scale_deg = (
                        measured if scale_deg is None
                        else (1.0 - CALIB_ALPHA) * scale_deg + CALIB_ALPHA * measured
                    )
                pending_calib = None

            if _yaw_ok() and _pitch_ok():
                return _result(True, "centred")

            try:
                current = svc.get_positions()
            except Exception as e:
                return _result(False, f"no pose: {e}")
            target = dict(current)

            # Yaw sign per the tracker's verified convention: dx>0 (subject right of
            # centre) -> base_yaw INCREASES. Do not flip this without device evidence.
            yaw_deg = 0.0
            if not _yaw_ok():
                scale = (scale_deg * SCALE_SAFETY if scale_deg is not None
                         else config.LOOK_AIM_FOV_DEG)
                yaw_deg = AIM_GAIN * last_dx_frac * scale
                yaw_deg = max(-MAX_STEP_DEG, min(MAX_STEP_DEG, yaw_deg))
                base_yaw = float(current.get("base_yaw.pos", 0.0)) + yaw_deg
                target["base_yaw.pos"] = max(C.YAW_MIN, min(C.YAW_MAX, base_yaw))
                pending_calib = (_yaw_of(svc), last_dx_frac)

            pitch_deg = 0.0
            if not _pitch_ok():
                pitch_deg = last_dy_frac * config.GAZE_PITCH_DEG_PER_FRAME
                pitch_deg = max(-config.GAZE_PITCH_MAX_STEP_DEG,
                                min(config.GAZE_PITCH_MAX_STEP_DEG, pitch_deg))
                target.update(servo_follow.distribute_pitch(current, pitch_deg))

            try:
                duration = min_move_duration(state.safety_policy, target, current,
                                             MOVE_DURATION_S)
                svc.move_and_hold(target, duration=duration)
            except Exception as e:
                logger.warning("[centre] move failed: %s", e)
                return _result(False, f"move failed: {e}")

            yaw_total += yaw_deg
            iterations += 1
            logger.info(
                "[centre] iter=%d dx=%.1f%% dy=%.1f%% -> yaw %+.1f pitch %+.1f deg (scale=%.0f%s)",
                iterations, last_dx_frac * 100.0, last_dy_frac * 100.0, yaw_deg, pitch_deg,
                scale_deg * SCALE_SAFETY if scale_deg is not None else config.LOOK_AIM_FOV_DEG,
                "" if scale_deg is not None else " guess",
            )

    return _final_look("max iterations")


def _measure_scale(moved_deg: float, shift_frac: float) -> Optional[float]:
    """Degrees of yaw per unit dx_frac, measured from what the last step did."""
    if abs(moved_deg) < CALIB_MIN_MOVE_DEG or abs(shift_frac) < CALIB_MIN_SHIFT_FRAC:
        return None
    if (moved_deg > 0) != (shift_frac > 0):
        return None
    scale = abs(moved_deg) / abs(shift_frac)
    if not (MIN_SCALE_DEG <= scale <= MAX_SCALE_DEG):
        return None
    return scale


# A joint is "already there" within this much, so a search does not re-issue a
# move for rounding noise on every step.
POSE_TOLERANCE_DEG: float = 2.0


def _bearing_step_target(svc: Any, est: Any, current: dict):
    """Absolute pose for one bearing step: yaw stepped, the rest restored."""
    cur_yaw = float(current.get("base_yaw.pos", 0.0))
    delta = float(est.bearing_deg) - cur_yaw
    step = delta

    try:
        valid = set(svc.get_joint_names())
    except Exception:
        valid = set(current.keys())

    pose = getattr(est, "pose", None)
    if not isinstance(pose, dict):
        pose = {}

    target: dict = {}
    for joint, value in pose.items():
        if joint == "base_yaw.pos" or joint not in valid:
            continue
        if abs(float(value) - float(current.get(joint, value))) > POSE_TOLERANCE_DEG:
            target[joint] = float(value)

    if abs(delta) >= 1.0:
        target["base_yaw.pos"] = cur_yaw + step
    elif not target:
        return None, 0.0
    return target, step


def _score_prediction(bearing_steps: int, found: bool) -> None:
    """Tell the estimate whether turning to it actually found anyone.

    Only meaningful when we actually turned to the bearing — an aim that never consulted
    it says nothing about whether it is still right.
    """
    if bearing_steps <= 0:
        return
    try:
        from hal.drivers.tracking import user_bearing

        user_bearing.record_prediction(found)
    except Exception as e:
        logger.debug("[look-aim] prediction scoring skipped: %s", e)


def _record_bearing_if_centred(svc: Any, dx_frac: float) -> None:
    """Fold this sighting into the remembered bearing, if it is centred enough.

    Failures are swallowed — losing a sample must never cost the user their answer.
    """
    if abs(dx_frac) > RECORD_DEADBAND_FRAC:
        return
    try:
        from hal.drivers.tracking import user_bearing

        positions = svc.get_positions()
        yaw = float(positions.get("base_yaw.pos", 0.0))
        user_bearing.record_sighting(yaw, pose=positions)
    except Exception as e:
        logger.debug("[look-aim] bearing record skipped: %s", e)


def aim_for_look(deadline_s: float, detector: Any = None) -> AimResult:
    """Centre the subject in yaw, then return so the caller can capture.

    Always returns — a failed aim must still let `look` capture something, because dead
    air is worse than an imperfectly framed frame.
    """
    import hal.app_state as state

    _abort_evt.clear()
    t_end = time.monotonic() + max(0.0, deadline_s)

    cap = getattr(state, "camera_capture", None)
    svc = getattr(state, "animation_service", None)
    if cap is None or svc is None:
        return AimResult(False, "no camera or animation service")
    if getattr(state, "_camera_disabled", False):
        # Privacy: never move toward someone who has asked the device not to look.
        return AimResult(False, "camera disabled")

    if detector is None:
        detector = get_detector()
        if detector is None:
            return AimResult(False, "no detector")

    iterations = 0
    yaw_total = 0.0
    bearing_steps = 0
    last_dx_frac: Optional[float] = None
    # Measured degrees-per-dx_frac for THIS aim. Per-aim, not persisted: the
    # scale depends on where in the frame the subject is, so a value learned
    # last time at the edge would be wrong near the centre.
    scale_deg: Optional[float] = None
    pending_calib: Optional[Tuple[float, float]] = None
    last_move_deg = 0.0
    announced_found = False
    announced_search = False
    found_any = False
    start_yaw = _yaw_of(svc)
    steps: list = []
    bearing_consulted: Optional[dict] = None

    def _result(aimed: bool, reason: str) -> AimResult:
        """Build the outcome with the pose actually reached, so a trace shows
        whether the head moved rather than just what was decided.
        """
        _score_prediction(bearing_steps, found=found_any)
        # A moved head is a parked head (`nudge` and `_step_toward_bearing` both end in
        # `move_and_hold`) — hand it back to idle after the capture the caller is about
        # to take.
        if iterations > 0 or bearing_steps > 0:
            body.release_to_idle_later(body.HOLD_AFTER_FIND_S, f"look-aim {reason}")
        return AimResult(
            aimed, reason, iterations, yaw_total, last_dx_frac, bearing_steps,
            start_yaw, _yaw_of(svc), bearing_consulted, steps, last_move_deg,
        )

    with _camera_consumer(cap):
        while iterations < MAX_ITERATIONS:
            if _abort_evt.is_set():
                return _result(False, "aborted")
            if time.monotonic() >= t_end:
                return _result(False, "deadline")

            with look_debug.stage("aim.frame_wait"):
                frame = _grab_frame(cap, svc, require_fresh=iterations > 0)
            if frame is None:
                if iterations > 0:
                    return _result(
                        abs(last_dx_frac or 1.0) <= CENTRE_DEADBAND_FRAC, "no fresh frame"
                    )
                return _result(False, "no frame")

            with look_debug.stage("aim.detect"):
                box, kind, conf = _detect_subject(detector, frame)
            if box is None:
                look_debug.note_step_frame(
                    iterations + 1, frame, None, f"iter {iterations + 1}: no detection"
                )
                # Priority 2 — occlusion, not absence. Never turn away from a scene
                # that just changed dramatically: a large object filling the frame is
                # evidence the subject is right there.
                if _recently_seen_here(svc):
                    steps.append({"n": iterations + 1, "saw": None,
                                  "action": "hold (recent sighting — likely occluded)",
                                  "yaw": _yaw_of(svc)})
                    return _result(False, "holding: seen here moments ago (likely occluded)")
                probe: dict = {}
                if bearing_steps < MAX_BEARING_STEPS and _step_toward_bearing(svc, probe):
                    bearing_consulted = probe.get("bearing")
                    steps.append({"n": iterations + 1, "saw": None,
                                  "action": "step toward remembered bearing",
                                  "bearing": probe.get("bearing"), "yaw": _yaw_of(svc)})
                    if bearing_steps == 0 and config.LOOK_AIM_SPEAK:
                        announced_search = True
                        _say("look_searching")
                    bearing_steps += 1
                    iterations += 1
                    continue
                bearing_consulted = probe.get("bearing", bearing_consulted)
                steps.append({"n": iterations + 1, "saw": None,
                              "action": probe.get("skipped", "give up — nothing found"),
                              "yaw": _yaw_of(svc)})
                if not announced_search and config.LOOK_AIM_SPEAK:
                    announced_search = True
                    _say("look_searching")
                swept_at = time.monotonic()
                found_by_sweep = _sweep_for_subject()
                # The clock stops while sweeping. The deadline exists so a live turn
                # never stalls in SILENCE.
                t_end += time.monotonic() - swept_at
                steps.append({"n": iterations + 1,
                              "action": "looked around",
                              "saw": "subject" if found_by_sweep else None,
                              "yaw": _yaw_of(svc)})
                if found_by_sweep:
                    iterations += 1
                    continue

                # Close the loop the announcement opened. Only when a search was
                # actually announced — a look that never turned owes no explanation, and
                # narrating every failed detection would be the noise `look_capturing`
                # is already gated to avoid.
                if announced_search and config.LOOK_AIM_SPEAK:
                    _say("look_lost")
                return _result(False, "subject not found")

            if bearing_steps > 0 and not announced_found and config.LOOK_AIM_SPEAK:
                announced_found = True
                _say("look_found")
            found_any = True
            _note_sighting(svc)
            x, _y, w, _h = box
            w_fr = float(frame.shape[1])
            dx = (x + w / 2.0) - (w_fr / 2.0)
            last_dx_frac = dx / w_fr
            conf_txt = f" conf={conf:.2f}" if isinstance(conf, (int, float)) else ""
            look_debug.note_step_frame(
                iterations + 1, frame, box,
                f"iter {iterations + 1}: {kind}{conf_txt} dx={last_dx_frac * 100:+.1f}%",
            )

            if pending_calib is not None:
                prev_yaw, prev_dx = pending_calib
                measured = _measure_scale(
                    _yaw_of(svc) - prev_yaw, prev_dx - last_dx_frac
                )
                if measured is not None:
                    scale_deg = (
                        measured if scale_deg is None
                        else (1.0 - CALIB_ALPHA) * scale_deg + CALIB_ALPHA * measured
                    )
                pending_calib = None

            if abs(last_dx_frac) <= CENTRE_DEADBAND_FRAC:
                _record_bearing_if_centred(svc, last_dx_frac)
                return _result(True, f"centred on {kind}")

            # Yaw sign per the tracker's verified convention: dx>0 (subject right of
            # centre) -> base_yaw INCREASES. Do not flip this without device evidence.
            scale = (
                scale_deg * SCALE_SAFETY if scale_deg is not None
                else config.LOOK_AIM_FOV_DEG
            )
            yaw_deg = AIM_GAIN * last_dx_frac * scale
            yaw_deg = max(-MAX_STEP_DEG, min(MAX_STEP_DEG, yaw_deg))
            pending_calib = (_yaw_of(svc), last_dx_frac)

            try:
                with look_debug.stage("aim.move"):
                    current = svc.get_positions()
                    svc.nudge(yaw_deg, 0.0, MOVE_DURATION_S, current, state.safety_policy)
            except Exception as e:
                logger.warning("[look-aim] nudge failed: %s", e)
                return _result(False, f"nudge failed: {e}")

            yaw_total += yaw_deg
            last_move_deg = yaw_deg
            iterations += 1
            steps.append({"n": iterations, "saw": kind, "dx_frac": round(last_dx_frac, 3),
                          "conf": round(conf, 3) if isinstance(conf, (int, float)) else None,
                          "action": f"centre: yaw {yaw_deg:+.1f}", "yaw": _yaw_of(svc),
                          "scale": round(scale, 1)})
            logger.info(
                "[look-aim] iter=%d %s dx=%.0fpx (%.1f%%) -> yaw %+.1f deg (scale=%.0f%s)",
                iterations, kind, dx, last_dx_frac * 100.0, yaw_deg, scale,
                "" if scale_deg is not None else " guess",
            )

    return _result(abs(last_dx_frac or 1.0) <= CENTRE_DEADBAND_FRAC, "max iterations")
