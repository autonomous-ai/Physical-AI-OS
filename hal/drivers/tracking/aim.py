"""One-shot aim for visual questions ("what am I holding?", "look at this").

When the realtime model calls the `look` tool, the head may be pointing
anywhere — `look` takes no parameters and grabs whatever the camera currently
sees, so the model can answer confidently about a wall. This centres the
subject *before* the shutter, bounded by a deadline so a live turn never
stalls waiting for servos.

Scope (v1) — yaw only, deliberately:
  * The yaw sign is COPIED from the tracker's empirically verified convention
    (`tracker_service.py`: "dx>0 (object on right) -> base_yaw must INCREASE to
    chase right (verified empirically vs legacy gimbal path)"). It is not
    re-derived; a sign error here is silent and mirrors every move.
  * Pitch is excluded. `AnimationService.nudge()` drives `base_pitch`, while the
    tracker distributes pitch across base/elbow/wrist — so the pitch sign is
    NOT validated for this path, and an inverted pitch is the exact bug this
    codebase already hit once (see servo_follow.command_pid docstring).

Safety: every move goes through `AnimationService.nudge()`, which applies
`min_move_duration(safety_policy, ...)` — so SAFETY.md's `max_speed` ceiling
stretches the move rather than being bypassed to hit the deadline.
"""

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

# How close to frame centre counts as "aimed", as a fraction of frame width.
# Wide enough that the lamp does not hunt for a perfect centre it cannot hold.
CENTRE_DEADBAND_FRAC: float = 0.06
# Proportional gain on the measured offset. Below 1.0 so a slightly wrong
# deg_per_px (the FOV constant is unverified — see the plan's Task P) converges
# instead of oscillating.
AIM_GAIN: float = 0.85
# Optimistic move duration. The safety policy stretches it when the move would
# exceed SAFETY.md max_speed, so this is a floor, not a promise.
MOVE_DURATION_S: float = 0.25
# Hard cap on correction rounds; the deadline usually bites first.
MAX_ITERATIONS: int = 6
# Recording a bearing needs a TIGHTER centre than framing does. The aim stops at
# CENTRE_DEADBAND_FRAC because that frames the subject well enough, but at that
# offset the servo position is not the bearing — it is the bearing plus up to
# 0.06 x FOV of uncorrected error. Only a near-exact centre lets us store
# `bearing = base_yaw` and stay independent of the disputed FOV constant.
RECORD_DEADBAND_FRAC: float = 0.02
# Priority 2 — occlusion hysteresis. A subject that vanishes seconds after being
# seen at this pose is almost certainly OCCLUDED (a held-up object covering
# them), not absent. Turning away then would abandon the very thing we were
# asked to look at.
RECENT_SIGHTING_S: float = 4.0
RECENT_SIGHTING_YAW_TOL_DEG: float = 25.0
# Priority 3 — the remembered-bearing fallback.
#
# It used to advance in fixed-size hops, re-detecting between them so it
# could not sail past somebody standing en route. `0dc1b667` removed the hops:
# the lens sees ~110 deg, so anyone between here and there is already in frame
# before the head moves at all, and each hop cost a detect plus a settle —
# most of a second against the aim's deadline. The move now goes straight to
# the remembered pose in one absolute command.
#
# So MAX_BEARING_STEPS no longer bounds hops. It bounds ATTEMPTS, and in
# practice never binds: after the first move `_bearing_step_target` finds every
# joint inside POSE_TOLERANCE_DEG and returns None, so a second attempt never
# happens. Kept as a backstop against a move that reports success without
# arriving — a stuck servo would otherwise retry forever.
MAX_BEARING_STEPS: int = 3
# Below this the estimate is too green or too stale to be worth turning for.
MIN_BEARING_CONFIDENCE: float = 0.2

# Last confirmed sighting: monotonic timestamp and the yaw it was seen at.
_last_seen_mono: float = 0.0
_last_seen_yaw: float = 0.0

# ONE detector for the process. Building an ObjectDetector is not cheap: with
# DL encryption on, its constructor fetches the public key over the network. A
# per-call detector made a single aim iteration take ~7s on device, which blew
# the realtime turn's budget — Gemini timed out, the turn fell back to the main
# agent, and the user got "I couldn't see it" for a frame that was captured
# perfectly. TrackerService builds its detector once for the same reason.
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
    """Ask an in-flight aim to stop as soon as it can.

    Called by the physical-button single-click path: a click means "stop moving
    and pay attention to me", and an aim that kept turning through it would be
    the exact failure that gesture exists to prevent.
    """
    _abort_evt.set()


