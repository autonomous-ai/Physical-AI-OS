"""Deliberate search sweep — asked for, never inline."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

import hal.app_state as state
from hal.drivers.tracking import constants as C
from hal.drivers.tracking import body
from hal.drivers.tracking.aim import _detect_subject, _grab_frame

logger = logging.getLogger(__name__)

# How far the base turns between stops.
STEP_DEG: float = 90.0
# Servos are commanded, then given time to stop ringing before a frame is read.
# SERVO_SMOOTH_TIME (0.32) is the easing constant this mirrors.
SETTLE_S: float = 0.35
MOVE_DURATION_S: float = 0.3
# How long to wait for the arm to actually ARRIVE before settling and shooting.
# move_and_hold returns when it has finished sending frames, not when the servos have
# got there.
ARRIVE_TIMEOUT_S: float = 7.0
ARRIVE_STILL_DEG: float = 0.8

# How fast the base may turn WHILE SWEEPING, in STS3215 Goal_Speed units.
SWEEP_YAW_SPEED: int = 1200
MAX_STOPS: int = 3
# How far, as a fraction of the frame, the centring probe will follow the nearest
# candidate between two frames.
STICKY_MAX_JUMP_FRAC: float = 0.55
# Slack on the "toward the centre" test: detector jitter on a box edge is a few
# percent of the frame and must not read as the object retreating.
STICKY_AWAY_TOL_FRAC: float = 0.06

# Where the head looks at each yaw stop, in order: left, straight on, right — then back
# to centre before the base turns again.
ROLL_LOOK_DEG: float = 45.0
PITCH_LOOK_DEG: float = 25.0
ROLL_DIAG_DEG: float = ROLL_LOOK_DEG
PITCH_DIAG_DEG: float = PITCH_LOOK_DEG

# The looks the head walks at each bearing, as (wrist_roll, pitch offset).
LOOK_CIRCLE = (
    (0.0, 0.0),
    (-ROLL_LOOK_DEG, 0.0),
    (-ROLL_DIAG_DEG, +PITCH_DIAG_DEG),
    (0.0, +PITCH_LOOK_DEG),
    (+ROLL_DIAG_DEG, +PITCH_DIAG_DEG),
    (+ROLL_LOOK_DEG, 0.0),
    (+ROLL_DIAG_DEG, -PITCH_DIAG_DEG),
    (0.0, -PITCH_LOOK_DEG),
    (-ROLL_DIAG_DEG, -PITCH_DIAG_DEG),
)
HALF_LOOKS: int = 6
# The half ring mirrored UP, for the user look-around only (#545). Faces sit at or
# above the seated view the sweep starts from; the down looks point at desks, and a
# standing user was missed through all 18 looks. Object searches keep looking down.
USER_LOOK_CIRCLE = tuple((roll, -dp) for roll, dp in LOOK_CIRCLE[:HALF_LOOKS])
# The gaze watcher's unasked look-around: a glance left and right at the bearing it
# already checked, no base turn. A living thing that turns to a voice and finds nobody
# glances about and lets it go; it does not quarter the room like a security camera.
GLANCE_LOOKS = ((-ROLL_LOOK_DEG, 0.0), (+ROLL_LOOK_DEG, 0.0))

# Margin held off the wrist_pitch soft stop. Device-measured 2026-09-09 on lamp-ac82:
# wrist_pitch reached -89.55 going up (stopped by WRIST_PITCH_MIN, not by the joint) and
# -16.61 going down with no stall at all.
WRIST_PITCH_MARGIN: float = 2.0

_abort_evt = threading.Event()


def request_abort() -> None:
    """Stop an in-flight search."""
    _abort_evt.set()


@dataclass
class SearchResult:
    found: bool
    reason: str
    looks_visited: int = 0
    found_at_yaw: Optional[float] = None
    found_at_roll: Optional[float] = None
    bearings_visited: int = 0
    kind: Optional[str] = None
    box: Optional[tuple] = None
    centred: bool = False
    image_path: Optional[str] = None


def _stop_list(seed: float) -> List[float]:
    """The seed first, then the remaining stops left to right.

    Device-observed 2026-08-25 with pure left-to-right ordering: the sweep found a
    person at yaw -102 — a colleague at another desk — while the user sat at the seed,
    -12, which it never reached.
    """
    seed = max(C.YAW_MIN, min(C.YAW_MAX, seed))
    span = (MAX_STOPS - 1) // 2
    stops: List[float] = [seed]
    for i in list(range(1, span + 1)) + list(range(-1, -span - 1, -1)):
        y = max(C.YAW_MIN, min(C.YAW_MAX, seed + i * STEP_DEG))
        if y not in stops:
            stops.append(y)
    return stops


def _current_yaw(svc: Any) -> float:
    try:
        return float(svc.get_positions().get("base_yaw.pos", 0.0))
    except Exception:
        return 0.0


def _seed_from_bearing(svc: Any) -> float:
    """Where to look first, and from what POSTURE."""
    try:
        from hal.drivers.tracking import aim, user_bearing

        est = user_bearing.read_estimate()
        if est is None:
            seeded = _rest_on_idle_pose(svc)
            return _current_yaw(svc) if seeded is None else seeded
        if est.confidence < aim.MIN_BEARING_CONFIDENCE:
            logger.info(
                "[search] ignoring bearing %+.1f — confidence %.2f < %.2f",
                est.bearing_deg, est.confidence, aim.MIN_BEARING_CONFIDENCE,
            )
            seeded = _rest_on_idle_pose(svc)
            return _current_yaw(svc) if seeded is None else seeded

        try:
            current = svc.get_positions()
            target, _step = aim._bearing_step_target(svc, est, current)
            if target:
                duration = aim.min_move_duration(
                    state.safety_policy, target, current, MOVE_DURATION_S
                )
                svc.move_and_hold(target, duration=duration)
                logger.info(
                    "[search] restored remembered posture (%d joints) before sweeping",
                    len(target),
                )
        except Exception as e:
            # A failed restore must not cost the search: sweeping from the
            # wrong pitch still beats not sweeping at all.
            logger.warning("[search] posture restore skipped: %s", e)

        return est.bearing_deg
    except Exception:
        seeded = _rest_on_idle_pose(svc)
        return _current_yaw(svc) if seeded is None else seeded


def _wait_until_still(svc: Any, target: dict) -> None:
    """Block until the commanded joints stop moving, or the timeout bites.

    Waits for the joints to STOP rather than to reach their target.
    """
    t0 = time.monotonic()
    deadline = t0 + ARRIVE_TIMEOUT_S
    polls = 0
    last: Optional[dict] = None
    while time.monotonic() < deadline:
        try:
            poll_t = time.monotonic()
            now_pose = svc.get_positions()
            read_s = time.monotonic() - poll_t
            polls += 1
        except Exception:
            return
        if last is not None and all(
            abs(float(now_pose.get(j, 0.0)) - float(last.get(j, 0.0))) < ARRIVE_STILL_DEG
            for j in target
        ):
            logger.info("[search] settled in %.2fs (%d polls, last read %.0fms)",
                        time.monotonic() - t0, polls, read_s * 1000)
            return
        last = now_pose
        time.sleep(0.1)
    logger.info("[search] still moving after %.1fs — shooting anyway", ARRIVE_TIMEOUT_S)


def _look_at(svc: Any, roll: float, wrist_pitch: Optional[float] = None,
             yaw: Optional[float] = None) -> bool:
    """Point the camera at one look. False if the move could not be made."""
    try:
        from hal.drivers.tracking import aim

        current = svc.get_positions()
        target = {"wrist_roll.pos": float(roll)}
        if wrist_pitch is not None:
            target["wrist_pitch.pos"] = float(wrist_pitch)
        if yaw is not None:
            target["base_yaw.pos"] = float(yaw)
        if all(abs(v - float(current.get(j, 0.0))) <= 0.5 for j, v in target.items()):
            return True
        duration = aim.min_move_duration(
            state.safety_policy, target, current, MOVE_DURATION_S
        )
        svc.move_and_hold(target, duration=duration)
        _wait_until_still(svc, target)
        return True
    except Exception as e:
        logger.warning("[search] look to roll %+.0f failed: %s", roll, e)
        return False


def _sticky_probe(detector: Any, target: str, first_box: tuple) -> Callable[[Any], Optional[tuple]]:
    """A probe for the centring correction that keeps the INSTANCE the sweep
    found, when the frame holds more than one of the class.
    """
    anchor = {"box": tuple(first_box)}

    def _centre(b: tuple) -> tuple:
        return (b[0] + b[2] / 2.0, b[1] + b[3] / 2.0)

    def probe(frame: Any) -> Optional[tuple]:
        cands: list = []
        getter = getattr(detector, "detect_candidates", None)
        if getter is not None and target not in ("person", "face"):
            try:
                cands = list(getter(frame, target) or [])
            except Exception as e:
                logger.debug("[search] detect_candidates failed, using detect: %s", e)
                cands = []
        if cands:
            ax, ay = _centre(anchor["box"])
            box = min((tuple(b) for b, _conf in cands),
                      key=lambda b: (_centre(b)[0] - ax) ** 2 + (_centre(b)[1] - ay) ** 2)
            # Nearest is not the same as near. One correction moves the object by at
            # most MAX_STEP_DEG over a ~100 deg lens — under half the frame — so
            # anything further has to be a different object.
            bx, by = _centre(box)
            fh, fw = frame.shape[0], frame.shape[1]
            if abs(bx - ax) > STICKY_MAX_JUMP_FRAC * fw or abs(by - ay) > STICKY_MAX_JUMP_FRAC * fh:
                logger.info("[search] probe: nearest %s is %.0f%% away — not the same one, treating as a miss",
                            target, 100.0 * max(abs(bx - ax) / fw, abs(by - ay) / fh))
                return None
            for prev, now, size in ((ax, bx, fw), (ay, by, fh)):
                p_off, n_off = prev - size / 2.0, now - size / 2.0
                same_side = (p_off < 0) == (n_off < 0)
                if same_side and abs(n_off) > abs(p_off) + STICKY_AWAY_TOL_FRAC * size:
                    logger.info("[search] probe: nearest %s moved AWAY from centre (%.0f%% -> %.0f%%) — "
                                "another instance, treating as a miss",
                                target, 100.0 * p_off / size, 100.0 * n_off / size)
                    return None
        else:
            box = _detect_target(detector, frame, target)[0]
        if box is not None:
            anchor["box"] = tuple(box)
        return box

    return probe


def _user_face_probe(first_box: tuple) -> Callable[[Any], Optional[tuple]]:
    """Centre on the FACE the user check passed, never on a nearby body.

    `_sticky_probe` falls back to `_detect_subject`, which prefers the nearest person
    box, and that is the #545 bug.
    """
    anchor = {"box": tuple(first_box)}

    def probe(frame: Any) -> Optional[tuple]:
        from hal.drivers.tracking import aim, detection, frame_utils

        small, scale = frame_utils.downscale(frame)
        with aim._detector_lock_use:
            faces = detection.detect_faces_with_landmarks(small)
        if not faces:
            return None
        s = 1.0 / scale if scale else 1.0
        boxes = [(int(x * s), int(y * s), int(w * s), int(h * s))
                 for (x, y, w, h), _lm in faces]
        ax = anchor["box"][0] + anchor["box"][2] / 2.0
        ay = anchor["box"][1] + anchor["box"][3] / 2.0
        box = min(boxes, key=lambda b: (b[0] + b[2] / 2.0 - ax) ** 2
                  + (b[1] + b[3] / 2.0 - ay) ** 2)
        fh, fw = frame.shape[0], frame.shape[1]
        if (abs(box[0] + box[2] / 2.0 - ax) > STICKY_MAX_JUMP_FRAC * fw
                or abs(box[1] + box[3] / 2.0 - ay) > STICKY_MAX_JUMP_FRAC * fh):
            logger.info("[search] probe: nearest face jumped too far — not the same one")
            return None
        anchor["box"] = box
        return box

    return probe


def _detect_target(detector: Any, frame: Any, target: str):
    """Find `target` in the frame. Returns (box, kind), or (None, None)."""
    from hal.drivers.tracking.aim import _detect_subject

    if target in ("person", "face"):
        box, kind, _conf = _detect_subject(detector, frame)
        return box, kind
    box = detector.detect(frame, target)
    return box, (target if box is not None else None)


def _abandon(svc: Any, seed_pose: Optional[dict], visited: int) -> "SearchResult":
    """End an aborted sweep on the pose it started from."""
    _restore(svc, seed_pose)
    logger.info("[search] aborted after %d look(s) — back to the starting pose", visited)
    return SearchResult(False, "aborted", visited)


def _persist_hit(frame: Any, box: Any, label: str) -> Optional[str]:
    """Write the frame the sweep stopped on, with the detection drawn on it.

    `frame` and `box` MUST come from the same grab.
    """
    try:
        import os

        from hal.drivers.tracking.look_debug import encode_annotated

        jpg = encode_annotated(frame, box, label, centre_lines=False)
        if jpg is None:
            return None
        os.makedirs(state._SNAPSHOT_DIR, exist_ok=True)
        path = os.path.join(state._SNAPSHOT_DIR,
                            f"snap_{int(time.time() * 1000)}.jpg")
        with open(path, "wb") as f:
            f.write(jpg)
        state._snapshot_paths.append(path)
        while len(state._snapshot_paths) > state._SNAPSHOT_MAX:
            oldest = state._snapshot_paths.pop(0)
            try:
                os.remove(oldest)
            except OSError:
                pass
        return path
    except Exception as e:
        logger.warning("[search] could not persist the winning frame: %s", e)
        return None


def _restore(svc: Any, pose: Optional[dict]) -> None:
    """Put the arm back on a remembered pose. Never raises."""
    if not pose:
        return
    try:
        from hal.drivers.tracking import aim

        current = svc.get_positions()
        duration = aim.min_move_duration(
            state.safety_policy, pose, current, MOVE_DURATION_S
        )
        svc.move_and_hold(pose, duration=duration)
    except Exception as e:
        logger.warning("[search] could not return to the starting pose: %s", e)


def _straighten_head_onto(svc: Any, yaw: float, roll: float) -> None:
    """Keep looking where the subject was found, but with the head level."""
    aimed_at = yaw + roll
    settled = max(C.YAW_MIN, min(C.YAW_MAX, aimed_at))
    # Whatever the base cannot absorb stays in the head, so the camera still
    # points at the subject even when the turn runs into the mechanical limit.
    try:
        from hal.drivers.tracking import aim

        target = {"base_yaw.pos": settled, "wrist_roll.pos": aimed_at - settled}
        current = svc.get_positions()
        duration = aim.min_move_duration(
            state.safety_policy, target, current, MOVE_DURATION_S
        )
        svc.move_and_hold(target, duration=duration)
    except Exception as e:
        logger.warning("[search] could not straighten onto the subject: %s", e)


def _rest_on_idle_pose(svc: Any) -> Optional[float]:
    """Stand the arm on the idle recording's own pose, and return its yaw."""
    baseline = getattr(svc, "_idle_baseline", None)
    if not isinstance(baseline, dict) or not baseline:
        return None
    target = {j: float(v) for j, v in baseline.items() if j.endswith(".pos")}
    if not target:
        return None
    try:
        from hal.drivers.tracking import aim

        current = svc.get_positions()
        duration = aim.min_move_duration(
            state.safety_policy, target, current, MOVE_DURATION_S
        )
        svc.move_and_hold(target, duration=duration)
        logger.info(
            "[search] no bearing yet — resting on the idle pose (%d joints) before sweeping",
            len(target),
        )
        return float(target.get("base_yaw.pos", _current_yaw(svc)))
    except Exception as e:
        logger.warning("[search] idle-pose restore skipped: %s", e)
        return None


