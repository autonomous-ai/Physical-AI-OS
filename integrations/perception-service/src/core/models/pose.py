"""Internal pose models — dataclasses for core logic, not HTTP."""

from dataclasses import dataclass, field
from enum import IntEnum
from typing import NamedTuple

import numpy as np
import numpy.typing as npt

from core.enums.pose import GraphEnum


class Point2D(NamedTuple):
    x: float
    y: float


class Point3D(NamedTuple):
    x: float
    y: float
    z: float


@dataclass
class RawPose2DDetection:
    """Raw 2D pose estimator output — batched numpy arrays."""

    keypoints: npt.NDArray[np.float32]
    """Shape: (N, K, 2) — (x, y) per joint per person."""

    scores: npt.NDArray[np.float32]
    """Shape: (N, K) — confidence per joint per person."""


@dataclass
class RawPose3DDetection:
    """Raw 3D pose lifter output — batched numpy arrays."""

    joints_3d: npt.NDArray[np.float32]
    """Shape: (N, K, 3) — (x, y, z) per joint per person."""


class RiskLevel(IntEnum):
    NEGLIGIBLE = 1  # Score 1-2: acceptable posture
    LOW = 2         # Score 3-4: further investigation, may need changes
    MEDIUM = 3      # Score 5-6: investigation and changes needed soon
    HIGH = 4        # Score 7+: investigation and changes needed immediately


@dataclass
class BodyPartScores:
    """Individual body-part scores and angles from ergonomic assessment."""

    upper_arm: int
    lower_arm: int
    wrist: int
    wrist_twist: int
    neck: int
    trunk: int
    legs: int
    table_a: int
    table_b: int
    score_a: int
    score_b: int
    upper_arm_angle: int
    lower_arm_angle: int
    neck_angle: int
    trunk_angle: int


@dataclass
class SideAssessment:
    """Ergonomic assessment result for one side of the body."""

    score: int
    risk_level: RiskLevel
    body_scores: BodyPartScores
    skipped_joints: list[str] = field(default_factory=list)


@dataclass
class ErgoAssessment:
    """Result of an ergonomic assessment for both sides."""

    left: SideAssessment
    right: SideAssessment
    score: int
    risk_level: RiskLevel


@dataclass
class Pose2D:
    graph_type: GraphEnum
    joints: list[Point2D]
    confs: list[float]


@dataclass
class Pose3D:
    graph_type: GraphEnum
    joints: list[Point3D]
    confs: list[float]


@dataclass
class PoseDetection:
    """Session output: pose estimation result for a single frame."""

    pose_2d: Pose2D
    pose_3d: Pose3D | None = None
    ergo: ErgoAssessment | None = None


@dataclass
class PosePerceptionSessionConfig:
    frame_interval: float = 1.0
    confidence_threshold_2d: float = 0.3
    min_valid_keypoints: int = 5
