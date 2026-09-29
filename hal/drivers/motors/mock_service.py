"""Mock motion driver — a body made of variables.

The whole stack has needed a robot to run. This is the smallest thing that
satisfies `MotionService` without hardware: joints are floats in a dict, moves
interpolate over their commanded duration the way the SDK-backed driver does,
and every call is recorded so a test (or a person poking HAL on a laptop) can
see exactly what the robot was told to do.

It is used by `robots/sim` (the mock body) together with `HAL_BOARD=sim`. It
is not a physics simulator: it models neither inertia nor collision. It does
replay the shipped CSV recordings in memory, through the same stretch-and-
resample timing the physical driver uses (hal/drivers/motors/recording_timing.py),
so a recording takes the same wall-clock time here as it does on a body.

    from hal.drivers.motors.mock_service import MockMotionService
    m = MockMotionService(); m.start()
    m.move_to({"base_yaw.pos": 20.0}, duration=0.5)
    m.calls[-1]        # ("move_to", {"base_yaw.pos": 20.0}, 0.5)
    m.get_positions()  # {"base_yaw.pos": 20.0, ...}
"""
from __future__ import annotations

import csv
import logging
import os
import threading
import time

from hal import config
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from hal.drivers.motors.recording_timing import RECORDING_TIME_COLUMN, resample_recording

logger = logging.getLogger("hal.motion.mock")

# Frame grid for playback and interpolated moves. Matches the rate the
# SDK-backed service runs its event loop at, so a recording takes the same
# wall-clock time here as it does on a body.
PLAYBACK_FPS = 30.0

# Where the arm ends up once torque is cut. The physical driver reaches this by
# walking to REST_RAW (raw servo ticks) and letting the arm settle; those ticks
# cannot be converted to degrees here, because the tick->degree mapping lives in
# each servo's own EEPROM calibration. So the mock arrives the way the real arm
# does — by falling. The stops are the lowest angle each joint was ever recorded
# at across hal/recordings/*.csv, i.e. the bottom of its observed travel, and
# "lower is downward" is the convention the `down` aim preset uses
# (base_pitch 8, elbow 15, wrist_pitch -8 vs center's 25/43/30).
GRAVITY_REST = {
    "base_pitch.pos": -13.7,
    "elbow_pitch.pos": -11.5,
    "wrist_pitch.pos": -27.8,
}

# Degrees/second^2 for the limp arm. Not a physical model of the linkage — it is
# tuned so the fall takes about half a second, which is what the arm does.
GRAVITY_DPS2 = 900.0

# After a one-shot recording ends the body keeps that pose briefly, then eases
# back into idle. aim() holds longer than a gesture does: the point of aiming is
# to look somewhere and stay looking, so 5s matches the physical driver.
AIM_HOLD_S = 5.0

# What routes/servo.py reports while that post-aim hold is running. The physical
# driver uses the same sentinel, so a client cannot tell the two apart.
AIM_HOLD_RECORDING = "__aim_hold__"

# Recordings that end by holding their final pose rather than returning to idle
# — sleepy stays down until something wakes the lamp.
NO_IDLE_RECORDINGS = {"sleepy"}

# The Lamp joint set, so skills and recordings written against the reference
# body work unchanged against the mock one.
DEFAULT_JOINTS = (
    "base_yaw.pos",
    "base_pitch.pos",
    "elbow_pitch.pos",
    "wrist_pitch.pos",
    "wrist_roll.pos",
)


