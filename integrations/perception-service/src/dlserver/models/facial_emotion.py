"""HTTP/WS models for emotion endpoints — Pydantic request/response types."""

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Tag

from core.models.facial_emotion import EmotionDetection
from core.types import Omit, omit

# --- WebSocket messages ---


class EmotionFrameRequest(BaseModel):
    type: Literal["frame"] = "frame"
    task: Literal["emotion"] = "emotion"
    frame_b64: str


class EmotionConfigRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(arbitrary_types_allowed=True)
    type: Literal["config"] = "config"
    task: Literal["emotion"] = "emotion"
    threshold: float | Omit = omit
    frame_interval: float | Omit = omit


class EmotionHeartBeatRequest(BaseModel):
    type: Literal["heartbeat"] = "heartbeat"
    task: Literal["emotion"] = "emotion"


EmotionRequest = Annotated[
    Annotated[EmotionFrameRequest, Tag("frame")]
    | Annotated[EmotionConfigRequest, Tag("config")]
    | Annotated[EmotionHeartBeatRequest, Tag("heartbeat")],
    Discriminator("type"),
]


# --- HTTP request/response ---


class EmotionRecognizeRequest(BaseModel):
    """HTTP request for single-image emotion recognition."""

    image_b64: str
    threshold: float = 0.5
    raw: bool = False
    """Skip the server's label gate and threshold drop and return every class
    probability, so the client can gate itself. Old servers ignore this field
    (no extra="forbid"), which is what lets a new client fall back."""


class EmotionItem(BaseModel):
    """Single emotion in an HTTP response."""

    emotion: str
    confidence: float
    face_confidence: float
    bbox: list[int]
    valence: float | None = None
    arousal: float | None = None
    probabilities: dict[str, float] | None = None


class EmotionRecognizeResponse(BaseModel):
    """HTTP response for single-image emotion recognition."""

    detections: list[EmotionItem]

    @staticmethod
    def from_emotion_detection(detection: EmotionDetection) -> "EmotionRecognizeResponse":
        return EmotionRecognizeResponse(
            detections=[
                EmotionItem(
                    emotion=e.emotion,
                    confidence=e.confidence,
                    face_confidence=e.face_confidence,
                    bbox=e.bbox,
                    valence=e.valence,
                    arousal=e.arousal,
                )
                for e in detection.emotions
            ]
        )


class EmotionResponse(BaseModel):
    """WS response for a single frame."""

    detections: list[EmotionItem]

    @staticmethod
    def from_emotion_detection(detection: EmotionDetection) -> "EmotionResponse":
        return EmotionResponse(
            detections=[
                EmotionItem(
                    emotion=e.emotion,
                    confidence=e.confidence,
                    face_confidence=e.face_confidence,
                    bbox=e.bbox,
                    valence=e.valence,
                    arousal=e.arousal,
                )
                for e in detection.emotions
            ]
        )
