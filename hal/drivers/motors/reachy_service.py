"""Reachy Mini motion driver — wraps Pollen's `reachy_mini` SDK."""
from __future__ import annotations

import logging
import math
import os
import threading
from typing import Any, Dict, List, Optional, Set

import numpy as np
from scipy.spatial.transform import Rotation

from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose

_HEAD_MOTOR_IDS = (
    "body_rotation",
    "stewart_1", "stewart_2", "stewart_3", "stewart_4", "stewart_5", "stewart_6",
)

logger = logging.getLogger(__name__)


_HEAD_KEYS = (
    "head_x.pos", "head_y.pos", "head_z.pos",
    "head_roll.pos", "head_pitch.pos", "head_yaw.pos",
)
_ANTENNA_KEYS = ("antenna_left.pos", "antenna_right.pos")
_BODY_KEY = "body_yaw.pos"
JOINT_KEYS: Set[str] = {*_HEAD_KEYS, *_ANTENNA_KEYS, _BODY_KEY}

# Daemon-enforced limits (the daemon clamps to the nearest valid pose; these
# are documented here for reference): head pitch/roll ±40°, head yaw ±180°,
# body yaw ±160°, head-vs-body yaw delta ≤ 65°.

_EMOTES_DATASET = "pollen-robotics/reachy-mini-emotions-library"
_DANCES_DATASET = "pollen-robotics/reachy-mini-dances-library"


def _move_datasets() -> List[str]:
    """Dataset names to load, in search order. Duplicates removed, order kept."""
    raw = os.environ.get("HAL_REACHY_MOVES", "")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    names += [_EMOTES_DATASET, _DANCES_DATASET]
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out

import hal.presets as P

_AIM_TARGETS: Dict[str, Dict[str, float]] = {
    P.AIM_CENTER: {"head_yaw.pos": 0.0, "head_pitch.pos": 0.0, "head_z.pos": 0.0},
    P.AIM_DESK:   {"head_yaw.pos": 0.0, "head_pitch.pos": -30.0, "head_z.pos": 0.0},
    P.AIM_WALL:   {"head_yaw.pos": 0.0, "head_pitch.pos": 20.0, "head_z.pos": 0.0},
    P.AIM_LEFT:   {"head_yaw.pos": 40.0},
    P.AIM_RIGHT:  {"head_yaw.pos": -40.0},
    P.AIM_UP:     {"head_pitch.pos": 30.0},
    P.AIM_DOWN:   {"head_pitch.pos": -35.0},
    P.AIM_USER:   {"head_yaw.pos": 0.0, "head_pitch.pos": 15.0},
}

_MOVE_MAP: Dict[str, str] = {
    P.SERVO_CURIOUS:       "curious1",
    P.SERVO_HAPPY_WIGGLE:  "cheerful1",
    P.SERVO_SAD:           "sad1",
    P.SERVO_THINKING_DEEP: "thoughtful1",
    P.SERVO_IDLE:          "serenity1",
    P.SERVO_EXCITED:       "enthusiastic1",
    P.SERVO_SHY:           "shy1",
    P.SERVO_SHOCK:         "surprised1",
    P.SERVO_LISTENING:     "attentive1",
    P.SERVO_LAUGH:         "laughing1",
    P.SERVO_CONFUSED:      "confused1",
    P.SERVO_SLEEPY:        "tired1",
    P.SERVO_GREETING:      "welcoming1",
    P.SERVO_GOODBYE:       "resigned1",
    P.SERVO_NOD:           "yes1",
    P.SERVO_ACKNOWLEDGE:   "understanding1",
    P.SERVO_STRETCHING:    "relief1",
    P.SERVO_SCANNING:      "inquiring1",
    P.SERVO_HEADSHAKE:     "no1",
    P.SERVO_WAKE_UP:       "enthusiastic2",
    P.SERVO_MUSIC_GROOVE:    "dance1",
    P.SERVO_MUSIC_JAZZ:      "dance2",
    P.SERVO_MUSIC_CLASSICAL: "dance3",
    P.SERVO_MUSIC_HIPHOP:    "dance1",
    P.SERVO_MUSIC_ROCK:      "dance2",
    P.SERVO_MUSIC_WALTZ:     "dance3",
    P.SERVO_MUSIC_CHILL:     "dance1",
    P.SERVO_MUSIC_HYPE:      "dance2",
}