@dataclass
class AimResult:
    """Outcome of one aim attempt. Never raises — the capture must proceed."""

    aimed: bool
    reason: str
    iterations: int = 0
    yaw_moved_deg: float = 0.0
    final_dx_frac: Optional[float] = None
    # How many remembered-bearing steps were taken. Task E reads this to judge
    # whether the estimate is still predicting well.
    bearing_steps: int = 0
    # Absolute servo yaw before and after — the only way to tell from a trace
    # whether the head actually MOVED, as opposed to deciding it should have.
    start_yaw: Optional[float] = None
    end_yaw: Optional[float] = None
    # The remembered bearing as consulted this look: its value and confidence,
    # or None when nothing was stored yet. Answers "did it look where it
    # remembered, or did it have nothing to go on?"
    bearing_consulted: Optional[dict] = None
    # One entry per decision, in order. What was seen, where, what was commanded.
    steps: list = field(default_factory=list)
    # Size of the LAST correction issued. The caller scales the capture settle
    # to it: an aim that exits right after a 30 deg swing leaves the arm still
    # ringing, and a flat settle photographs the ring as motion blur.
    last_move_deg: float = 0.0


# How long after a servo write a frame is trusted to show the new pose.
# Mirrors capture_still's settle: the arm is still ringing before this. 0.25 was
# too short — a trace showed a +15.3 deg command reading back as +9.78 deg
# because the pose was sampled mid-flight, and the next step then corrected from
# an offset the head was still travelling through.
FRAME_SETTLE_S: float = 0.35
# Cap on waiting for that fresh frame — better a slightly stale measurement
# than a stalled aim.
FRAME_WAIT_S: float = 0.6


def _grab_frame(cap: Any, svc: Any = None, require_fresh: bool = False) -> Optional[Any]:
    """A frame captured AFTER the last servo write, not just the newest one held.

    This must wait. Reading `last_frame` immediately after commanding a move
    returns the PRE-move image, so the next correction is computed from a pose
    the head has already left — the loop then re-issues the same correction and
    marches the head across the room while `dx` never changes (observed on
    device: six identical +12.3 deg steps, dx frozen at 0.241, 61 deg travelled).

    Caller must hold the consumer (see `_camera_consumer`) — without one the
    device does not capture at full FPS and a fresh frame may never arrive.
    """
    try:
        # isinstance guards, not truthiness: a test double's attribute is a Mock,
        # which is truthy but not a number, and the arithmetic below would then
        # raise into the except and silently report "no frame".
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
            # Deliberately no best-effort fallback here. Correcting again from a
            # frame the head has already moved past is what marched it across
            # the room; with no new evidence the right move is no move.
            return None
        return cap.last_frame  # first measurement: nothing has moved yet
    except Exception as e:  # camera races are not worth failing the capture over
        logger.debug("[look-aim] frame grab failed: %s", e)
        return None