def _seed_yaw(svc: Any) -> float:
    """Backwards-compatible alias — see _seed_from_bearing."""
    return _seed_from_bearing(svc)


def _say_at_the_midpoint() -> Callable[[int, int], None]:
    """A progress handler that breaks the silence once, halfway through."""
    said = {"done": False}

    def handler(visited: int, total: int) -> None:
        if said["done"] or visited * 2 < total:
            return
        said["done"] = True
        from hal.drivers.tracking.aim import _say

        _say("look_still_searching")

    return handler


def search_for_subject(target: str = "person", detector: Any = None,
                       on_progress: Optional[Callable[[int, int], None]] = None,
                       exhaustive: bool = False, for_user: bool = False,
                       glance: bool = False) -> SearchResult:
    """Sweep for a subject, stopping at the first one seen.

    `for_user`: the subject is the lamp's user, not anybody (#545). Each look must
    turn up a face that passes user_check.adopts_bearing; a body alone never ends it.
    `glance`: GLANCE_LOOKS at the seed bearing only, for a look-around nobody asked for.
    """
    _abort_evt.clear()

    if exhaustive and target not in ("person", "face"):
        logger.info("[search] exhaustive ignored for '%s' — an object search "
                    "stops at the first sighting", target)
        exhaustive = False
    if for_user and (exhaustive or target != "person"):
        logger.info("[search] for_user ignored for '%s'%s", target,
                    " (exhaustive)" if exhaustive else "")
        for_user = False

    cap = getattr(state, "camera_capture", None)
    svc = getattr(state, "animation_service", None)
    if cap is None or svc is None:
        return SearchResult(False, "no camera or animation service")
    if getattr(state, "_camera_disabled", False):
        return SearchResult(False, "camera disabled")

    if detector is None:
        from hal.drivers.tracking.aim import get_detector

        detector = get_detector()
        if detector is None:
            return SearchResult(False, "no detector")

    # Own the body for the whole sweep. Device-traced 2026-08-25 during one sweep: idle
    # wrote base_yaw 280 times to the search's 31. The visible result was a base that
    # crawled.
    from hal.drivers.tracking import aim

    if on_progress is None:
        on_progress = _say_at_the_midpoint()

    with aim.servo_ownership():
        capped = svc.set_joint_speed("base_yaw", SWEEP_YAW_SPEED)
        try:
            res = _sweep(svc, cap, detector, target, on_progress, exhaustive, for_user,
                         glance)
        finally:
            if capped:
                svc.set_joint_speed(
                    "base_yaw",
                    getattr(svc, "UNWRITTEN_SPEED_EQUIVALENT", 0),
                )

    hold_s = body.HOLD_AFTER_FIND_S if res.found else 0.0
    body.release_to_idle_later(hold_s, f"search for '{target}' ended")
    return res