class MockMotionService:
    """In-memory MotionService. Every mutation is recorded in `calls`."""

    def __init__(
        self,
        joints: Optional[Set[str]] = None,
        safety_policy: Any = None,
        geometry: Any = None,
        idle_recording: str = "idle",
    ) -> None:
        self._joints = set(joints or DEFAULT_JOINTS)
        self.idle_recording = idle_recording
        # Keep the same construction shape as SDK-backed motion services. The
        # HTTP routes still enforce the policy; retaining it here lets the mock
        # boot through the production factory without special cases.
        self._safety_policy = safety_policy
        self._geometry = geometry
        self._positions: Dict[str, float] = {j: 0.0 for j in self._joints}
        # A physical Lamp is never visually meaningful with all five joints at
        # zero.  The laptop Lamp starts in the same center pose its real aim
        # driver uses; the generic `sim` body deliberately remains all-zero.
        if config.SIMULATE and os.environ.get("DEVICE_TYPE") == "lamp":
            from hal.presets import AIM_CENTER, AIM_PRESETS
            self._positions.update(AIM_PRESETS[AIM_CENTER])
        self._lock = threading.Lock()
        self._connected = False
        self._suppressed = False
        # `_suppressed` has three setters and cannot say which. Write both together.
        self._mode: Optional[str] = None
        self._frozen = False
        # Body ownership — see MotionService in base.py. The simulator carries
        # it for the same reason it carries every other suppression flag: the
        # routes read it, so a laptop body has to answer the same questions a
        # physical one does.
        self._tracking_flag = False
        self._body_owners = 0
        self._body_owner_lock = threading.Lock()
        self._torque = True
        # Set by halt(), cleared by the next commanded move — mirrors the real
        # driver's _halt event, so a test can assert the sequence without one.
        self._halted = False
        # routes/servo.py exposes the active recording for every MotionService.
        self._current_recording: Optional[str] = None
        self._recordings: Dict[str, List[Dict[str, float]]] = {}
        self._play_cancel = threading.Event()
        self._play_thread: Optional[threading.Thread] = None
        self.calls: List[tuple] = []

    # --- Lifecycle ---

    def start(self, skip_wake: bool = False) -> None:
        self._connected = True
        self._record("start")
        logger.info("[mock] motion up — %d joints, no hardware", len(self._joints))

    def stop(self, timeout: float = 5.0) -> None:
        self._cancel_playback()
        self._connected = False
        self._record("stop", timeout)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def ensure_running(self) -> None:
        self._connected = True

    # --- Animation / event dispatch ---

    def dispatch(self, event_type: str, payload: Any) -> None:
        if event_type == "play":
            self._play_recording(str(payload))
        self._record("dispatch", event_type, payload)

    def get_available_recordings(self) -> List[str]:
        recordings_dir = Path(__file__).parents[2] / "recordings"
        builtins = {path.stem for path in recordings_dir.glob("*.csv")}
        return sorted(builtins | set(self._recordings))

    def add_recording(self, name: str, actions: List[Dict[str, float]]) -> None:
        self._recordings[name] = list(actions)
        self._record("add_recording", name, len(actions))

    # --- Freeze ---

    def freeze(self) -> None:
        self._frozen = True
        self._record("freeze")

    def unfreeze(self) -> None:
        self._frozen = False
        self._record("unfreeze")

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    @property
    def is_suppressed(self) -> bool:
        return self._suppressed

    @property
    def motion_mode(self) -> Optional[str]:
        """Never "released": release() here cuts torque without suppressing."""
        return self._mode

    @property
    def _tracking_active(self) -> bool:
        """True while anything owns the body — a flag holder or a live writer."""
        return self._tracking_flag or self._body_owners > 0

    @_tracking_active.setter
    def _tracking_active(self, value: bool) -> None:
        # Assignment sets the FLAG only, never a counter held by a live writer.
        self._tracking_flag = bool(value)

    def acquire_body(self) -> None:
        with self._body_owner_lock:
            self._body_owners += 1

    def release_body(self) -> None:
        with self._body_owner_lock:
            self._body_owners = max(0, self._body_owners - 1)

    # --- Motion primitives ---

    def move_to(self, target_positions: Dict[str, float], duration: float = 2.0) -> None:
        self._cancel_playback()
        self._halted = False
        self._travel(target_positions, duration)
        self._record("move_to", dict(target_positions), duration)

    def move_and_hold(self, target_positions: Dict[str, float], duration: float = 2.0) -> None:
        self._cancel_playback()
        self._travel(target_positions, duration)
        self._suppressed = True
        self._mode = "hold"
        self._record("move_and_hold", dict(target_positions), duration)

    def get_joint_names(self) -> Set[str]:
        return set(self._joints)

    def get_positions(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._positions)

    def send_positions(self, positions: Dict[str, float]) -> None:
        self._apply(positions)
        self._record("send_positions", dict(positions))

    # --- Postures & modes ---

    def zero_pose(self) -> None:
        self._apply({j: 0.0 for j in self._joints})
        self._suppressed = True
        self._mode = "zero"
        self._record("zero_pose")

    def release(self) -> Dict[str, str]:
        # Same shape as the real driver: reach rest, then torque off. The mock
        # is where "release is not a stop" is easiest to see — the arm moves
        # first, and nothing here can abort that move in flight either.
        self._cancel_playback()
        self._fall()
        self._torque = False
        self._record("release")
        return {}

    def halt(self) -> None:
        # The honest mock: a halt writes no new position and does not touch
        # torque. Contrast release() above, which travels to rest first — the
        # whole point of the distinction this driver exists to make visible.
        self._halted = True
        self._cancel_playback()
        self._record("halt")

    def resume(self) -> None:
        self._halted = False
        self._torque = True
        self._suppressed = False
        self._mode = None
        self._record("resume")

    # AnimationService exposes _hold_mode and routes/scene.py reads and writes it;
    # without it /scene/off raised AttributeError in the simulator.
    @property
    def _hold_mode(self) -> bool:
        return self._mode == "hold"

    @_hold_mode.setter
    def _hold_mode(self, value: bool) -> None:
        if value:
            self._suppressed = True
            self._mode = "hold"
        elif self._mode == "hold":
            self._suppressed = False
            self._mode = None

    def hold(self, explicit: bool = False) -> None:
        self._suppressed = True
        self._mode = "hold"
        self._record("hold", explicit)

    def joint_status(self) -> Dict[str, dict]:
        with self._lock:
            return {
                j: {"online": self._connected, "angle": self._positions[j], "id": i + 1}
                for i, j in enumerate(sorted(self._joints))
            }

    # --- Aim & nudge ---

    def aim(self, direction: str, duration: float, current_positions: Dict[str, float],
            safety_policy: Any) -> Dict[str, float]:
        # Keep aim semantics aligned with AnimationService. The simulator is a
        # safe motion driver, not a second Lamp kinematic table: left/right
        # only pan base_yaw; other named aims preserve the current yaw.
        from hal.presets import AIM_CENTER, AIM_LEFT, AIM_PRESETS, AIM_RIGHT
        from hal.safety.policy import min_move_duration

        preset = AIM_PRESETS.get(direction)
        explicit_center = direction == AIM_CENTER
        if preset is None:
            logger.warning("Unknown aim direction %r — defaulting to center", direction)
            direction = AIM_CENTER
            preset = AIM_PRESETS[AIM_CENTER]

        current = current_positions or self.get_positions()
        if direction in (AIM_LEFT, AIM_RIGHT):
            target = {**current, "base_yaw.pos": preset["base_yaw.pos"]}
        elif explicit_center:
            # Same rule as AnimationService.
            target = dict(preset)
        else:
            target = {**preset, "base_yaw.pos": current.get("base_yaw.pos", preset["base_yaw.pos"])}
        self._cancel_playback()
        self._halted = False
        # Same speed ceiling the body obeys: a simulator that swung faster than
        # SAFETY.md allows would show a move the robot cannot make.
        self._travel(target, min_move_duration(safety_policy, target, current, duration))
        # Look, stay looking, then breathe again — the physical driver parks an
        # __aim_hold__ pose for 5s and only then returns to idle.
        self._settle_to_idle(AIM_HOLD_S)
        self._record("aim", direction, duration)
        return self.get_positions()

    def nudge(self, yaw: float, pitch: float, duration: float,
              current_positions: Dict[str, float],
              safety_policy: Any) -> Dict[str, float]:
        from hal.safety.policy import min_move_duration

        base = current_positions or self.get_positions()
        target = {
            "base_yaw.pos": base.get("base_yaw.pos", 0.0) + yaw,
            "base_pitch.pos": base.get("base_pitch.pos", 0.0) + pitch,
        }
        # move_and_hold, as the real driver does — a nudge holds where it lands.
        self.move_and_hold(target, min_move_duration(safety_policy, target, base, duration))
        self._record("nudge", yaw, pitch, duration)
        return self.get_positions()

    # --- internals ---

    def _travel(self, target: Dict[str, float], duration: float) -> None:
        """Interpolate to the target at PLAYBACK_FPS, blocking like the body does.

        The SDK-backed driver's move_to walks frames and returns only once the
        arm has arrived, so a caller measuring how long a move takes measures
        the same thing here. duration <= 0 lands in one step, as it does there.
        """
        frames = int(duration * PLAYBACK_FPS)
        if frames < 1:
            self._apply(target)
            return
        start = self.get_positions()
        step = 1.0 / PLAYBACK_FPS
        for frame in range(1, frames + 1):
            if self._halted:
                return
            time.sleep(step)
            p = frame / frames
            self._apply({
                joint: start.get(joint, value) + (value - start.get(joint, value)) * p
                for joint, value in target.items()
            })

    def _fall(self) -> None:
        """Let the limp arm drop to GRAVITY_REST, accelerating as it goes.

        A joint that is already at or below its stop does not move: gravity only
        pulls one way. Yaw and roll are untouched — the arm swings about
        horizontal axes, so neither of those is loaded by its own weight.
        """
        speed = {joint: 0.0 for joint in GRAVITY_REST}
        step = 1.0 / PLAYBACK_FPS
        while True:
            moving = {}
            for joint, stop in GRAVITY_REST.items():
                if joint not in self._joints:
                    continue
                angle = self.get_positions().get(joint, 0.0)
                if angle <= stop:
                    continue
                speed[joint] += GRAVITY_DPS2 * step
                moving[joint] = max(stop, angle - speed[joint] * step)
            if not moving:
                return
            time.sleep(step)
            self._apply(moving)

    def _apply(self, positions: Dict[str, float]) -> None:
        with self._lock:
            for joint, value in positions.items():
                if joint in self._joints:
                    self._positions[joint] = float(value)

    def _cancel_playback(self) -> None:
        self._play_cancel.set()
        self._current_recording = None

    def _play_recording(self, name: str, hold_s: float = 0.0) -> None:
        """Replay the shipped CSV frames in memory, with no actuator output.

        A recording that finishes does not leave the body frozen where it
        landed: the driver holds that pose for `hold_s`, then interpolates back
        into the idle loop, and idle repeats until something else is commanded.
        A mock that stopped dead instead would show a lamp that goes still after
        every gesture, which is not what the robot does.
        """
        self._cancel_playback()
        frames = self._load_recording(name)
        if not frames:
            logger.warning("[mock] recording %r is unavailable", name)
            return

        cancel = threading.Event()
        self._play_cancel = cancel
        self._current_recording = name

        def replay() -> None:
            playing, current, hold = name, frames, hold_s
            try:
                while True:
                    previous = current[0][0]
                    for timestamp, positions in current:
                        if cancel.wait(max(0.0, timestamp - previous)):
                            return
                        self._apply(positions)
                        previous = timestamp
                    if playing == self.idle_recording:
                        if self._suppressed:
                            return  # hold() owns the pose: stop breathing, keep the current frame
                        continue  # idle is the resting loop, not a one-shot
                    # sleepy and friends are meant to hold their final pose, and
                    # an explicit hold() means the caller owns the pose now.
                    if playing in NO_IDLE_RECORDINGS or self._suppressed:
                        return
                    if hold and cancel.wait(hold):
                        return
                    hold = 0.0
                    following = self._load_recording(self.idle_recording)
                    if not following:
                        return
                    playing, current = self.idle_recording, following
                    self._current_recording = playing
            finally:
                if self._play_cancel is cancel:
                    self._current_recording = None

        self._play_thread = threading.Thread(
            target=replay, daemon=True, name=f"mock-servo-{name}"
        )
        self._play_thread.start()

    def _settle_to_idle(self, hold_s: float) -> None:
        """Hold the pose just reached, then fall back into the idle loop."""
        self._cancel_playback()
        cancel = threading.Event()
        self._play_cancel = cancel
        self._current_recording = AIM_HOLD_RECORDING

        def settle() -> None:
            if cancel.wait(hold_s):
                return
            if self._play_cancel is cancel and not self._suppressed:
                self._play_recording(self.idle_recording)

        self._play_thread = threading.Thread(
            target=settle, daemon=True, name="mock-servo-settle"
        )
        self._play_thread.start()

    def _load_recording(self, name: str) -> List[tuple[float, Dict[str, float]]]:
        """Frames on the same grid the physical driver would play them on.

        A simulator that played a recording faster than the body can move it
        would misreport the one thing playback is about, so the shared
        stretch-and-resample rule runs here too — see recording_timing.
        """
        step = 1.0 / PLAYBACK_FPS
        if name in self._recordings:
            return [(index * step, positions) for index, positions in enumerate(self._recordings[name])]
        path = Path(__file__).parents[2] / "recordings" / f"{name}.csv"
        if not path.is_file():
            return []
        try:
            with path.open(newline="") as source:
                times: List[float] = []
                frames: List[Dict[str, float]] = []
                for row in csv.DictReader(source):
                    raw_t = row.get(RECORDING_TIME_COLUMN)
                    if raw_t in (None, ""):
                        times = []
                        break
                    times.append(float(raw_t))
                    frames.append({
                        joint: float(value) for joint, value in row.items()
                        if joint != RECORDING_TIME_COLUMN and value is not None
                    })
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("[mock] cannot load recording %r: %s", name, exc)
            return []

        if len(frames) < 2 or len(times) != len(frames):
            # No usable time axis: play what was authored rather than invent timing.
            return [(index * step, positions) for index, positions in enumerate(frames)]
        resampled = resample_recording(
            times, frames, name, PLAYBACK_FPS, self._safety_policy, self._geometry
        )
        return [(index * step, positions) for index, positions in enumerate(resampled)]

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))