def prewarm() -> None:
    """Build the detector AND run one throwaway inference, off the critical path.

    Constructing the detector is cheap (~400ms); the FIRST detect() is not — the
    model loads and compiles lazily inside it. On device that cost a 6.0s first
    aim that blew its own 2.5s deadline and captured uncentred, while every
    later iteration ran ~0.4s. Paying it once at startup makes the first real
    look as fast as the rest.
    """
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
    """Hold the camera at full FPS for the whole aim.

    Acquiring and releasing per frame let the device drop back to reduced
    capture between iterations, so `last_frame` went stale exactly when the loop
    needed it fresh.
    """
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
    """Is this box a person close enough to be the one talking to us?

    Apparent size is the only distance cue a single camera has, and the person
    who asked "look at what I'm holding" is by definition within arm's reach of
    the thing they are holding. Device frames (2026-08-19, 1280x720):

      ~22px  face  — someone across the office; the lamp chased this
      ~65px  person — a real colleague, far away, also not the asker
      ~165px person — the actual user, close, clipped by the frame edge

    Height, not area or width: a close subject is routinely clipped left/right
    (the good frame above is half out of shot) but their apparent height still
    scales with distance.
    """
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

    Caller holds `_detector_lock_use`. Returns the same
    ``(box, target, confidence)`` shape as `_detect_subject`, or None when the
    candidate path is unavailable (an older detector, a failure, or nobody
    clearing the floor) so the caller can fall back to `detect`.
    """
    getter = getattr(detector, "detect_candidates", None)
    if not callable(getter):
        return None  # detector predates the candidate path
    try:
        candidates = getter(
            frame, "person", strict=False,
            min_conf=config.LOOK_AIM_MIN_CONFIDENCE,
        )
        # isinstance, not truthiness: a test double's attribute is a Mock, which
        # is callable AND truthy but not iterable, and the comprehension below
        # would raise out of the aim entirely. The same guard the bearing step
        # already applies to a malformed estimate.
        if not isinstance(candidates, (list, tuple)) or not candidates:
            return None
        # Floor FIRST, then rank. Both halves matter — see _detect_subject.
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
    # Tallest wins; confidence only breaks a tie between similar heights.
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
    """Look around for the subject. True if the sweep stopped on one.

    Imported here rather than at module scope: `search` imports from this file,
    so a top-level import either way is a cycle. The aim asking the sweep for
    help is the only direction that edge runs.
    """
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

    Person first because a hand-held object often occludes the face but rarely
    the whole body — and because framing the person includes whatever they are
    holding, which a tightly centred face does not.

    Detections too small to be the asker are rejected rather than returned: the
    caller treats "no subject" as a reason to hold or consult the remembered
    bearing, which is a far better answer than turning to a stranger at the
    other end of the room.

    Among several people, the one CLOSEST is taken as the asker — apparent
    height, the only distance cue a single camera has. Not the detector's own
    pick: `detect` ranks by confidence, which measures how CANONICAL a shape
    is, and that inverts the answer exactly when it matters. Device-proven
    2026-08-24 (look_logs/20260824-112802): a small, fully-visible colleague at
    the back scored 0.71 while the person actually asking — clipped by the
    frame edge, occluded by the toy they were holding up, close enough to be a
    face and one shoulder — did not win at all, and the aim turned 19.8 deg
    away from them.

    The size floor therefore has to run BEFORE the choice. Applied after, as it
    was, it only ever rubber-stamps a decision already made: `detect` returns
    ONE box, so the asker was discarded before `_is_near_enough` saw anything.
    The same lesson is written out in detection._measurable_faces, for faces.

    Faces need none of this: `_detect_face_yunet` already picks the LARGEST,
    and largest is tallest, so a floor applied afterwards can only reject — it
    cannot pick the wrong one.

    Returns (box, target, confidence); confidence is None for detectors that do
    not report one.
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
                # Detector predates the min_conf parameter — the size gate below
                # still applies, so degrade rather than lose the subject.
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

    Without this the sequence "hear the question -> aim -> capture" can be
    broken by an emotion animation landing in the middle: emotion presets play
    RECORDED poses that are absolute on every joint, including wrist_roll, so
    one arriving between the aim and the shutter re-poses the head entirely and
    the frame shows wherever the animation parked it.

    `nudge()` already preempts an animation that is ALREADY playing, but not one
    dispatched afterwards — which is exactly the window the capture sits in.

    `_tracking_active` is the existing lock for this: routes/emotion.py
    suppresses ALL emotion servo while it is set, and the animation loop drops
    any in-progress recording rather than fighting for the joints. The vision
    tracker uses the same lock.

    Ownership is REFCOUNTED — acquire_body/release_body, which are already
    mutex-guarded — and never saved and restored. Six call sites enter this from
    three threads (the gaze watcher, the realtime `look` tool, the sweep), so
    two owners overlap routinely. Save/restore lost that update: the owner that
    exited LAST re-asserted a stale True, wedging the lock with no holder and
    silently suppressing every emotion animation until a face-track session
    happened to clear it (#312). A refcount also fixes the narrower half of the
    same bug — `prev` read the composite property but the setter wrote the flag,
    so an aim overlapping a live ServoFollower turned a counter hold into a
    permanent one.

    A refcount keeps the original guarantee for free: a genuine tracking session
    already running is a separate hold, and this releases only its own.
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

    os-server owns the phrases, the language and the WAV cache; HAL only decides
    WHEN. Fire-and-forget: the aim must never wait on speech, and a muted speaker
    is handled downstream by the speak path.

    ``owner`` tags the phrase with the utterance the aim is serving, so the
    audio the user hears while the lamp turns is attributed to that turn
    instead of arriving unclaimed (see hal/telemetry/voice_metrics.py).
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
    """True when a subject was confirmed at roughly this pose moments ago.

    Then a sudden disappearance means OCCLUSION, not absence — hold and capture
    rather than turning away from whatever is being held up to the camera.
    """
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
    """Take ONE bounded step toward the remembered bearing. True if it moved.

    `out` collects what the estimate said, so a trace can distinguish "there was
    no bearing to use" from "there was one and it was wrong".
    """
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
            return False  # already in the remembered shape; nothing left to try
        # Absolute move, not a relative nudge. The pitch SIGN was never validated
        # for the nudge path (see this module's docstring), which is why the aim
        # has only ever driven yaw. An absolute target has no sign to get wrong,
        # so restoring the remembered posture is safe here where correcting it
        # incrementally would not be.
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


# Self-calibration bounds for the measured degrees-per-dx_frac scale. The
# device has measured 91 deg near the frame centre and 229 deg at the edge, so
# the window is deliberately wide — it exists only to reject nonsense from a
# noisy or mis-detected step, not to encode a value.
MIN_SCALE_DEG: float = 40.0
# 400 was too permissive: the device measured 302 at the frame edge, which asked
# for a 70 deg correction, got clamped to 45, and overshot the subject.
MAX_SCALE_DEG: float = 250.0
# The measured scale is taken at the CURRENT eccentricity but spent on the NEXT,
# smaller one — and on a fisheye the scale shrinks toward the centre, so the
# measurement is systematically too big for the correction it is used for.
# Biasing low costs an extra step at worst; biasing high oscillates, which is
# what dx=+27% -> -10% -> +11% looked like on device.
SCALE_SAFETY: float = 0.7
# Below these, a step tells us nothing: dividing a tiny shift by a tiny move
# amplifies detector jitter into a wild scale.
CALIB_MIN_MOVE_DEG: float = 3.0
CALIB_MIN_SHIFT_FRAC: float = 0.02
# How fast the measured scale follows the newest step. High because the true
# scale genuinely changes as the subject moves in from the edge.
CALIB_ALPHA: float = 0.6
# Hard cap on one correction, whatever the measured scale says.
MAX_STEP_DEG: float = 45.0


# How long a centring correction may run. A search hit is already seconds into
# a sweep, so this is a top-up rather than the aim's own deadline: six
# iterations of move + settle + detect fit comfortably, and a subject that
# needs longer is one the loop is not converging on anyway.
CENTRE_DEADLINE_S: float = 4.0
# Consecutive probe misses tolerated before the correction reports the subject
# lost. A marginal detection at the frame edge flickers frame to frame —
# device-observed on lamp-ac82, the sweep saw a keyboard and the correction's
# very next probe did not — so one miss is noise, not absence. Each retry waits
# for a FRESH frame, and the deadline still bounds the whole loop.
CENTRE_MAX_MISSES: int = 3
# Vertical deadband, as a fraction of frame HEIGHT. Looser than the yaw one:
# the things a sweep is asked for sit on desks and are wide, so "centred
# enough" vertically is a band, not a line, and every pitch step costs a full
# settle of three joints.
CENTRE_PITCH_DEADBAND_FRAC: float = 0.10


@dataclass
class CentreResult:
    centred: bool
    reason: str
    iterations: int = 0
    yaw_total: float = 0.0
    dx_frac: Optional[float] = None
    # The box and the frame AS LAST SEEN, so a caller that wants to SHOW what it
    # centred on does not have to grab and detect all over again — a second
    # capture would be a different frame from a body that has since settled, and
    # drawing this box on that frame puts the rectangle beside the subject.
    box: Optional[tuple] = None
    frame: Any = None
    dy_frac: Optional[float] = None


def centre_on_box(svc: Any, cap: Any, probe: Callable[[Any], Optional[tuple]],
                  deadline_s: float = CENTRE_DEADLINE_S) -> CentreResult:
    """Turn and tilt the camera until `probe`'s box sits in the middle of the
    frame — both axes.

    Yaw is the measure-and-nudge loop `aim_for_look` runs, with everything that
    is specific to answering "where are you" left out: no bearing steps, no
    filler announcements, no occlusion hold, no bearing scoring. A search hit
    must not score the remembered bearing — the sweep finds an OBJECT as often
    as a person, and teaching the user-bearing estimator that a keyboard is
    where the user sits is exactly the kind of quiet corruption #226 was about.

    Pitch is copied from `gaze._maybe_pitch`, the one loop on this arm whose
    pitch sign was measured rather than assumed. A box ABOVE centre (dy < 0)
    needs the camera tilted UP, and up is the DECREASING direction on the pitch
    joints — device-measured on lamp-0c89, paired A/B/A, 18 samples per
    position: wrist_pitch -75 -> dy +0.009, -90 -> dy +0.113. With the sign
    the other way every correction enlarges the error it measures. The step is
    spread across base/elbow/wrist by `servo_follow.distribute_pitch`, because
    the wrist alone stalls at -34.8 on the way up while idle rests it near -32.
    The calibration constants are gaze's own (GAZE_PITCH_DEG_PER_FRAME,
    GAZE_PITCH_MAX_STEP_DEG): one source of truth for how many degrees a frame
    fraction is worth on this lens.

    Both corrections go out as ONE absolute `move_and_hold`, the way `nudge`
    and gaze each already move — a yaw nudge followed by a separate pitch move
    would double the settle time of every iteration.

    Deliberately duplicated rather than factored out of `aim_for_look` or
    `_maybe_pitch`: each of those loops is entangled with the state it keeps
    across calls, and pulling them apart is a larger change than any caller
    needs today.

    `probe` takes a frame and returns an (x, y, w, h) box or None, so the caller
    keeps ownership of WHAT is being centred — the search passes its own target
    detector, where the aim would pass the closest-subject policy.
    """
    import hal.app_state as state
    from hal.drivers.tracking import constants as C
    from hal.drivers.tracking import servo_follow

    # Cleared at entry, exactly as aim_for_look does. The flag means "abort the
    # correction IN FLIGHT"; it is set only by the button's single click and
    # nothing resets it afterwards, so a click hours ago would otherwise defeat
    # every correction that follows. Device-observed on lamp-ac82: the first
    # centring ever run there returned "aborted after 0 iteration(s)" on a flag
    # left over from long before the sweep began. A click landing DURING the
    # correction still stops it — the loop re-checks on every iteration.
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
        """One more fresh frame after the LAST move, so the result describes
        where the lamp is pointing now rather than where it was pointing
        before it moved there.

        Device-observed on lamp-ac82 ("find my doll"): three corrections, the
        third one centred the doll — the user watched it happen — and the frame
        persisted was the one measured BEFORE that move, doll top-left,
        `centred: false`. The deadline and max-iteration exits fired at the top
        of the next iteration, before any frame had been taken since the move.
        Bounded by _grab_frame's own wait; the deadline does not apply to it.
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
                    # Gone, not flickering. Report the last good box rather
                    # than nothing: the caller still has something true to show.
                    return _result(False, "lost the subject")
                # Try again on a fresh frame; the deadline check above bounds it.
                require_fresh = True
                continue
            misses = 0

            _measure(frame, box)

            # Learn the local degrees-per-dx_frac from what the previous step
            # actually achieved. The lens is fisheye — device-measured 91 deg per
            # frac at the centre against 229 at the edge — so a fixed FOV is only
            # ever the first guess.
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

            # Yaw sign per the tracker's verified convention: dx>0 (subject right
            # of centre) -> base_yaw INCREASES. Do not flip this without device
            # evidence. Magnitude from the MEASURED scale once we have one; the
            # config FOV is only the first-step guess.
            yaw_deg = 0.0
            if not _yaw_ok():
                scale = (scale_deg * SCALE_SAFETY if scale_deg is not None
                         else config.LOOK_AIM_FOV_DEG)
                yaw_deg = AIM_GAIN * last_dx_frac * scale
                yaw_deg = max(-MAX_STEP_DEG, min(MAX_STEP_DEG, yaw_deg))
                base_yaw = float(current.get("base_yaw.pos", 0.0)) + yaw_deg
                target["base_yaw.pos"] = max(C.YAW_MIN, min(C.YAW_MAX, base_yaw))
                pending_calib = (_yaw_of(svc), last_dx_frac)

            # Pitch: gaze's step, gaze's sign, gaze's joint distribution.
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
    """Degrees of yaw per unit dx_frac, measured from what the last step did.

    This replaces the fixed FOV constant, which cannot be right everywhere: the
    lens is a fisheye, so degrees-per-pixel grows toward the edge (device-
    measured 91 deg centre, 229 deg edge). One constant either overshoots the
    middle or crawls at the edge — measuring the LOCAL scale each step avoids
    choosing.

    Returns None when the step is not informative enough to divide by.
    """
    if abs(moved_deg) < CALIB_MIN_MOVE_DEG or abs(shift_frac) < CALIB_MIN_SHIFT_FRAC:
        return None
    if (moved_deg > 0) != (shift_frac > 0):
        # The subject moved the wrong way for our correction — they walked, or
        # the detector jumped to something else. Not a measurement of optics.
        return None
    scale = abs(moved_deg) / abs(shift_frac)
    if not (MIN_SCALE_DEG <= scale <= MAX_SCALE_DEG):
        return None
    return scale


