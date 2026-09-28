"""HTTP/WS models for object detection endpoints — Pydantic request/response types."""

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag

from core.models.object import ObjectDetection
from core.types import Omit, omit


class ObjectFrameRequest(BaseModel):
    type: Literal["frame"] = "frame"
    task: Literal["object"] = "object"
    frame_b64: str


class ObjectConfigRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(arbitrary_types_allowed=True)
    type: Literal["config"] = "config"
    task: Literal["object"] = "object"
    frame_interval: float | Omit = omit
    classes: list[str] | None | Omit = omit
    threshold: float | Omit = omit


class ObjectHeartBeatRequest(BaseModel):
    type: Literal["heartbeat"] = "heartbeat"
    task: Literal["object"] = "object"


ObjectRequest = Annotated[
    Annotated[ObjectFrameRequest, Tag("frame")]
    | Annotated[ObjectConfigRequest, Tag("config")]
    | Annotated[ObjectHeartBeatRequest, Tag("heartbeat")],
    Discriminator("type"),
]


class ObjectDetectRequest(BaseModel):
    """HTTP request for single-image object detection."""

    image_b64: str
    classes: list[str] | None = Field(None, max_length=500)


class ObjectDetectionItemResponse(BaseModel):
    class_name: str
    xywh: list[float]
    confidence: float


class ObjectDetectResponse(BaseModel):
    """HTTP response for single-image object detection."""

    detections: list[ObjectDetectionItemResponse]

    @staticmethod
    def from_object_detection(detection: ObjectDetection) -> "ObjectDetectResponse":
        return ObjectDetectResponse(
            detections=[
                ObjectDetectionItemResponse(
                    class_name=d.class_name,
                    xywh=d.xywh,
                    confidence=d.confidence,
                )
                for d in detection.detections
            ]
        )


class ObjectResponse(BaseModel):
    """WS response for a single frame."""

    detections: list[ObjectDetectionItemResponse]

    @staticmethod
    def from_object_detection(detection: ObjectDetection) -> "ObjectResponse":
        return ObjectResponse(
            detections=[
                ObjectDetectionItemResponse(
                    class_name=d.class_name,
                    xywh=d.xywh,
                    confidence=d.confidence,
                )
                for d in detection.detections
            ]
        )