def _look_list(seed_pose: Optional[dict], exhaustive: bool,
               for_user: bool = False, glance: bool = False) -> list:
    """Absolute (roll, wrist_pitch) for every look at one bearing."""
    base_wp = None
    if seed_pose:
        try:
            base_wp = float(seed_pose["wrist_pitch.pos"])
        except (KeyError, TypeError, ValueError):
            base_wp = None
    if glance:
        pattern = GLANCE_LOOKS
    elif for_user:
        pattern = USER_LOOK_CIRCLE
    else:
        pattern = LOOK_CIRCLE if exhaustive else LOOK_CIRCLE[:HALF_LOOKS]
    lo = C.WRIST_PITCH_MIN + WRIST_PITCH_MARGIN
    hi = C.WRIST_PITCH_MAX - WRIST_PITCH_MARGIN
    out = []
    for roll, dp in pattern:
        wp = None if base_wp is None else max(lo, min(hi, base_wp + dp))
        out.append((roll, wp))
    return out


def _sweep(svc: Any, cap: Any, detector: Any, target: str,
           on_progress: Optional[Callable[[int, int], None]] = None,
           exhaustive: bool = False, for_user: bool = False,
           glance: bool = False) -> SearchResult:
    """The sweep itself, with the body already owned."""
    from hal.drivers.tracking import aim

    stops = _stop_list(_seed_yaw(svc))
    if glance:
        stops = stops[:1]
    try:
        seed_pose = {j: float(v) for j, v in svc.get_positions().items()
                     if j.endswith('.pos')}
    except Exception:
        seed_pose = None
    looks = _look_list(seed_pose, exhaustive, for_user, glance)
    total_looks = len(stops) * len(looks)
    logger.info("[search] sweeping %d bearings x %d looks (%d total) for '%s': %s",
                len(stops), len(looks), total_looks, target,
                [round(s) for s in stops])

    visited = 0
    bearings = 0
    found: List[SearchResult] = []
    for yaw in stops:
        bearings += 1
        if _abort_evt.is_set():
            return _abandon(svc, seed_pose, visited)

        # Look around from here before turning the body again. The base turns +90 while
        # the head turns -90 and the camera never leaves the spot.
        for n, (roll, wrist_pitch) in enumerate(looks):
            if _abort_evt.is_set():
                return _abandon(svc, seed_pose, visited)
            if not _look_at(svc, roll, wrist_pitch,
                            yaw=yaw if n == 0 else None):
                continue

            time.sleep(SETTLE_S)
            visited += 1
            if on_progress is not None:
                try:
                    on_progress(visited, total_looks)
                except Exception as e:
                    # A talkative caller must never be able to sink the search.
                    logger.debug("[search] progress callback failed: %s", e)

            _t_grab = time.monotonic()
            frame = _grab_frame(cap)
            _grab_ms = (time.monotonic() - _t_grab) * 1000
            if frame is None:
                continue
            _t_det = time.monotonic()
            if for_user:
                from hal.drivers.tracking import user_check

                track = user_check.best_user_face(
                    user_check.observe_faces(lambda: _grab_frame(cap)), "[search]",
                )
                box, kind = (track.box, "face") if track is not None else (None, None)
            else:
                box, kind = _detect_target(detector, frame, target)
            logger.info("[search] look %d/%d: grab %.0fms detect %.0fms -> %s",
                        visited, total_looks, _grab_ms,
                        (time.monotonic() - _t_det) * 1000,
                        kind or "nothing")
            if box is not None:
                logger.info(
                    "[search] found %s at yaw %+.0f roll %+.0f after %d look(s)",
                    kind, yaw, roll, visited,
                )
                hit = SearchResult(True, f"found {kind}", visited, yaw, roll,
                                   bearings, kind, box)
                if not exhaustive:
                    # Correct FIRST, straighten AFTER. And without any correction at all
                    # the sweep pointed at `yaw + roll`.
                    probe = (_user_face_probe(box) if for_user
                             else _sticky_probe(detector, target, box))
                    centred = aim.centre_on_box(svc, cap, probe=probe)
                    hit.centred = centred.centred
                    if centred.box is not None:
                        hit.box = centred.box
                    logger.info("[search] centring: %s after %d iteration(s)",
                                centred.reason, centred.iterations)
                    try:
                        now_pose = svc.get_positions()
                        now_yaw = float(now_pose.get("base_yaw.pos", yaw))
                        now_roll = float(now_pose.get("wrist_roll.pos", roll))
                    except Exception:
                        now_yaw, now_roll = yaw + centred.yaw_total, roll
                    _straighten_head_onto(svc, now_yaw, now_roll)
                    hit.found_at_yaw = now_yaw
                    # Prefer the CENTRED frame and its box — that pair is what the lamp
                    # is pointing at now. Fall back to the frame that triggered the hit
                    # when the correction never got one (no fresh frame, an abort, a
                    # failed nudge).
                    shot_frame, shot_box = ((centred.frame, centred.box)
                                            if centred.frame is not None
                                            else (frame, box))
                    hit.image_path = _persist_hit(shot_frame, shot_box, kind)
                    return hit
                found.append(hit)

    if exhaustive and found:
        _restore(svc, seed_pose)
        logger.info("[search] full sweep: %d sighting(s) of '%s' across %d looks",
                    len(found), target, visited)
        return SearchResult(True, f"found {target} x{len(found)}", visited,
                            found[0].found_at_yaw, found[0].found_at_roll,
                            bearings, found[0].kind, found[0].box)

    _restore(svc, seed_pose)
    logger.info("[search] no %s found after %d look(s) — back to the starting pose",
                target, visited)
    return SearchResult(False, f"no {target} found", visited,
                        bearings_visited=bearings)