# A joint is "already there" within this much, so a search does not re-issue a
# move for rounding noise on every step.
POSE_TOLERANCE_DEG: float = 2.0


def _bearing_step_target(svc: Any, est: Any, current: dict):
    """Absolute pose for one bearing step: yaw stepped, the rest restored.

    Returns (target, step_deg), or (None, 0.0) when the head is already in the
    remembered shape.

    The move goes straight to the remembered pose. It used to advance in fixed
    hops, re-detecting between them so it could not sail past
    someone standing en route — but the lens sees about 110 deg, so anyone
    between here and there is already in frame before the head moves at all.
    The hops bought no extra coverage and cost a detect plus a settle each,
    which is most of a second per hop against the aim's deadline.
    """
    cur_yaw = float(current.get("base_yaw.pos", 0.0))
    delta = float(est.bearing_deg) - cur_yaw
    step = delta

    try:
        valid = set(svc.get_joint_names())
    except Exception:
        valid = set(current.keys())

    # isinstance, not truthiness: a malformed estimate (or a test double) can
    # carry a non-dict here, and iterating it would raise inside the caller's
    # except and silently disable the whole bearing step.
    pose = getattr(est, "pose", None)
    if not isinstance(pose, dict):
        pose = {}

    target: dict = {}
    for joint, value in pose.items():
        if joint == "base_yaw.pos" or joint not in valid:
            continue
        if abs(float(value) - float(current.get(joint, value))) > POSE_TOLERANCE_DEG:
            target[joint] = float(value)

    # Yaw still to travel, or only the posture is wrong (head left pointing at
    # the floor at the right bearing — the case that made a correct search
    # sweep the ground and find nobody).
    if abs(delta) >= 1.0:
        target["base_yaw.pos"] = cur_yaw + step
    elif not target:
        return None, 0.0
    return target, step


