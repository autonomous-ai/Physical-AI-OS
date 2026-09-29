"""HTTP/WS models for pose endpoints — Pydantic request/response types."""

from dataclasses import asdict
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Tag

from core.models.pose import PoseDetection
from core.types import Omit, omit


class PoseFrameRequest(BaseModel):
    type: Literal["frame"] = "frame"
    task: Literal["pose"] = "pose"
    frame_b64: str


class PoseConfigRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(arbitrary_types_allowed=True)
    type: Literal["config"] = "config"
    task: Literal["pose"] = "pose"
    frame_interval: float | Omit = omit
    confidence_threshold_2d: float | Omit = omit
    min_valid_keypoints: int | Omit = omit


class PoseHeartBeatRequest(BaseModel):
    type: Literal["heartbeat"] = "heartbeat"
    task: Literal["pose"] = "pose"


PoseRequest = Annotated[
    Annotated[PoseFrameRequest, Tag("frame")]
    | Annotated[PoseConfigRequest, Tag("config")]
    | Annotated[PoseHeartBeatRequest, Tag("heartbeat")],
    Discriminator("type"),
]


class PoseEstimateRequest(BaseModel):
    """HTTP request for single-image pose estimation."""

    image_b64: str


class Pose2DResponse(BaseModel):
    graph_type: str
    joints: list[list[float]]
    confs: list[float]


class Pose3DResponse(BaseModel):
    graph_type: str
    joints: list[list[float]]
    confs: list[float]


class PoseEstimateResponse(BaseModel):
    """HTTP response for single-image pose estimation."""

    pose_2d: Pose2DResponse
    pose_3d: Pose3DResponse | None = None
    ergo: dict[str, Any] | None = None

    @staticmethod
    def from_pose_detection(detection: PoseDetection) -> "PoseEstimateResponse":
        pose_2d: Pose2DResponse = Pose2DResponse(
            graph_type=detection.pose_2d.graph_type,
            joints=[[p.x, p.y] for p in detection.pose_2d.joints],
            confs=detection.pose_2d.confs,
        )

        pose_3d: Pose3DResponse | None = None
        if detection.pose_3d is not None:
            pose_3d = Pose3DResponse(
                graph_type=detection.pose_3d.graph_type,
                joints=[[p.x, p.y, p.z] for p in detection.pose_3d.joints],
                confs=detection.pose_3d.confs,
            )

        return PoseEstimateResponse(
            pose_2d=pose_2d,
            pose_3d=pose_3d,
            ergo=asdict(detection.ergo) if detection.ergo is not None else None,
        )


class PoseResponse(BaseModel):
    """WS response for a single frame."""

    pose_2d: Pose2DResponse
    pose_3d: Pose3DResponse | None = None
    ergo: dict[str, Any] | None = None

    @staticmethod
    def from_pose_detection(detection: PoseDetection) -> "PoseResponse":
        return PoseResponse(
            **PoseEstimateResponse.from_pose_detection(detection).model_dump()
        )