_MIN_MOVE_DURATION_S = 0.1

# Seconds the daemon takes to interpolate into a move's first pose
# (play_move(initial_goto_duration=...)).
_PLAY_RAMP_S = float(os.environ.get("HAL_REACHY_PLAY_RAMP_S", "0.8"))

# The SDK client holds one long-lived connection to the daemon. Anything that restarts
# the daemon — an OS reboot, `systemctl restart reachy-mini-daemon`, an update.
_RECONNECT_MARKERS = (
    "lost connection",
    "connection closed",
    "not connected",
    "broken pipe",
    "connection reset",
)


def _is_disconnect(err: Exception) -> bool:
    return any(m in str(err).lower() for m in _RECONNECT_MARKERS)


# Recordings are authored at ~50-60 Hz but their timestamps jitter: the shipped library
# has duplicate timestamps (1.1% of frames) and gaps as short as 1 ms against a 16 ms
# median.
_SPEED_WINDOW_S = 0.02


def peak_head_dps(timestamps: List[float], trajectory: List[Dict[str, Any]]) -> float:
    """Highest head rotation speed a recorded move demands, in deg/s.

    Measured as the true angle between two head orientations at least `_SPEED_WINDOW_S`
    apart, NOT as per-axis euler deltas.
    """
    n = min(len(timestamps), len(trajectory))
    peak = 0.0
    lo = 0
    for hi in range(1, n):
        while lo + 1 < hi and timestamps[hi] - timestamps[lo + 1] >= _SPEED_WINDOW_S:
            lo += 1
        dt = timestamps[hi] - timestamps[lo]
        if dt <= 0:
            continue
        prev = np.asarray(trajectory[lo]["head"], dtype=np.float64)[:3, :3]
        cur = np.asarray(trajectory[hi]["head"], dtype=np.float64)[:3, :3]
        angle = Rotation.from_matrix(cur @ prev.T).magnitude() * 180.0 / math.pi
        peak = max(peak, angle / dt)
    return peak


def _joints_from_sdk(head_pose: Any, antennas: Any, body_yaw_rad: Any) -> Dict[str, float]:
    """SDK pose (4x4 meters/rad, antennas rad, body yaw rad) → HAL joints (deg/mm)."""
    xyz = np.asarray(head_pose)[:3, 3] * 1000.0
    rpy = Rotation.from_matrix(np.asarray(head_pose)[:3, :3]).as_euler("xyz", degrees=True)
    return {
        "head_x.pos": float(xyz[0]),
        "head_y.pos": float(xyz[1]),
        "head_z.pos": float(xyz[2]),
        "head_roll.pos": float(rpy[0]),
        "head_pitch.pos": float(rpy[1]),
        "head_yaw.pos": float(rpy[2]),
        "antenna_right.pos": math.degrees(float(antennas[0])),
        "antenna_left.pos": math.degrees(float(antennas[1])),
        _BODY_KEY: math.degrees(float(body_yaw_rad)),
    }