def _score_prediction(bearing_steps: int, found: bool) -> None:
    """Tell the estimate whether turning to it actually found anyone.

    Only meaningful when we actually turned to the bearing — an aim that never
    consulted it says nothing about whether it is still right.
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

    Passive by design: nothing reads the estimate yet. Failures are swallowed —
    losing a sample must never cost the user their answer.
    """
    if abs(dx_frac) > RECORD_DEADBAND_FRAC:
        return
    try:
        from hal.drivers.tracking import user_bearing

        positions = svc.get_positions()
        yaw = float(positions.get("base_yaw.pos", 0.0))
        # The whole shape, not just the base: pitch lives across base/elbow/wrist,
        # so yaw alone cannot describe "looking at the user".
        user_bearing.record_sighting(yaw, pose=positions)
    except Exception as e:
        logger.debug("[look-aim] bearing record skipped: %s", e)


def aim_for_look(deadline_s: float, detector: Any = None) -> AimResult:
    """Centre the subject in yaw, then return so the caller can capture.

    Always returns — a failed aim must still let `look` capture something,
    because dead air is worse than an imperfectly framed frame.
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
    # (yaw, dx_frac) captured just before the last move, so the next
    # measurement can tell what that move actually achieved.
    pending_calib: Optional[Tuple[float, float]] = None
    last_move_deg = 0.0
    announced_found = False
    # Whether `look_searching` was ever spoken. Only an ANNOUNCED search
    # owes the user a resolution — see the give-up branch.
    announced_search = False
    # Did this aim ever confirm a subject? Read by _result to score the
    # remembered bearing exactly once, whichever way the aim exits.
    found_any = False
    start_yaw = _yaw_of(svc)
    steps: list = []
    bearing_consulted: Optional[dict] = None

    def _result(aimed: bool, reason: str) -> AimResult:
        """Build the outcome with the pose actually reached, so a trace shows
        whether the head moved rather than just what was decided.

        Also scores the remembered bearing, HERE rather than at each exit. The
        aim leaves by five doors — centred, deadline, max iterations, no fresh
        frame, occlusion hold, give-up — and only the give-up one used to
        report a miss. A bearing move plus its settle is most of a second
        against an 8s deadline, so timing out right after turning is ordinary,
        and every one of those was an invisible failure: the estimate kept its
        confidence while repeatedly finding nobody.

        Scoring in the single place every exit passes through means a door
        added later cannot forget to.
        """
        _score_prediction(bearing_steps, found=found_any)
        # A moved head is a parked head (`nudge` and `_step_toward_bearing`
        # both end in `move_and_hold`) — hand it back to idle after the
        # capture the caller is about to take. Not when nothing moved:
        # playback was never preempted, and dispatching idle over a running
        # idle restarts it.
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
                    # Aim on what we have rather than steering blind.
                    return _result(
                        abs(last_dx_frac or 1.0) <= CENTRE_DEADBAND_FRAC, "no fresh frame"
                    )
                return _result(False, "no frame")

            with look_debug.stage("aim.detect"):
                box, kind, conf = _detect_subject(detector, frame)
            if box is None:
                # Log the empty frame too — "what did it see when it saw nothing"
                # is exactly the question a hold/search step raises.
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
                # Priority 3 — go to the remembered bearing, then re-detect from
                # a post-move frame. One absolute move, not a series of hops:
                # see MAX_BEARING_STEPS for why the hops were removed.
                probe: dict = {}
                if bearing_steps < MAX_BEARING_STEPS and _step_toward_bearing(svc, probe):
                    bearing_consulted = probe.get("bearing")
                    steps.append({"n": iterations + 1, "saw": None,
                                  "action": "step toward remembered bearing",
                                  "bearing": probe.get("bearing"), "yaw": _yaw_of(svc)})
                    if bearing_steps == 0 and config.LOOK_AIM_SPEAK:
                        # Only on the FIRST step: the lamp is about to turn away from
                        # the user mid-question, which reads as broken unless explained.
                        announced_search = True
                        _say("look_searching")
                    bearing_steps += 1
                    iterations += 1
                    continue
                bearing_consulted = probe.get("bearing", bearing_consulted)
                steps.append({"n": iterations + 1, "saw": None,
                              "action": probe.get("skipped", "give up — nothing found"),
                              "yaw": _yaw_of(svc)})
                # Before giving up, actually look around. `look_lost` claims
                # "I can't find you", and until now it said that having only
                # turned toward a remembered bearing — which is a guess about
                # where someone WAS, not a search. The phrase should be earned.
                if not announced_search and config.LOOK_AIM_SPEAK:
                    # The lamp is about to move a lot and take seconds over it.
                    # Saying so is what turns that from dead air into waiting.
                    announced_search = True
                    _say("look_searching")
                swept_at = time.monotonic()
                found_by_sweep = _sweep_for_subject()
                # The clock stops while sweeping. The deadline exists so a live
                # turn never stalls in SILENCE; the announcement above has
                # already dealt with that, and charging the sweep against a
                # budget it cannot fit in would mean never sweeping at all.
                t_end += time.monotonic() - swept_at
                steps.append({"n": iterations + 1,
                              "action": "looked around",
                              "saw": "subject" if found_by_sweep else None,
                              "yaw": _yaw_of(svc)})
                if found_by_sweep:
                    # Back into the loop: the sweep stopped on a subject but did
                    # not centre them, and centring is this function's job.
                    iterations += 1
                    continue

                # Close the loop the announcement opened. `look_searching`
                # promises to look; going silent here leaves the lamp turned
                # away mid-question with nothing said, while the model answers
                # about whatever the camera happened to be facing. Only when a
                # search was actually announced — a look that never turned owes
                # no explanation, and narrating every failed detection would be
                # the noise `look_capturing` is already gated to avoid.
                if announced_search and config.LOOK_AIM_SPEAK:
                    _say("look_lost")
                return _result(False, "subject not found")

            if bearing_steps > 0 and not announced_found and config.LOOK_AIM_SPEAK:
                # Once, and only after a search was announced. `bearing_steps`
                # stays above zero for the REST of the aim, so without this latch
                # every subsequent centring iteration re-announced it — device
                # 2026-08-19 said "bạn đây rồi" four times in three seconds.
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

            # Learn the local scale from what the previous step actually did,
            # before deciding whether we are done.
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
            # Magnitude comes from the MEASURED scale once we have one; the config
            # FOV is only the first-step guess.
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
