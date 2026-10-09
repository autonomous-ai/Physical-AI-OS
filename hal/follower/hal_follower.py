import os
#!/usr/bin/env python

# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import time
from functools import cached_property
from typing import Any

from lerobot.cameras.utils import make_cameras_from_configs
from lerobot.errors import DeviceAlreadyConnectedError, DeviceNotConnectedError
from lerobot.motors import Motor, MotorCalibration, MotorNormMode
from lerobot.motors.feetech import (
    FeetechMotorsBus,
    OperatingMode,
)

from lerobot.robots import Robot
from lerobot.robots.utils import ensure_safe_goal_position
from .config_hal_follower import LeLampFollowerConfig

logger = logging.getLogger(__name__)

# Set HAL_TRACE_JOINT to a motor name (e.g. "elbow_pitch") to log every
# Goal_Position write for it together with the call site that made it.
_TRACE_JOINT = os.environ.get("HAL_TRACE_JOINT", "").strip()


class LeLampFollower(Robot):
    """
    LeLamp Follower Arm designed by TheRobotStudio and Hugging Face.
    """

    config_class = LeLampFollowerConfig
    name = "hal_follower"

    def __init__(self, config: LeLampFollowerConfig):
        super().__init__(config)
        self.config = config
        norm_mode_body = MotorNormMode.DEGREES if config.use_degrees else MotorNormMode.RANGE_M100_100
        self.bus = FeetechMotorsBus(
            port=self.config.port,
            motors={
                "base_yaw": Motor(1, "sts3215", norm_mode_body),
                "base_pitch": Motor(2, "sts3215", norm_mode_body),
                "elbow_pitch": Motor(3, "sts3215", norm_mode_body),
                "wrist_roll": Motor(4, "sts3215", norm_mode_body),
                "wrist_pitch": Motor(5, "sts3215", norm_mode_body),
            },
            calibration=self.calibration,
        )

        self.cameras = make_cameras_from_configs(self.config.cameras)

        # Monotonic timestamp of the last motion command sent to the bus.
        # Camera consumers use it to wait for a frame captured after the
        # servos went quiet (motion-blur avoidance). Stamped by send_action;
        # AnimationService.move_to_raw stamps it too (bypasses send_action).
        self.last_write_monotonic: float = 0.0

        # Set by AnimationService while its overload cut-off holds the servos limp.
        # A Goal_Position write re-engages torque on these servos, so send_action
        # drops goals instead of writing them.
        self.goal_writes_blocked: bool = False

    @property
    def _motors_ft(self) -> dict[str, type]:
        return {f"{motor}.pos": float for motor in self.bus.motors}

    @property
    def _cameras_ft(self) -> dict[str, tuple]:
        return {
            cam: (self.config.cameras[cam].height, self.config.cameras[cam].width, 3) for cam in self.cameras
        }

    @cached_property
    def observation_features(self) -> dict[str, type | tuple]:
        return {**self._motors_ft, **self._cameras_ft}

    @cached_property
    def action_features(self) -> dict[str, type]:
        return {**self._motors_ft}

    @property
    def is_connected(self) -> bool:
        # Print each connected check
        status = self.bus.is_connected and all(cam.is_connected for cam in self.cameras.values())

        return status

    def connect(self, calibrate: bool = True) -> None:
        """
        We assume that at connection time, arm is in a rest position,
        and torque can be safely disabled to run calibration.
        """
        if self.is_connected:
            raise DeviceAlreadyConnectedError(f"{self} already connected")

        self.bus.connect()
        if not self.is_calibrated and calibrate:
            logger.info(
                "Mismatch between calibration values in the motor and the calibration file or no calibration file found"
            )
            self.calibrate()

        for cam in self.cameras.values():
            cam.connect()

        self.configure()
        logger.info(f"{self} connected.")

    @property
    def is_calibrated(self) -> bool:
        return self.bus.is_calibrated

    def calibrate(self) -> None:
        if self.calibration:
            # self.calibration is not empty here
            user_input = input(
                f"Press ENTER to use provided calibration file associated with the id {self.id}, or type 'c' and press ENTER to run calibration: "
            )
            if user_input.strip().lower() != "c":
                logger.info(f"Writing calibration file associated with the id {self.id} to the motors")
                self.bus.write_calibration(self.calibration)
                return

        logger.info(f"\nRunning calibration of {self}")
        self.bus.disable_torque()
        for motor in self.bus.motors:
            self.bus.write("Operating_Mode", motor, OperatingMode.POSITION.value)

        input(f"Move {self} to the middle of its range of motion and press ENTER....")
        homing_offsets = self.bus.set_half_turn_homings()

        print(
            "Move all joints sequentially through their entire ranges "
            "of motion.\nRecording positions. Press ENTER to stop..."
        )
        range_mins, range_maxes = self.bus.record_ranges_of_motion()

        self.calibration = {}
        for motor, m in self.bus.motors.items():
            self.calibration[motor] = MotorCalibration(
                id=m.id,
                drive_mode=0,
                homing_offset=homing_offsets[motor],
                range_min=range_mins[motor],
                range_max=range_maxes[motor],
            )

        self.bus.write_calibration(self.calibration)
        self._save_calibration()
        print("Calibration saved to", self.calibration_fpath)

    def configure(self) -> None:
        with self.bus.torque_disabled():
            self.bus.configure_motors()
            for motor in self.bus.motors:
                self.bus.write("Operating_Mode", motor, OperatingMode.POSITION.value)
                # Set P_Coefficient to lower value to avoid shakiness (Default is 32)
                self.bus.write("P_Coefficient", motor, 16)
                # Set I_Coefficient and D_Coefficient to default value 0 and 32
                self.bus.write("I_Coefficient", motor, 0)
                self.bus.write("D_Coefficient", motor, 32)

    def setup_motors(self) -> None:
        for motor in reversed(self.bus.motors):
            input(f"Connect the controller board to the '{motor}' motor only and press enter.")
            self.bus.setup_motor(motor)
            print(f"'{motor}' motor id set to {self.bus.motors[motor].id}")

    def get_observation(self) -> dict[str, Any]:
        if not self.is_connected:
            raise DeviceNotConnectedError(f"{self} is not connected.")

        # Read arm position
        start = time.perf_counter()
        obs_dict = self.bus.sync_read("Present_Position")
        obs_dict = {f"{motor}.pos": val for motor, val in obs_dict.items()}
        dt_ms = (time.perf_counter() - start) * 1e3
        logger.debug(f"{self} read state: {dt_ms:.1f}ms")

        # Capture images from cameras
        for cam_key, cam in self.cameras.items():
            start = time.perf_counter()
            obs_dict[cam_key] = cam.async_read()
            dt_ms = (time.perf_counter() - start) * 1e3
            logger.debug(f"{self} read {cam_key}: {dt_ms:.1f}ms")

        # Read LEDs
        start = time.perf_counter()
        dt_ms = (time.perf_counter() - start) * 1e3
        logger.debug(f"{self} read LEDs: {dt_ms:.1f}ms")
        return obs_dict

    def send_action(self, action: dict[str, Any]) -> dict[str, Any]:
        """Command arm to move to a target joint configuration.

        The relative action magnitude may be clipped depending on the configuration parameter
        `max_relative_target`. In this case, the action sent differs from original action.
        Thus, this function always returns the action actually sent.

        Raises:
            RobotDeviceNotConnectedError: if robot is not connected.

        Returns:
            the action sent to the motors, potentially clipped.
        """
        if not self.is_connected:
            raise DeviceNotConnectedError(f"{self} is not connected.")

        if self.goal_writes_blocked:
            return {}

        goal_pos = {key.removesuffix(".pos"): val for key, val in action.items() if key.endswith(".pos")}

        # Temporary: name whoever writes a joint, so a correction that is applied
        # and then quietly undone shows both writers. Off unless HAL_TRACE_JOINT
        # names a motor — extract_stack on every frame at 30fps is not free.
        if _TRACE_JOINT and _TRACE_JOINT in goal_pos:
            import traceback
            stack = traceback.extract_stack(limit=6)[:-1]
            where = " <- ".join(
                f"{f.name}:{f.lineno}" for f in reversed(stack[-3:])
            )
            try:
                te = self.bus.read("Torque_Enable", _TRACE_JOINT, normalize=False)
                pr = self.bus.read("Present_Position", _TRACE_JOINT, normalize=False)
                mv = self.bus.read("Moving", _TRACE_JOINT, normalize=False)
            except Exception as e:
                te = pr = mv = "err:%s" % e
            logger.info("[trace] %s goal=%.2f torque=%s present_raw=%s moving=%s  %s",
                        _TRACE_JOINT, goal_pos[_TRACE_JOINT], te, pr, mv, where)

        # Cap goal position when too far away from present position.
        # /!\ Slower fps expected due to reading from the follower.
        if self.config.max_relative_target is not None:
            present_pos = self.bus.sync_read("Present_Position")
            goal_present_pos = {key: (g_pos, present_pos[key]) for key, g_pos in goal_pos.items()}
            goal_pos = ensure_safe_goal_position(goal_present_pos, self.config.max_relative_target)


        # Send goal position to the arm
        self.bus.sync_write("Goal_Position", goal_pos)
        self.last_write_monotonic = time.monotonic()
        return {f"{motor}.pos": val for motor, val in goal_pos.items()}

    def disconnect(self):
        if not self.is_connected:
            raise DeviceNotConnectedError(f"{self} is not connected.")

        self.bus.disconnect(self.config.disable_torque_on_disconnect)
        for cam in self.cameras.values():
            cam.disconnect()

        logger.info(f"{self} disconnected.")
