import os
import csv
import time
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Set
from hal.follower import LeLampFollowerConfig, LeLampFollower
from hal.presets import EMO_SLEEPY, SERVO_CMD_PLAY, SERVO_CMD_MUSIC_START, SERVO_CMD_MUSIC_STOP, SERVO_IDLE, SERVO_MUSIC_GROOVE
from hal.drivers.motors.overload import OverloadGuard, load_magnitude
from hal.drivers.motors.tracking_wedge import TrackingWedgeWatchdog

logger = logging.getLogger(__name__)

DEFAULT_MOVE_DURATION = 2.0

ZERO_RAW = {
    "base_yaw":    2025,
    "base_pitch":  2674,
    "elbow_pitch": 2636,
    "wrist_roll":  2054,
    "wrist_pitch": 2056,
}

# Wake position in raw encoder units — all 5 joints. Where the arm goes once
# on connect, as the first queued event, before idle starts animating.
# Not used by resume(): that path re-enables torque and goes straight to idle.
STARTUP_RAW = {
    "base_yaw":    2025,
    "base_pitch":  2674,
    "elbow_pitch": 2636,
    "wrist_roll":  2054,
    "wrist_pitch": 2056,
}

# Gravity-rest position in raw encoder units — where release() parks the arm before
# cutting torque, so it settles instead of dropping.
REST_RAW = {
    "base_yaw":    2029,
    "base_pitch":  2030,
    "elbow_pitch": 2030,
    "wrist_roll":  2053,
    "wrist_pitch": 1770,
}

STARTUP_MOVE_DURATION = 5.0

# How often the overload cut-off samples Present_Load.
OVERLOAD_POLL_S = 0.1

from hal.drivers.motors.recording_timing import (  # noqa: E402
    RECORDING_TIME_COLUMN,
    SERVO_MAX_DPS,
    resample_recording,
)

# Internal event: wake move + state sync, queued by start() so the 5s
# interpolation runs in the event thread instead of blocking the caller
# (server lifespan Phase 3 joins the servo init thread).
SERVO_CMD_STARTUP_MOVE = "__startup_move__"

NO_IDLE_RECORDINGS = {EMO_SLEEPY}


def _motor_positions_from_bus(robot: LeLampFollower) -> Dict[str, float]:
    """Read Present_Position only — same numeric scale as CSV, no camera/LED path."""
    t0 = time.perf_counter()
    raw = robot.bus.sync_read("Present_Position")
    dt = time.perf_counter() - t0
    if dt > 0.75:
        logger.warning("slow sync_read Present_Position: %.2fs (serial/USB may be stalling)", dt)
    return {f"{motor}.pos": float(val) for motor, val in raw.items()}