class ReachyMotionService:
    """MotionService implementation backed by the Pollen reachy_mini daemon."""

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None,
                 safety_policy: Any = None):
        self._host = host or os.getenv("REACHY_DAEMON_HOST", "localhost")
        self._port = int(port or os.getenv("REACHY_DAEMON_PORT", "8000"))
        self._safety_policy = safety_policy
        self._mini: Optional[ReachyMini] = None
        # The SDK client documents no thread-safety guarantee — one writer.
        self._lock = threading.RLock()
        # Held for the whole of one recorded-move pass, so a play thread that
        # lost the servo cannot stream on top of the one that took it.
        self._play_lock = threading.Lock()
        self._target: Dict[str, float] = {k: 0.0 for k in JOINT_KEYS}
        self._released = False
        self._zero_mode = False
        self._hold_mode = False
        self._hold_explicit = False
        self._frozen = False
        # Body ownership — see MotionService in base.py. Split the same way the feetech
        # backend splits it.
        self._tracking_flag = False
        self._body_owners = 0
        self._body_owner_lock = threading.Lock()
        self._moves = None
        self._move_peak_dps: Dict[str, float] = {}
        self._play_thread: Optional[threading.Thread] = None
        # Bumped on every play request; the play thread stops repeating once its
        # own generation is stale (a newer play owns the servo).
        self._play_gen = 0
        self._music_playing = False
        self._music_recording: Optional[str] = None
        # Read by GET /servo alongside get_available_recordings().
        self._current_recording: Optional[str] = None

    def start(self, skip_wake: bool = False) -> None:
        with self._lock:
            # media_backend="no_media": HAL owns camera/audio via its own
            # drivers — the SDK must not grab them too.
            self._mini = ReachyMini(
                host=self._host, port=self._port, media_backend="no_media"
            )
            self._enable_motors()
            # wake_up() is a goto + a sound: exactly the performance a device
            # that was asleep must not put on just because HAL restarted.
            if skip_wake:
                logger.info("[reachy] wake_up skipped -- device was asleep")
            else:
                try:
                    self._mini.wake_up()
                except Exception as e:
                    logger.warning("[reachy] wake_up failed (continuing): %s", e)
        logger.info(
            "[reachy] connected to daemon at %s:%s (play ramp %.2fs%s)",
            self._host, self._port, _PLAY_RAMP_S,
            "" if self._safety_policy is not None else ", no safety policy",
        )

    def _enable_motors(self) -> None:
        """Put torque on the motors (caller holds _lock)."""
        if self._mini is None:
            return
        try:
            self._mini.enable_motors()
        except Exception as e:
            logger.warning("[reachy] enable_motors failed — robot will not move: %s", e)

    def stop(self, timeout: float = 5.0) -> None:
        self._music_playing = False
        self._music_recording = None
        self._take_servo()
        if self._play_thread and self._play_thread.is_alive():
            self._play_thread.join(timeout=timeout)
        with self._lock:
            if self._mini is None:
                return
            try:
                self._mini.goto_sleep()
            except Exception as e:
                logger.warning("[reachy] goto_sleep on stop failed: %s", e)
            try:
                self._mini.client.disconnect()
            except Exception as e:
                logger.warning("[reachy] client.disconnect failed: %s", e)
            self._mini = None

    @property
    def is_connected(self) -> bool:
        return self._mini is not None

    def dispatch(self, event_type: str, payload: Any) -> None:
        if event_type == P.SERVO_CMD_PLAY and payload:
            self._play_recording(str(payload))
        elif event_type == P.SERVO_CMD_MUSIC_START:
            self._start_music(str(payload) if payload else P.SERVO_MUSIC_GROOVE)
        elif event_type == P.SERVO_CMD_MUSIC_STOP:
            self._stop_music()
        else:
            logger.debug("[reachy] dispatch %s ignored", event_type)

    def _start_music(self, recording: str) -> None:
        """Groove until music_stop — the move repeats instead of playing once."""
        logger.info("[reachy] music groove '%s' — repeating until music_stop", recording)
        self._music_recording = recording
        self._music_playing = True
        self._play_recording(recording)

    def _stop_music(self) -> None:
        """Stop grooving immediately — cancel the in-flight repeat."""
        if self._music_playing:
            logger.info("[reachy] music groove stopped")
        self._music_playing = False
        self._music_recording = None
        self._cancel_move()

    def _next_groove(self, played: str, gen: int, failed: bool = False) -> Optional[str]:
        """Recording the play thread runs next, or None to end the thread."""
        if gen != self._play_gen:
            return None                       # a newer play owns the servo now
        if not self._music_playing:
            return None
        if self._ambient_blocked:
            logger.info("[reachy] groove interrupted — %s", self._ambient_block_reason)
            return None
        groove = self._music_recording
        if not groove:
            return None
        if failed and played == groove:
            logger.warning("[reachy] groove '%s' keeps failing — stopping the repeat", groove)
            return None
        return groove

    def get_available_recordings(self) -> List[str]:
        """Playable names, reported in HAL vocabulary wherever one exists."""
        libs = self._recorded_moves()
        if libs is None:
            return []
        hf_moves = set()
        for lib in libs:
            try:
                hf_moves.update(lib.list_moves())
            except Exception as e:
                logger.warning("[reachy] list_moves failed on %r: %s", lib, e)
        mapped = {hal for hal, hf in _MOVE_MAP.items() if hf in hf_moves}
        return sorted(mapped | (hf_moves - set(_MOVE_MAP.values())))

    def add_recording(self, name: str, actions: List[Dict[str, float]]) -> None:
        logger.warning("[reachy] add_recording('%s') not supported on this backend", name)

    def ensure_running(self) -> None:
        pass

    @property
    def is_suppressed(self) -> bool:
        """True when /servo/play must be refused — same rule as the feetech
        backend (zero or hold), plus a released (limp) robot."""
        return self._zero_mode or self._hold_mode or self._released

    @property
    def _tracking_active(self) -> bool:
        """True while anything owns the body — a flag holder or a live writer."""
        return self._tracking_flag or self._body_owners > 0

    @_tracking_active.setter
    def _tracking_active(self, value: bool) -> None:
        self._tracking_flag = bool(value)

    def acquire_body(self) -> None:
        with self._body_owner_lock:
            self._body_owners += 1

    def release_body(self) -> None:
        with self._body_owner_lock:
            self._body_owners = max(0, self._body_owners - 1)

    @property
    def motion_mode(self) -> Optional[str]:
        """`released` first: a limp robot is the one state whose answer is not
        "press Resume"."""
        if self._released:
            return "released"
        if self._zero_mode:
            return "zero"
        if self._hold_mode:
            return "hold"
        return None

    @property
    def _ambient_blocked(self) -> bool:
        """True when self-driven motion (the music groove) must not continue."""
        return self.is_suppressed or self._frozen

    @property
    def _ambient_block_reason(self) -> str:
        if self._released:
            return "released"
        if self._zero_mode:
            return "zero pose"
        if self._hold_mode:
            return "hold"
        return "frozen (camera)"

    def freeze(self) -> None:
        """Stop self-driven motion so a camera consumer can get a still frame."""
        self._frozen = True

    def unfreeze(self) -> None:
        was_frozen = self._frozen
        self._frozen = False
        if not was_frozen or not (self._music_playing and self._music_recording):
            return
        if self._play_thread is not None and self._play_thread.is_alive():
            return
        self._play_recording(self._music_recording)

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    def move_to(self, target_positions: Dict[str, float], duration: float = 2.0) -> None:
        gen = self._take_servo()
        merged = {**self._current_or_target(), **target_positions}
        eff = max(duration, _MIN_MOVE_DURATION_S)
        self._goto(merged, eff)
        self._release_servo(gen, eff)

    def move_and_hold(self, target_positions: Dict[str, float], duration: float = 2.0) -> None:
        self.move_to(target_positions, duration)

    def get_joint_names(self) -> Set[str]:
        return set(JOINT_KEYS)

    def get_positions(self) -> Dict[str, float]:
        with self._lock:
            if self._mini is None:
                return dict(self._target)
            try:
                pose = self._mini.get_current_head_pose()
                head_joints, ant = self._mini.get_current_joint_positions()
                return _joints_from_sdk(pose, ant, head_joints[0])
            except Exception as e:
                logger.warning("[reachy] get_positions read failed, using last target: %s", e)
                return dict(self._target)

    def send_positions(self, positions: Dict[str, float]) -> None:
        # No _release_servo: this is the tracker's streaming path, it keeps the
        # servo for as long as it is running rather than for one command.
        self._take_servo()
        merged = {**self._current_or_target(), **positions}
        head, antennas, body_yaw = self._compose(merged)
        self._call(
            "set_target",
            lambda m: m.set_target(head=head, antennas=antennas, body_yaw=body_yaw),
        )
        self._target = merged

    def zero_pose(self) -> None:
        self._zero_mode = True
        self._take_servo()
        self._goto({k: 0.0 for k in JOINT_KEYS}, 2.0)

    def halt(self) -> None:
        """Pin the head where it is, motors still enabled."""
        self._take_servo()
        try:
            here = self.get_positions()
            if here:
                self._goto(here, _MIN_MOVE_DURATION_S)
        except Exception as e:
            # Never raise — halt must always be answerable.
            logger.warning("[reachy] halt could not pin current pose: %s", e)
        logger.info("[reachy] motion halted, holding pose (motors enabled)")

    def release(self) -> Dict[str, str]:
        errors: Dict[str, str] = {}
        self._released = True
        self._take_servo()
        with self._lock:
            mini = self._require_mini()
            try:
                mini.goto_sleep()
            except Exception as e:
                errors["sleep"] = str(e)
            try:
                mini.disable_motors()
            except Exception as e:
                errors["torque"] = str(e)
        return errors

    def resume(self) -> None:
        self._zero_mode = False
        self._hold_mode = False
        self._hold_explicit = False
        with self._lock:
            mini = self._require_mini()
            try:
                mini.enable_motors()
            except Exception as e:
                logger.warning("[reachy] enable_motors failed: %s", e)
            try:
                mini.wake_up()
            except Exception as e:
                logger.warning("[reachy] wake_up failed: %s", e)
        self._released = False

    def hold(self, explicit: bool = False) -> None:
        self._hold_mode = True
        self._hold_explicit = explicit
        self._take_servo()

    def joint_status(self) -> Dict[str, dict]:
        with self._lock:
            head, antennas = self._require_mini().get_current_joint_positions()
        status: Dict[str, dict] = {}
        for i, (name, angle) in enumerate(zip(_HEAD_MOTOR_IDS, head)):
            status[name] = {
                "online": True, "id": i + 1, "angle": round(math.degrees(float(angle)), 1),
            }
        for i, (name, angle) in enumerate(zip(("right_antenna", "left_antenna"), antennas)):
            status[name] = {
                "online": True, "id": len(_HEAD_MOTOR_IDS) + i + 1,
                "angle": round(math.degrees(float(angle)), 1),
            }
        return status

    def aim(self, direction: str, duration: float, current_positions: Dict[str, float],
            safety_policy: Any) -> Dict[str, float]:
        from hal.safety.policy import min_move_duration

        target = _AIM_TARGETS.get(direction)
        if target is None:
            logger.warning("[reachy] unknown aim direction %r — defaulting to center", direction)
            target = _AIM_TARGETS[P.AIM_CENTER]
        positions = {**current_positions, **target}
        eff = max(
            min_move_duration(safety_policy, positions, current_positions, duration),
            _MIN_MOVE_DURATION_S,
        )
        gen = self._take_servo()
        self._goto({**self._current_or_target(), **positions}, eff)
        self._release_servo(gen, eff)
        return positions

    def nudge(self, yaw: float, pitch: float, duration: float,
              current_positions: Dict[str, float],
              safety_policy: Any) -> Dict[str, float]:
        from hal.safety.policy import min_move_duration

        positions = dict(current_positions)
        if yaw != 0:
            positions["head_yaw.pos"] = current_positions.get("head_yaw.pos", 0.0) + yaw
        if pitch != 0:
            positions["head_pitch.pos"] = current_positions.get("head_pitch.pos", 0.0) + pitch

        eff = max(
            min_move_duration(safety_policy, positions, current_positions, duration),
            _MIN_MOVE_DURATION_S,
        )
        gen = self._take_servo()
        self._goto({**self._current_or_target(), **positions}, eff)
        self._release_servo(gen, eff)
        return positions

    def _reconnect(self) -> None:
        """Drop the dead client and dial the daemon again (caller holds _lock)."""
        try:
            if self._mini is not None:
                self._mini.client.disconnect()
        except Exception:
            pass
        self._mini = ReachyMini(host=self._host, port=self._port, media_backend="no_media")
        # Torque again: whatever killed the connection (a daemon restart, an update)
        # dropped the motors back to their unpowered boot state too, so a reconnect
        # without this hands back a client that reports healthy and moves nothing.
        self._enable_motors()
        logger.info("[reachy] reconnected to daemon at %s:%s", self._host, self._port)

    def _call(self, what: str, fn):
        """Run a daemon call, reconnecting once if the connection went stale.

        Retries only on disconnect markers: a clamp rejection or a bad pose is a real
        error and must not be replayed against a fresh connection.
        """
        with self._lock:
            self._require_mini()
            try:
                return fn(self._mini)
            except Exception as e:
                if not _is_disconnect(e):
                    raise
                logger.warning("[reachy] %s: connection lost (%s) — reconnecting", what, e)
                self._reconnect()
                return fn(self._mini)

    def _require_mini(self) -> ReachyMini:
        if self._mini is None:
            raise RuntimeError("Reachy daemon not connected")
        return self._mini

    def _current_or_target(self) -> Dict[str, float]:
        """Best-effort current pose, falling back to the last commanded target."""
        try:
            return self.get_positions()
        except Exception:
            return dict(self._target)

    def _compose(self, positions: Dict[str, float]):
        """HAL joint dict (deg/mm) → SDK (4x4 head pose, antennas rad, body_yaw rad)."""
        head = create_head_pose(
            x=positions["head_x.pos"], y=positions["head_y.pos"], z=positions["head_z.pos"],
            roll=positions["head_roll.pos"], pitch=positions["head_pitch.pos"],
            yaw=positions["head_yaw.pos"],
            degrees=True, mm=True,
        )
        antennas = np.deg2rad(
            [positions["antenna_right.pos"], positions["antenna_left.pos"]]
        )
        body_yaw = math.radians(positions[_BODY_KEY])
        return head, antennas, body_yaw

    def _goto(self, positions: Dict[str, float], duration: float) -> None:
        head, antennas, body_yaw = self._compose(positions)
        self._call(
            "goto_target",
            lambda m: m.goto_target(
                head=head, antennas=antennas, body_yaw=body_yaw, duration=duration
            ),
        )
        self._target = dict(positions)

    def _take_servo(self) -> int:
        """Claim the servo for a direct pose command. Returns the new generation.

        The feetech backend never has two writers.
        """
        with self._lock:
            self._play_gen += 1
            gen = self._play_gen
        # The pose command owns the servo now, so no recording is running.
        self._current_recording = None
        if self._play_thread is not None and self._play_thread.is_alive():
            self._cancel_move()
        return gen

    def _release_servo(self, gen: int, delay: float) -> None:
        """Hand the servo back to the music groove once the pose command lands."""
        if not self._music_playing or not self._music_recording:
            return

        def _resume():
            if gen != self._play_gen:
                return                       # something else claimed the servo
            groove = self._music_recording
            if groove and self._music_playing and not self._ambient_blocked:
                self._play_recording(groove)

        timer = threading.Timer(max(delay, 0.0), _resume)
        timer.daemon = True
        timer.start()

    def _cancel_move(self) -> None:
        with self._lock:
            if self._mini is None:
                return
            try:
                self._mini.cancel_move()
            except Exception as e:
                logger.debug("[reachy] cancel_move: %s", e)

    def _recorded_moves(self):
        """Lazy-load the HF move libraries. Returns the loaders, or None if none.

        One failing dataset does not take the others down: a community dataset that was
        renamed or pulled from the Hub must not cost the robot its official emotions.
        """
        if self._moves is None:
            libs = []
            try:
                from reachy_mini.motion.recorded_move import RecordedMoves
            except Exception as e:
                logger.warning("[reachy] recorded moves unavailable: %s", e)
                self._moves = False
                return None
            for dataset in _move_datasets():
                try:
                    libs.append(RecordedMoves(dataset))
                except Exception as e:
                    logger.warning("[reachy] move library %r unavailable: %s", dataset, e)
            if not libs:
                logger.warning("[reachy] no move library loaded")
            self._moves = libs or False
        return self._moves or None

    def _find_move(self, hf_name: str) -> Optional[Any]:
        """First library holding `hf_name`, or None. Search order is load order."""
        for lib in self._recorded_moves() or []:
            try:
                return lib.get(hf_name)
            except Exception as e:
                logger.debug("[reachy] '%s' not in %r: %s", hf_name, lib, e)
        return None

    def _ramp_for(self, move: Any) -> float:
        """Seconds to interpolate into the move's first pose before playing it."""
        if self._safety_policy is None:
            return _PLAY_RAMP_S
        try:
            from hal.safety.policy import min_move_duration

            head, antennas, body_yaw = move.evaluate(0.0)
            target = _joints_from_sdk(head, antennas, body_yaw)
            return min_move_duration(
                self._safety_policy, target, self._current_or_target(), _PLAY_RAMP_S
            )
        except Exception as e:
            logger.debug("[reachy] ramp estimate failed (%s) — using %.2fs", e, _PLAY_RAMP_S)
            return _PLAY_RAMP_S

    def _move_refused(self, move: Any, hf_name: str) -> bool:
        """True when the move demands more head speed than SAFETY.md allows.

        The choice is play or do not play, and it has to be made before the trajectory
        is handed over (#286).
        """
        policy = self._safety_policy
        if policy is None or policy.motion is None or policy.motion.max_speed is None:
            return False
        peak = self._move_peak_dps.get(hf_name)
        if peak is None:
            try:
                peak = peak_head_dps(move.timestamps, move.trajectory)
            except Exception as e:
                logger.debug("[reachy] cannot scan '%s' (%s) — not gated", hf_name, e)
                return False
            self._move_peak_dps[hf_name] = peak
        ceiling = float(policy.motion.max_speed)
        if peak <= ceiling:
            logger.debug("[reachy] move '%s' peak %.0f deg/s — within %.0f", hf_name, peak, ceiling)
            return False
        logger.warning(
            "[reachy] move '%s' REFUSED — peak %.0f deg/s exceeds declared "
            "motion.max_speed %.0f deg/s",
            hf_name, peak, ceiling,
        )
        return True

    def _play_move_once(self, move: Any, name: str, gen: int) -> bool:
        """Play one pass of a recorded move (blocking). False if superseded.

        The generation is re-checked here, under the lock, so a thread that lost the
        servo while waiting never plays at all.
        """
        with self._play_lock:
            if gen != self._play_gen:
                logger.debug("[reachy] play '%s' superseded before start", name)
                return False
            # Report the recording only from the thread that actually owns the servo.
            self._current_recording = name
            with self._lock:
                mini = self._require_mini()
            ramp = self._ramp_for(move)
            try:
                mini.play_move(move, initial_goto_duration=ramp)
            except Exception as e:
                # Same stale-connection case as _call, but the retry cannot run
                # under _lock: a move blocks for its whole duration and would
                # stall every state read behind it.
                if not _is_disconnect(e):
                    raise
                logger.warning("[reachy] play '%s': connection lost — reconnecting", name)
                with self._lock:
                    self._reconnect()
                    mini = self._mini
                mini.play_move(move, initial_goto_duration=ramp)
        return True

    def _play_recording(self, name: str) -> None:
        # Only a limp robot refuses outright. A hold is the routes' call — the emotion
        # route decides which emotions survive one (routes/emotion.py reads
        # _hold_mode/_hold_explicit) and /servo/play is already gated on is_suppressed.
        if self._released:
            logger.info("[reachy] play '%s' refused — servos released", name)
            return
        with self._lock:
            self._play_gen += 1
            gen = self._play_gen
        if self._play_thread and self._play_thread.is_alive():
            self._cancel_move()

        def _run():
            if self._recorded_moves() is None:
                logger.warning("[reachy] play '%s' skipped — no move library", name)
                return
            current = name
            try:
                while True:
                    hf_name = _MOVE_MAP.get(current, current)
                    if hf_name != current:
                        logger.debug(
                            "[reachy] mapped recording '%s' → HF move '%s'", current, hf_name
                        )
                    failed = False
                    move = self._find_move(hf_name)
                    if move is None:
                        logger.warning(
                            "[reachy] move '%s' (HF: '%s') not in any library",
                            current, hf_name,
                        )
                        failed = True
                    elif self._move_refused(move, hf_name):
                        failed = True
                    else:
                        try:
                            # _play_move_once publishes _current_recording once
                            # it has confirmed this thread still owns the servo.
                            if not self._play_move_once(move, current, gen):
                                return   # a newer play owns the servo
                        except Exception as e:
                            logger.warning(
                                "[reachy] play '%s' (HF: '%s') failed: %s", current, hf_name, e
                            )
                            failed = True
                    # Repeat while music plays — the feetech backend does the same in
                    # _continue_playback.
                    next_rec = self._next_groove(current, gen, failed=failed)
                    if next_rec is None:
                        return
                    current = next_rec
            finally:
                # Only the current owner clears it; a stale thread finishing
                # late must not blank the recording a newer play just set.
                if gen == self._play_gen:
                    self._current_recording = None

        self._play_thread = threading.Thread(
            target=_run, daemon=True, name="reachy-play"
        )
        self._play_thread.start()
