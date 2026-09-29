"""A narrated tour of the joint limits — "show me how far you can move"."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, List, Optional, Tuple

import hal.app_state as state
from hal.drivers.tracking import constants as C
from hal.drivers.tracking.search import _wait_until_still

logger = logging.getLogger(__name__)

# Requested duration for one leg. The safety policy stretches it when the move would
# exceed SAFETY.md's max_speed, so this is a floor rather than a promise.
LEG_DURATION_S: float = 1.0
DWELL_S: float = 0.3

# How fast the base may turn WHILE DEMONSTRATING, in STS3215 Goal_Speed units. Same
# register and same reason as the sweep: base_yaw manages about 14 deg/s untouched,
# which makes a 135 deg leg take ~9 s.
DEMO_YAW_SPEED: int = 1600

# Per-joint reach, each measured from the pose the demo started in and clamped to the
# joint's travel.
ROLL_REACH_DEG: float = 45.0
PITCH_LOOK_DEG: float = 25.0
ELBOW_REACH_DEG: float = 10.0
BASE_PITCH_LEAN_DEG: float = 12.0
# Margin held off every soft stop, mirroring the sweep's WRIST_PITCH_MARGIN:
# commanding the exact limit stalls the servo against it.
PITCH_MARGIN: float = 2.0

TTS_WAIT_TIMEOUT_S: float = 3.0

_abort_evt = threading.Event()
_running = threading.Event()


def request_abort() -> None:
    """Stop an in-flight demo."""
    _abort_evt.set()


def is_running() -> bool:
    return _running.is_set()


def waypoints(seed_pose: dict) -> List[Tuple[dict, str]]:
    """(absolute pose, phrase pool) for each leg, in performance order."""
    def _seed(joint: str, default: float = 0.0) -> float:
        try:
            return float(seed_pose.get(joint, default))
        except (TypeError, ValueError):
            return default

    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    yaw0 = _seed("base_yaw.pos")
    roll0 = _seed("wrist_roll.pos")
    wp0 = _seed("wrist_pitch.pos")
    el0 = _seed("elbow_pitch.pos")
    bp0 = _seed("base_pitch.pos")

    wp_lo, wp_hi = C.WRIST_PITCH_MIN + PITCH_MARGIN, C.WRIST_PITCH_MAX - PITCH_MARGIN
    el_lo = C.PITCH_TRAVEL_MIN["elbow_pitch.pos"] + PITCH_MARGIN
    el_hi = C.PITCH_TRAVEL_MAX["elbow_pitch.pos"] - PITCH_MARGIN
    bp_lo = C.PITCH_TRAVEL_MIN["base_pitch.pos"] + PITCH_MARGIN
    bp_hi = C.PITCH_TRAVEL_MAX["base_pitch.pos"] - PITCH_MARGIN
    roll_lo, roll_hi = C.WRIST_ROLL_MIN + PITCH_MARGIN, C.WRIST_ROLL_MAX - PITCH_MARGIN

    return [
        ({"base_yaw.pos": C.YAW_MIN}, "demo_left"),
        ({"base_yaw.pos": C.YAW_MAX}, "demo_right"),
        ({"base_yaw.pos": yaw0}, "demo_centre"),
        ({"wrist_roll.pos": _clamp(roll0 - ROLL_REACH_DEG, roll_lo, roll_hi)}, "demo_head"),
        ({"wrist_roll.pos": _clamp(roll0 + ROLL_REACH_DEG, roll_lo, roll_hi)}, ""),
        ({"wrist_roll.pos": roll0}, ""),
        ({"wrist_pitch.pos": _clamp(wp0 - PITCH_LOOK_DEG, wp_lo, wp_hi)}, "demo_up"),
        ({"wrist_pitch.pos": _clamp(wp0 + PITCH_LOOK_DEG, wp_lo, wp_hi)}, "demo_down"),
        ({"wrist_pitch.pos": wp0}, ""),
        ({"elbow_pitch.pos": _clamp(el0 + ELBOW_REACH_DEG, el_lo, el_hi)}, "demo_neck"),
        ({"elbow_pitch.pos": _clamp(el0 - ELBOW_REACH_DEG, el_lo, el_hi)}, ""),
        ({"elbow_pitch.pos": el0}, ""),
        ({"base_pitch.pos": _clamp(bp0 - BASE_PITCH_LEAN_DEG, bp_lo, bp_hi)}, "demo_lean"),
        ({"base_pitch.pos": bp0}, ""),
    ]


def _wait_for_tts() -> None:
    """Let the previous phrase finish.

    Best-effort and bounded: a stuck speaking flag must not freeze the body part-way
    through a performance.
    """
    tts = getattr(state, "tts_service", None)
    if tts is None:
        return
    deadline = time.monotonic() + TTS_WAIT_TIMEOUT_S
    while time.monotonic() < deadline:
        if not getattr(tts, "speaking", False):
            return
        time.sleep(0.1)


def _go(svc: Any, pose: dict) -> None:
    """Command one leg and wait for the body to actually get there."""
    from hal.drivers.tracking import aim

    current = svc.get_positions()
    duration = aim.min_move_duration(state.safety_policy, pose, current,
                                     LEG_DURATION_S)
    svc.move_and_hold(pose, duration=duration)
    _wait_until_still(svc, pose)


def run(svc: Any) -> dict:
    """Perform the demo. Blocking — `start()` is what callers use."""
    from hal.drivers.tracking import aim

    _abort_evt.clear()
    _running.set()
    try:
        seed_pose = {j: float(v) for j, v in svc.get_positions().items()
                     if j.endswith(".pos")}
    except Exception as e:
        _running.clear()
        logger.warning("[demo] could not read the starting pose: %s", e)
        return {"completed": False, "reason": f"no pose: {e}", "waypoints": 0}

    wps = waypoints(seed_pose)
    logger.info("[demo] %d leg(s): yaw %.0f..%.0f, roll +/-%.0f, wrist pitch +/-%.0f, "
                "elbow +/-%.0f, base pitch -%.0f",
                len(wps), C.YAW_MIN, C.YAW_MAX, ROLL_REACH_DEG, PITCH_LOOK_DEG,
                ELBOW_REACH_DEG, BASE_PITCH_LEAN_DEG)
    done = 0
    try:
        with aim.servo_ownership():
            capped = svc.set_joint_speed("base_yaw", DEMO_YAW_SPEED)
            try:
                _wait_for_tts()
                aim._say("demo_intro")
                for pose, pool in wps:
                    if _abort_evt.is_set():
                        _go(svc, seed_pose)
                        logger.info("[demo] aborted after %d leg(s)", done)
                        return {"completed": False, "reason": "aborted",
                                "waypoints": done}
                    # A silent leg (return to centre, second half of a pair)
                    # must not reach the filler endpoint at all: an empty pool
                    # there falls through to the realtime "Hmm..." cue.
                    if pool:
                        _wait_for_tts()
                        aim._say(pool)
                    _go(svc, pose)
                    time.sleep(DWELL_S)
                    done += 1
                _go(svc, seed_pose)
                _wait_for_tts()
                aim._say("demo_done")
            finally:
                if capped:
                    svc.set_joint_speed(
                        "base_yaw",
                        getattr(svc, "UNWRITTEN_SPEED_EQUIVALENT", 0),
                    )
        return {"completed": True, "reason": "done", "waypoints": done}
    except Exception as e:
        logger.warning("[demo] failed after %d leg(s): %s", done, e)
        return {"completed": False, "reason": str(e), "waypoints": done}
    finally:
        _running.clear()


def start(svc: Any) -> dict:
    """Kick the demo off on its own thread and return at once."""
    if getattr(state, "_sleeping", False):
        logger.info("[demo] ignored -- device is sleeping")
        return {"started": False, "waypoints": 0, "reason": "sleeping"}
    if _running.is_set():
        logger.info("[demo] ignored -- already running")
        return {"started": False, "waypoints": 0, "reason": "already running"}
    wps = 0
    try:
        wps = len(waypoints(svc.get_positions()))
    except Exception as e:
        logger.debug("[demo] could not count legs before starting: %s", e)
    threading.Thread(target=run, args=(svc,), name="range-demo",
                     daemon=True).start()
    return {"started": True, "waypoints": wps, "reason": "started"}