class AnimationService:
    def __init__(self, port: str, lamp_id: str, fps: int = 30, duration: float = 5.0, idle_recording: str = SERVO_IDLE, hold_s: float = 0.0, safety_policy=None, geometry=None,
                 overload_guard: Optional[OverloadGuard] = None,
                 on_overload: Optional[Callable[[str, int], None]] = None):
        self.port = port
        self.lamp_id = lamp_id
        self.fps = fps
        self.duration = duration
        self.idle_recording = idle_recording
        self.hold_s = hold_s
        # SAFETY.md motion.max_speed, applied to recording playback at load
        # time (see _load_recording). aim/nudge take theirs per call from the
        # route; playback has no route to carry it, so the service holds it.
        self._safety_policy = safety_policy
        self._geometry = geometry
        self._hold_until: float = 0.0
        self._no_idle_recordings = NO_IDLE_RECORDINGS
        # disable_torque_on_disconnect=False: dropping torque is what `release()` does,
        # deliberately and on request ("arm limp").
        self.robot_config = LeLampFollowerConfig(
            port=port, id=lamp_id, disable_torque_on_disconnect=False
        )
        self.robot: LeLampFollower = None
        self.recordings_dir = os.path.join(os.path.dirname(__file__), "..", "..", "recordings")

        self._recording_cache: Dict[str, List[Dict[str, float]]] = {}
        self._current_state: Optional[Dict[str, float]] = None
        self._current_recording: Optional[str] = None
        self._current_frame_index: int = 0
        self._current_actions: List[Dict[str, float]] = []
        self._interpolation_frames: int = 0
        self._interpolation_total_frames: int = 0  # denominator for progress; must match _interpolation_frames initial value
        self._interpolation_target: Optional[Dict[str, float]] = None

        self._music_playing = False
        self._music_recording = SERVO_MUSIC_GROOVE

        # Deterministic stop. Set by halt(), checked every frame by the move and
        # playback loops; they return where they are, leaving the last goal written and
        # torque ON.
        self._halt = threading.Event()

        self._running = threading.Event()
        self._event_queue = []
        self._event_lock = threading.Lock()
        self._event_thread: Optional[threading.Thread] = None

        # Serial bus lock — all bus access (read/write/ping) must hold this lock
        self.bus_lock = threading.RLock()

        self._resume_duration: Optional[float] = None

        # Freeze flag — when set, _continue_playback() skips servo writes so camera can capture a stable frame
        self._frozen = threading.Event()

        self._raw_write_monotonic = 0.0

        # Hold mode — suppresses idle/ambient animations but allows emotion dispatch.
        # Set by /servo/hold, cleared by /servo/resume.
        self._hold_mode = False
        # True only when the hold came from an EXPLICIT /servo/hold (agent command like
        # "face the wall and stay there").
        self._hold_explicit = False

        # Tracking lock — stricter than hold_mode: absolutely no servo writes from the
        # animation loop, and in-progress recordings are dropped so they don't fight the
        # tracker or resume jerking when tracking ends.
        self._tracking_flag = False
        self._body_owners = 0
        self._body_owner_lock = threading.Lock()

        # Backstop: the flag half of the lock has exactly one legitimate holder (a live
        # tracking session, whose follower also holds a counter slot), so
        # flag-set-with-no-writer is an impossible state.
        self._tracking_wedge = TrackingWedgeWatchdog()

        self._idle_settled = False

        # Overload cut-off (gear protection); None leaves it off. on_overload(joint,
        # load) runs after the torque is cut, for what this service cannot reach
        # (tracker, chime).
        self._overload = overload_guard
        self._on_overload = on_overload
        self._overload_stop = threading.Event()
        self._overload_thread: Optional[threading.Thread] = None
        self._overload_read_ok = True

    @property
    def _tracking_active(self) -> bool:
        """True while anything owns the body — a flag holder or a live writer."""
        return self._tracking_flag or self._body_owners > 0

    @_tracking_active.setter
    def _tracking_active(self, value: bool) -> None:
        self._tracking_flag = bool(value)

    _GOAL_SPEED_REG = 46
    # Unwritten servos read 0 but run ~16 deg/s; writing 0 lifts the cap entirely, so
    # restoring needs ~175 (0.062 deg/s per unit, device-measured).
    @property
    def UNWRITTEN_SPEED_EQUIVALENT(self) -> int:
        """What base_yaw rests at — the same value startup writes. 0 = no cap."""
        return self._SERVO_REST_SPEED.get(1, 0)

    def set_joint_speed(self, motor_name: str, speed: int) -> bool:
        """Cap one joint's velocity, or lift the cap with 0. Never raises."""
        try:
            with self.bus_lock:
                motor = self.robot.bus.motors.get(motor_name)
                if motor is None:
                    return False
                self.robot.bus.packet_handler.write2ByteTxRx(
                    self.robot.bus.port_handler, motor.id,
                    self._GOAL_SPEED_REG, int(speed),
                )
            return True
        except Exception as e:
            logger.warning("could not set %s speed to %s: %s", motor_name, speed, e)
            return False

    def acquire_body(self) -> None:
        """Claim the body for as long as the caller keeps writing to it.

        Re-entrant by count, so nested or overlapping owners each release once.
        """
        with self._body_owner_lock:
            self._body_owners += 1

    def release_body(self) -> None:
        with self._body_owner_lock:
            self._body_owners = max(0, self._body_owners - 1)

    # P gain — upstream default is 16 for all. It carries the most gravity torque of any
    # joint (the whole forearm plus head), and with P=16 and no integral term the
    # commanded torque.
    _SERVO_PGAIN = {1: 16, 2: 16, 3: 32, 4: 16, 5: 32}

    # I gain — 0 everywhere by default, which is what the servos ship with.
    _SERVO_IGAIN = {3: 10, 5: 10}

    # Resting Goal_Speed, written at startup so a joint the search retunes always starts
    # from a known value.
    _SERVO_REST_SPEED = {1: 0}

    def _configure_servos_raw(self, energize: bool = True):
        """Configure servos directly via scservo_sdk, bypassing lerobot."""
        locked_out = energize and self.overload_active
        if locked_out:
            energize = False
        with self.bus_lock:
            ph = self.robot.bus.port_handler
            pk = self.robot.bus.packet_handler
            from scservo_sdk import COMM_SUCCESS
            for motor_name, motor_obj in self.robot.bus.motors.items():
                sid = motor_obj.id
                pgain = self._SERVO_PGAIN.get(sid, 32)
                igain = self._SERVO_IGAIN.get(sid, 0)
                rest_speed = self._SERVO_REST_SPEED.get(sid)
                _, result, _ = pk.ping(ph, sid)
                if result != COMM_SUCCESS:
                    logger.warning(f"{motor_name} (ID {sid}): offline, skipping")
                    continue
                pk.write1ByteTxRx(ph, sid, 40, 0)   # Torque_Enable = 0
                pk.write1ByteTxRx(ph, sid, 33, 0)
                pk.write1ByteTxRx(ph, sid, 21, pgain)
                pk.write1ByteTxRx(ph, sid, 23, igain)
                pk.write1ByteTxRx(ph, sid, 22, 32)
                # See _SERVO_REST_SPEED: clears a cap a killed sweep left behind.
                if rest_speed is not None:
                    pk.write2ByteTxRx(ph, sid, self._GOAL_SPEED_REG, rest_speed)
                if energize:
                    pk.write1ByteTxRx(ph, sid, 40, 1)   # Torque_Enable = 1
                logger.info(
                    f"{motor_name} (ID {sid}): P={pgain}, I={igain}"
                    + (f", speed={rest_speed}" if rest_speed is not None else "")
                    + f", torque {'ON' if energize else 'OFF (overload cut-off)' if locked_out else 'OFF (asleep)'}"
                )

    def start(self, skip_wake: bool = False):
        """skip_wake: come up without the startup pose + idle loop."""
        self.robot = LeLampFollower(self.robot_config)
        try:
            self.robot.connect(calibrate=False)
        except Exception as e:
            logger.warning(f"Robot connect (partial): {e}")

        # Configure servos directly — works even if connect() partially failed
        try:
            self._configure_servos_raw(energize=not skip_wake)
        except Exception as e:
            logger.warning(f"Raw configure failed: {e}")

        logger.info(f"Animation service connected to {self.port}")

        # Start the event thread first, then queue wake move + idle.
        self._running.set()
        self._event_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._event_thread.start()
        if self._overload is not None and self._overload.enabled:
            self._overload_stop.clear()
            self._overload_thread = threading.Thread(
                target=self._overload_loop, daemon=True, name="servo-overload",
            )
            self._overload_thread.start()
            logger.info(
                "Overload cut-off armed: load >= %.1f%% for %.1fs cuts torque, retry after %.0fs",
                self._overload.threshold / 10.0, self._overload.hold_s, self._overload.retry_s,
            )
        if skip_wake:
            logger.info("Servo startup move + idle skipped -- device was asleep")
            return
        self.dispatch(SERVO_CMD_STARTUP_MOVE, None)

        self.dispatch(SERVO_CMD_PLAY, self.idle_recording)

    def stop(self, timeout: float = 5.0):
        self._overload_stop.set()
        if self._overload_thread and self._overload_thread.is_alive():
            self._overload_thread.join(timeout=timeout)
        self._running.clear()
        if self._event_thread and self._event_thread.is_alive():
            self._event_thread.join(timeout=timeout)

        if self.robot:
            self.robot.disconnect()
            self.robot = None

    def _sync_state_from_hardware(self) -> None:
        """Set _current_state from Present_Position for all joints."""
        if not self.robot:
            return
        try:
            with self.bus_lock:
                pos = _motor_positions_from_bus(self.robot)
            if pos:
                self._current_state = pos
        except Exception as e:
            logger.warning(f"sync state from hardware failed: {e}")

    def dispatch(self, event_type: str, payload: Any):
        """Dispatch an event - same interface as ServiceBase"""
        if not self._running.is_set():
            print(f"Animation service is not running, ignoring event {event_type}")
            return

        with self._event_lock:
            self._event_queue.append((event_type, payload))

    def _event_loop(self):
        """Custom event loop that supports interruption."""
        while self._running.is_set():
            with self._event_lock:
                if self._event_queue:
                    event_type, payload = self._event_queue.pop(0)
                else:
                    event_type, payload = None, None

            if event_type:
                self._idle_settled = False
                try:
                    self.handle_event(event_type, payload)
                except Exception as e:
                    print(f"Error handling event {event_type}: {e}")

            held = self._tracking_wedge.check(self._tracking_flag, self._body_owners)
            if held is not None:
                logger.error(
                    "[tracking] flag held %.0fs with no owner — clearing wedged lock "
                    "(emotion servo and gaze were suppressed for the whole of it)",
                    held,
                )
                self._tracking_flag = False

            self._continue_playback()

            time.sleep(1.0 / self.fps)

    def _handle_startup_move(self):
        """Wake move to startup pose + hardware state sync.

        _sync_state_from_hardware must run after the move: the following idle
        interpolation starts from _current_state, and missing/stale joints treated as 0°
        cause violent corrections and servo stall.
        """
        # Move all joints to wake/startup position (includes wrist_pitch outside calib
        # range).
        try:
            self.move_to_raw(
                STARTUP_RAW,
                duration=STARTUP_MOVE_DURATION,
                should_abort=lambda: not self._running.is_set(),
            )
            logger.info("Servos moved to startup position")
        except Exception as e:
            logger.warning(f"Failed to move to startup position: {e}")
        self._sync_state_from_hardware()

    def handle_event(self, event_type: str, payload: Any):
        if event_type == SERVO_CMD_STARTUP_MOVE:
            self._handle_startup_move()
        elif event_type == SERVO_CMD_PLAY:
            self._handle_play(payload)
        elif event_type == SERVO_CMD_MUSIC_START:
            self._handle_music_start(payload)
        elif event_type == SERVO_CMD_MUSIC_STOP:
            self._handle_music_stop()
        else:
            print(f"Unknown event type: {event_type}")

    def _handle_music_start(self, recording_name: Optional[str] = None):
        """Start grooving to music -- loops recording until music stops."""
        self._music_recording = recording_name if recording_name else SERVO_MUSIC_GROOVE
        self._music_playing = True
        self._handle_play(self._music_recording)

    def _handle_music_stop(self):
        """Stop music groove — interrupt immediately and return to idle."""
        was_playing = self._music_playing
        self._music_playing = False
        if not was_playing:
            return
        self._hold_until = 0.0
        self._handle_play(self.idle_recording)

    def _handle_play(self, recording_name: str):
        """Start playing a recording with interpolation from current state"""
        self._begin_motion()
        self._idle_settled = False
        self._holding_logged = False
        self._hold_logged = False
        if not self.robot:
            print("Robot not connected")
            return

        actions = self._load_recording(recording_name)
        if actions is None:
            return

        print(f"Starting {recording_name} with interpolation")

        self._current_recording = recording_name
        self._current_actions = actions
        self._current_frame_index = 0

        if self._current_state is not None:
            effective_duration = self._resume_duration if self._resume_duration is not None else self.duration
            self._resume_duration = None
            total = int(effective_duration * self.fps)
            self._interpolation_frames = total
            self._interpolation_total_frames = total
            self._interpolation_target = actions[0]
        else:
            self._interpolation_frames = 0
            self._interpolation_target = None

    def freeze(self):
        """Pause servo writes so camera can capture a stable frame."""
        self._frozen.set()

    def unfreeze(self):
        """Resume servo writes after camera capture."""
        self._frozen.clear()

    @property
    def is_frozen(self) -> bool:
        """True while a camera consumer wants the servos still."""
        return self._frozen.is_set()

    _QUIET_RECORDINGS: frozenset = frozenset(
        r.strip()
        for r in os.environ.get("HAL_QUIET_RECORDINGS", "").split(",")
        if r.strip()
    )

    @property
    def is_actively_moving(self) -> bool:
        """True while the arm makes AUDIBLE movement."""
        if self._tracking_active:
            return True
        rec = self._current_recording
        if rec is None or self._idle_settled:
            return False
        return rec not in self._QUIET_RECORDINGS

    @property
    def last_servo_write(self) -> float:
        """Monotonic timestamp of the last servo motion command, across ALL write paths:
        robot.send_action (animation loop, tracker worker, move_to, motors_service) and
        move_to_raw (direct register writes).
        """
        via_action = getattr(self.robot, "last_write_monotonic", 0.0) if self.robot else 0.0
        return max(via_action, self._raw_write_monotonic)

    def _continue_playback(self):
        """Continue current playback - called every frame"""
        if not self._current_recording or not self._current_actions:
            return

        # Skip servo writes while frozen (camera stabilization)
        if self._frozen.is_set():
            return

        # Halt: drop the recording where it is and stop writing. No goal is written, so
        # the servos hold the last frame that landed.
        if self._halt.is_set():
            logger.info("[halt] dropped recording %r mid-playback", self._current_recording)
            self._idle_settled = True
            self._current_recording = None
            self._current_actions = []
            self._current_frame_index = 0
            self._interpolation_frames = 0
            self._interpolation_target = None
            return

        # Tracking lock: tracker owns the servo.
        if self._tracking_active:
            if self._current_recording is not None:
                logger.info(
                    "[tracking] dropped recording %r mid-playback (flag=%s owners=%d)",
                    self._current_recording, self._tracking_flag, self._body_owners,
                )
            self._idle_settled = True
            self._current_recording = None
            self._current_actions = []
            self._current_frame_index = 0
            self._interpolation_frames = 0
            self._interpolation_target = None
            return

        try:
            if self._interpolation_frames > 0 and self._interpolation_target is not None:
                denom = self._interpolation_total_frames if self._interpolation_total_frames > 0 else int(self.duration * self.fps)
                progress = 1.0 - (self._interpolation_frames / denom)
                progress = max(0.0, min(1.0, progress))

                target = self._interpolation_target
                interpolated_action = {}
                for joint in target.keys():
                    # Default 0 is unsafe if _current_state is incomplete (see _sync_state_from_hardware).
                    current_val = self._current_state.get(joint) if self._current_state else None
                    if current_val is None:
                        logger.warning(
                            "interpolation: joint %s missing from _current_state, using 0 (risk of jam)",
                            joint,
                        )
                        current_val = 0.0
                    target_val = target[joint]
                    interpolated_action[joint] = current_val + (target_val - current_val) * progress

                with self.bus_lock:
                    self.robot.send_action(interpolated_action)
                self._current_state = interpolated_action.copy()
                self._interpolation_frames -= 1
                return

            if self._current_frame_index < len(self._current_actions):
                action = self._current_actions[self._current_frame_index]
                with self.bus_lock:
                    self.robot.send_action(action)
                self._current_state = action.copy()
                self._current_frame_index += 1
            else:
                if self._music_playing and self._current_recording == self._music_recording:
                    self._current_frame_index = 0
                elif self._current_recording in self._no_idle_recordings:
                    if not getattr(self, '_holding_logged', False):
                        logger.info("Holding final pose for '%s' — no idle fallback", self._current_recording)
                        self._holding_logged = True
                    return
                elif self._hold_mode and not self._music_playing:
                    # Hold mode: keep final pose, do not return to idle
                    self._idle_settled = True
                    if not getattr(self, '_hold_logged', False):
                        logger.info("Hold mode: keeping final pose for '%s'", self._current_recording)
                        self._hold_logged = True
                    return
                elif self._current_recording != self.idle_recording:
                    if not self._music_playing:
                        if self.hold_s > 0 and self._hold_until == 0.0:
                            self._hold_until = time.time() + self.hold_s
                            return
                        if self._hold_until > 0.0:
                            if time.time() < self._hold_until:
                                return
                            self._hold_until = 0.0
                    else:
                        self._hold_until = 0.0
                    if self._music_playing:
                        next_rec = self._music_recording
                    else:
                        next_rec = self.idle_recording
                    next_actions = self._load_recording(next_rec)
                    if next_actions is not None and len(next_actions) > 0:
                        self._current_recording = next_rec
                        self._current_actions = next_actions
                        self._current_frame_index = 0
                        if self._current_state is not None:
                            total = int(self.duration * self.fps)
                            self._interpolation_frames = total
                            self._interpolation_total_frames = total
                            self._interpolation_target = next_actions[0]
                elif self._hold_mode:
                    self._idle_settled = True
                    return
                else:
                    self._idle_settled = True
                    self._current_frame_index = 0

        except Exception as e:
            logger.exception("playback error: %s", e)
            self._current_recording = None
            self._current_actions = []
            self._current_frame_index = 0

    def get_available_recordings(self) -> List[str]:
        """Get list of recording names available for this lamp ID"""
        if not os.path.exists(self.recordings_dir):
            return []

        recordings = []
        suffix = f".csv"

        for filename in os.listdir(self.recordings_dir):
            if filename.endswith(suffix):
                recording_name = filename[:-len(suffix)]
                recordings.append(recording_name)

        return sorted(recordings)

    def _load_recording(self, recording_name: str) -> Optional[List[Dict[str, float]]]:
        """Load a recording from cache or file, resampled for playback."""
        if recording_name in self._recording_cache:
            return self._recording_cache[recording_name]

        csv_filename = f"{recording_name}.csv"
        csv_path = os.path.join(self.recordings_dir, csv_filename)

        if not os.path.exists(csv_path):
            logger.warning(f"Recording not found: {csv_path}")
            return None

        try:
            with open(csv_path, 'r') as csvfile:
                csv_reader = csv.DictReader(csvfile)
                actions = []
                times = []
                for row in csv_reader:
                    action = {key: float(value) for key, value in row.items() if key != RECORDING_TIME_COLUMN}
                    actions.append(action)
                    raw_t = row.get(RECORDING_TIME_COLUMN)
                    times.append(float(raw_t) if raw_t not in (None, "") else None)

            if len(actions) < 2 or any(t is None for t in times):
                if actions and any(t is None for t in times):
                    logger.warning(
                        "recording %r has no %s column — playing frames unresampled",
                        recording_name, RECORDING_TIME_COLUMN,
                    )
                self._recording_cache[recording_name] = actions
                return actions

            # Cached per name, which is safe: the policy is read once at boot
            # and never changes for the life of the process.
            actions = resample_recording(
                times, actions, recording_name, self.fps, self._safety_policy,
                self._geometry,
            )

            self._recording_cache[recording_name] = actions
            return actions

        except Exception as e:
            logger.error(f"Error loading recording {recording_name}: {e}")
            return None

    def _motion_aborted(self) -> bool:
        """True when an in-flight move or playback must stop THIS frame."""
        return self._halt.is_set() or not self._running.is_set()

    def _begin_motion(self) -> None:
        """Clear a previous halt so a newly commanded motion can run.

        Called by the commanded-motion entry points, never by the loops themselves: a
        halt has to outlive the move it interrupted, or the very next frame would clear
        it.
        """
        self._halt.clear()

    def halt(self) -> None:
        """Abort any move/recording in flight and hold position. Torque stays ON."""
        self._halt.set()
        # Pin the servos where they are.
        try:
            with self.bus_lock:
                current = _motor_positions_from_bus(self.robot) if self.robot else {}
            if current:
                with self.bus_lock:
                    self.robot.send_action(current)
        except Exception as e:
            # Never raise: halt is the one call that must always be answerable.
            logger.warning("[halt] could not pin current position: %s", e)
        logger.info("[halt] motion halted, holding position (torque ON)")

    def move_to(
        self,
        target_positions: Dict[str, float],
        duration: float = DEFAULT_MOVE_DURATION,
        should_abort: Optional[Callable[[], bool]] = None,
    ):
        """Smoothly move servos to target positions using software interpolation."""
        if not self.robot:
            raise RuntimeError("Robot not connected")
        self._refuse_if_overloaded()
        self._begin_motion()

        try:
            with self.bus_lock:
                current = _motor_positions_from_bus(self.robot)
            if not current:
                raise ValueError("empty Present_Position read")
        except Exception:
            if self._current_state:
                current = self._current_state.copy()
            else:
                with self.bus_lock:
                    self.robot.send_action(target_positions)
                return

        total_frames = max(1, int(duration * self.fps))
        abort = should_abort or self._motion_aborted

        for frame in range(1, total_frames + 1):
            if abort():
                logger.info("move_to aborted at frame %d/%d — holding position", frame, total_frames)
                return
            t0 = time.perf_counter()
            progress = frame / total_frames

            interpolated = {}
            for joint, target_val in target_positions.items():
                cur_val = current.get(joint, target_val)
                interpolated[joint] = cur_val + (target_val - cur_val) * progress

            try:
                with self.bus_lock:
                    self.robot.send_action(interpolated)
            except Exception as e:
                logger.warning(f"Interpolated move frame {frame} failed: {e}")
                break

            dt = time.perf_counter() - t0
            sleep_time = (1.0 / self.fps) - dt
            if sleep_time > 0:
                time.sleep(sleep_time)

        try:
            with self.bus_lock:
                self.robot.send_action(target_positions)
        except Exception:
            pass

        # Prefer full pose from hardware so other joints are not left stale
        try:
            with self.bus_lock:
                pos = _motor_positions_from_bus(self.robot)
            if pos:
                self._current_state = pos
                return
        except Exception as e:
            logger.warning(f"move_to: could not read full state after move: {e}")
        self._current_state = target_positions.copy()

    def move_and_hold(self, target_positions: Dict[str, float], duration: float = DEFAULT_MOVE_DURATION):
        """Take over the servo for an explicit /servo/move or /servo/nudge."""
        self._refuse_if_overloaded()
        # Preempt: drop any recording the event loop is playing so it stops sending its
        # frames.
        if self._current_recording is not None:
            logger.info(
                "[preempt] dropped recording %r for a direct move",
                self._current_recording,
            )
        self._current_recording = None
        self._current_actions = []
        self._current_frame_index = 0
        self._interpolation_frames = 0
        self._interpolation_target = None
        self._idle_settled = True

        if duration > 0:
            self.move_to(target_positions, duration=duration)
        else:
            with self.bus_lock:
                self.robot.send_action(target_positions)
            try:
                with self.bus_lock:
                    pos = _motor_positions_from_bus(self.robot)
                self._current_state = pos if pos else dict(target_positions)
            except Exception:
                self._current_state = dict(target_positions)

    def move_to_raw(
        self,
        target_raw: Dict[str, int],
        duration: float = DEFAULT_MOVE_DURATION,
        should_abort: Optional[Callable[[], bool]] = None,
    ):
        """Smoothly move servos to raw encoder positions via direct STS3215 register writes.

        The release/park path passes None — parking must always finish.
        """
        if not self.robot:
            raise RuntimeError("Robot not connected")
        if self.overload_active:
            # Startup, zero and park moves are skipped, not failed: the arm is already limp.
            logger.info("move_to_raw skipped — overload cut-off active")
            return

        GOAL_POSITION_REG = 42
        PRESENT_POSITION_REG = 56

        ph = self.robot.bus.port_handler
        pk = self.robot.bus.packet_handler

        current_raw: Dict[str, int] = {}
        with self.bus_lock:
            for motor_name, motor_obj in self.robot.bus.motors.items():
                data, result, _ = pk.read2ByteTxRx(ph, motor_obj.id, PRESENT_POSITION_REG)
                current_raw[motor_name] = data if result == 0 else target_raw.get(motor_name, 2048)

        total_frames = max(1, int(duration * self.fps))

        for frame in range(1, total_frames + 1):
            if should_abort and should_abort():
                logger.info("move_to_raw aborted at frame %d/%d", frame, total_frames)
                return
            t0 = time.perf_counter()
            progress = frame / total_frames

            with self.bus_lock:
                for motor_name, target in target_raw.items():
                    cur = current_raw.get(motor_name, target)
                    raw = max(0, min(4095, int(cur + (target - cur) * progress)))
                    motor_obj = self.robot.bus.motors.get(motor_name)
                    if motor_obj:
                        pk.write2ByteTxRx(ph, motor_obj.id, GOAL_POSITION_REG, raw)
            self._raw_write_monotonic = time.monotonic()

            dt = time.perf_counter() - t0
            sleep_time = (1.0 / self.fps) - dt
            if sleep_time > 0:
                time.sleep(sleep_time)

        with self.bus_lock:
            for motor_name, raw in target_raw.items():
                motor_obj = self.robot.bus.motors.get(motor_name)
                if motor_obj:
                    pk.write2ByteTxRx(ph, motor_obj.id, GOAL_POSITION_REG, raw)
        self._raw_write_monotonic = time.monotonic()

        try:
            with self.bus_lock:
                pos = _motor_positions_from_bus(self.robot)
            if pos:
                self._current_state = pos
        except Exception as e:
            logger.warning("move_to_raw: could not read state after move: %s", e)

    @property
    def is_connected(self) -> bool:
        return self.robot is not None and getattr(self.robot, "is_connected", False)

    def get_joint_names(self) -> Set[str]:
        """Valid joint keys, e.g. {"base_yaw.pos", "base_pitch.pos", ...}."""
        if not self.robot or not self.robot.bus or not self.robot.bus.motors:
            return set()
        return {f"{m}.pos" for m in self.robot.bus.motors}

    def get_positions(self) -> Dict[str, float]:
        """Read current positions from hardware (bus-only, no camera)."""
        if not self.robot:
            raise RuntimeError("Robot not connected")
        with self.bus_lock:
            obs = self.robot.get_observation()
        return {k: v for k, v in obs.items() if k.endswith(".pos")}

    def send_positions(self, positions: Dict[str, float]) -> None:
        """Write joint positions directly (one-shot, no interpolation)."""
        if not self.robot:
            raise RuntimeError("Robot not connected")
        self._refuse_if_overloaded()
        with self.bus_lock:
            self.robot.send_action(positions)

    @property
    def is_suppressed(self) -> bool:
        """True when zero_pose or explicit hold is active."""
        return getattr(self, "_zero_mode", False) or self._hold_mode

    @property
    def motion_mode(self) -> Optional[str]:
        """Zero wins over hold: zero_pose() parks the body, hold() only freezes it. A
        released body also reports None — release() cuts torque without setting either
        flag.
        """
        if getattr(self, "_zero_mode", False):
            return "zero"
        if self._hold_mode:
            return "hold"
        return None

    def ensure_running(self) -> None:
        """Restart the event loop if it stopped (e.g. after zero/hold)."""
        if not self._running.is_set():
            self._running.set()
            self._event_thread = threading.Thread(
                target=self._event_loop, daemon=True,
            )
            self._event_thread.start()
            logger.info("Animation event loop restarted")

    def add_recording(self, name: str, actions: List[Dict[str, float]]) -> None:
        """Invalidate the cache for a recording (used after upload).

        `actions` arrives stripped of its timestamp column, so caching it here would
        store frames that never went through resample_recording.
        """
        self._recording_cache.pop(name, None)

    def hold(self, explicit: bool = False) -> None:
        """Suppress idle/ambient animations, torque stays ON."""
        self._hold_mode = True
        if explicit:
            self._hold_explicit = True
        logger.info(
            "Hold mode activated (explicit=%s) — idle suppressed%s",
            explicit, ", emotion servo fully suppressed" if explicit else "",
        )

    def zero_pose(self) -> None:
        """Move to zero/park pose and hold (torque ON). Stops event loop."""
        self._zero_mode = True
        self._running.clear()
        if self._event_thread and self._event_thread.is_alive():
            self._event_thread.join(timeout=3.0)
        try:
            self._configure_servos_raw()
        except Exception as e:
            logger.warning("zero: raw configure failed: %s", e)
        try:
            self.move_to_raw(ZERO_RAW, duration=2.0)
        except Exception as e:
            logger.warning("Could not move to zero: %s", e)
        self._sync_state_from_hardware()

    def release(self) -> Dict[str, str]:
        """Move to gravity-rest, then disable torque. Returns per-motor errors."""
        self._running.clear()
        if self._event_thread and self._event_thread.is_alive():
            self._event_thread.join(timeout=3.0)
        try:
            self.move_to_raw(REST_RAW, duration=2.0)
        except Exception as e:
            logger.warning("Could not move to rest before release: %s", e)
        time.sleep(0.4)
        errors: Dict[str, str] = {}
        if not self.robot or not self.robot.bus:
            return errors
        bus = self.robot.bus
        with self.bus_lock:
            for motor_name in bus.motors:
                try:
                    bus.write("Torque_Enable", motor_name, 0)
                except Exception as e:
                    errors[motor_name] = str(e)
        if errors:
            logger.warning("Servo release errors (offline?): %s", errors)
        else:
            logger.info("release: torque disabled on all servos (arm limp)")
        return errors

    def resume(self) -> None:
        """Exit zero/hold, re-enable torque, restart idle animation."""
        self._zero_mode = False
        self._hold_mode = False
        self._hold_explicit = False
        self._running.clear()
        if self._event_thread and self._event_thread.is_alive():
            self._event_thread.join(timeout=3.0)
        try:
            self._configure_servos_raw()
        except Exception as e:
            logger.warning("resume: raw configure failed: %s", e)
        self._sync_state_from_hardware()
        self._resume_duration = self.duration
        self._running.set()
        self.dispatch(SERVO_CMD_PLAY, self.idle_recording)
        self._event_thread = threading.Thread(
            target=self._event_loop, daemon=True,
        )
        self._event_thread.start()
        logger.info("Servo resumed from zero-hold mode")

    # --- Overload cut-off (gear protection) --------------------------------------

    @property
    def overload_active(self) -> bool:
        """True while the overload cut-off holds the servos limp."""
        return self._overload is not None and self._overload.locked

    def _refuse_if_overloaded(self) -> None:
        if self.overload_active:
            raise RuntimeError(
                "Servo overload cut-off active; retrying in %.0fs" % self._overload.retry_in_s()
            )

    def overload_status(self) -> Optional[Dict[str, Any]]:
        """Cut-off state for GET /health; None when the feature is off."""
        guard = self._overload
        if guard is None or not guard.enabled:
            return None
        last = guard.last_trip
        return {
            "active": guard.locked,
            "retry_in_s": round(guard.retry_in_s(), 1),
            "threshold": guard.threshold,
            "hold_s": guard.hold_s,
            "retry_s": guard.retry_s,
            "trips": guard.trips,
            "last_trip": {"joint": last[0], "load": last[1]} if last else None,
            "load": dict(guard.load),
            "peak": dict(guard.peak),
        }

    def _overload_loop(self):
        while not self._overload_stop.wait(OVERLOAD_POLL_S):
            try:
                self._overload_tick()
            except Exception as e:
                logger.warning("[overload] monitor tick failed: %s", e)

    def _overload_tick(self) -> None:
        """One pass: end a finished lockout, otherwise sample load and cut if it stalled."""
        guard = self._overload
        if guard.locked:
            if guard.retry_due():
                self._overload_recover()
            return
        tripped = guard.observe(self._read_loads())
        if tripped:
            self._overload_cut(*tripped)

    def _read_loads(self) -> Optional[Dict[str, int]]:
        """Per-joint Present_Load magnitude (0..1000), or None when the bus read fails."""
        try:
            with self.bus_lock:
                raw = self.robot.bus.sync_read("Present_Load", normalize=False)
        except Exception as e:
            if self._overload_read_ok:
                logger.warning("[overload] Present_Load read failed — cut-off blind until it recovers: %s", e)
            self._overload_read_ok = False
            return None
        if not self._overload_read_ok:
            logger.info("[overload] Present_Load read recovered")
        self._overload_read_ok = True
        return {motor: load_magnitude(val) for motor, val in raw.items()}

    def _overload_cut(self, joint: str, load: int) -> None:
        """Stop motion and cut torque on every servo. No park move: the arm is blocked."""
        guard = self._overload
        logger.warning(
            "[overload] %s load %.1f%% >= %.1f%% for %.1fs — cutting torque, retry in %.0fs",
            joint, load / 10.0, guard.threshold / 10.0, guard.hold_s, guard.retry_s,
        )
        self._halt.set()
        errors: Dict[str, str] = {}
        # Block and cut under one lock hold, so no goal write can land in between and
        # re-engage torque.
        with self.bus_lock:
            self.robot.goal_writes_blocked = True
            for motor_name in self.robot.bus.motors:
                try:
                    self.robot.bus.write("Torque_Enable", motor_name, 0)
                except Exception as e:
                    errors[motor_name] = str(e)
        if errors:
            logger.warning("[overload] torque-off errors (offline?): %s", errors)
        if self._on_overload is not None:
            try:
                self._on_overload(joint, load)
            except Exception as e:
                logger.warning("[overload] on_overload handler failed: %s", e)

    def _overload_recover(self) -> None:
        """Lockout over: allow goal writes again and bring the body back like a resume."""
        with self.bus_lock:
            if self.robot:
                self.robot.goal_writes_blocked = False
        if not self._running.is_set():
            # Released (asleep) or zero-posed meanwhile: torque stays off for the next resume.
            logger.info("[overload] lockout over — body is parked, torque stays off until resume")
            return
        logger.info("[overload] lockout over — re-enabling servos")
        self.resume()

    def joint_status(self) -> Dict[str, dict]:
        """Per-joint online/offline status with angle and servo ID."""
        if not self.robot or not self.robot.bus:
            return {}
        bus = self.robot.bus
        ph = bus.port_handler
        pk = bus.packet_handler
        from scservo_sdk import COMM_SUCCESS

        servos: Dict[str, dict] = {}
        with self.bus_lock:
            for motor_name, motor_obj in bus.motors.items():
                key = f"{motor_name}.pos"
                sid = motor_obj.id
                detail = {"id": sid, "angle": None, "online": False, "error": None}
                try:
                    _, result, _ = pk.ping(ph, sid)
                    if result != COMM_SUCCESS:
                        detail["error"] = "no status packet"
                    else:
                        detail["online"] = True
                        try:
                            pos = bus.read("Present_Position", motor_name)
                            detail["angle"] = float(pos)
                        except Exception as e:
                            detail["error"] = f"read failed: {e}"
                except Exception as e:
                    detail["error"] = str(e)
                servos[key] = detail
        return servos

    def aim(self, direction: str, duration: float,
            current_positions: Dict[str, float],
            safety_policy: object) -> Dict[str, float]:
        """Aim to a named direction. Returns the final joint positions."""
        from hal.presets import AIM_PRESETS, AIM_LEFT, AIM_RIGHT, AIM_CENTER
        from hal.safety.policy import min_move_duration

        preset = AIM_PRESETS.get(direction)
        explicit_center = direction == AIM_CENTER
        if preset is None:
            logger.warning("Unknown aim direction %r — defaulting to center", direction)
            direction = AIM_CENTER
            preset = AIM_PRESETS[AIM_CENTER]

        if direction in (AIM_LEFT, AIM_RIGHT):
            positions = {**current_positions, "base_yaw.pos": preset["base_yaw.pos"]}
        elif explicit_center:
            positions = dict(preset)
        else:
            positions = {**preset, "base_yaw.pos": current_positions.get("base_yaw.pos", preset["base_yaw.pos"])}

        eff_duration = min_move_duration(safety_policy, positions, current_positions, duration)

        was_running = self._running.is_set()
        if was_running:
            self._running.clear()
            if self._event_thread and self._event_thread.is_alive():
                self._event_thread.join(timeout=2.0)

        try:
            if eff_duration > 0:
                # Abort on a real halt (POST /servo/stop) only. NOT on
                # _motion_aborted, whose _running check this very method just
                # falsified two lines up to take the bus.
                self.move_to(
                    positions,
                    duration=eff_duration,
                    should_abort=lambda: self._halt.is_set(),
                )
            else:
                self.send_positions(positions)
        finally:
            if was_running and not self._running.is_set():
                hold_pos = self._current_state
                if hold_pos:
                    self._current_recording = "__aim_hold__"
                    self._current_actions = [hold_pos]
                    self._current_frame_index = 0
                    self._hold_until = time.time() + 5.0
                self._running.set()
                self._event_thread = threading.Thread(
                    target=self._event_loop, daemon=True,
                )
                self._event_thread.start()
                if not hold_pos:
                    self.dispatch(SERVO_CMD_PLAY, self.idle_recording)

        return positions

    def nudge(self, yaw: float, pitch: float, duration: float,
              current_positions: Dict[str, float],
              safety_policy: object) -> Dict[str, float]:
        """Relative nudge from current position. Returns final positions."""
        from hal.safety.policy import min_move_duration

        positions = dict(current_positions)
        if yaw != 0:
            positions["base_yaw.pos"] = current_positions.get("base_yaw.pos", 0) + yaw
        if pitch != 0:
            positions["base_pitch.pos"] = current_positions.get("base_pitch.pos", 0) + pitch

        eff_duration = min_move_duration(safety_policy, positions, current_positions, duration)
        self.move_and_hold(positions, duration=eff_duration)
        return positions
