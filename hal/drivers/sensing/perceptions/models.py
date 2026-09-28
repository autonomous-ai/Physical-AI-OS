from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import cv2


class PersonKind(StrEnum):
    FRIEND = "friend"
    STRANGER = "stranger"
    UNSURE = "unsure"


@dataclass
class Face:
    bbox: list[int]
    kind: PersonKind
    person_id: str
    confidence: float
    # Axis-aligned [x1, y1, x2, y2] face box (frame pixels) derived from the dense 468
    # face-mesh landmarks during recognition — the re-centered, no-rotation framing the
    # cloud emotion model expects.
    emotion_box: list[int] | None = None


@dataclass
class PersonData:
    id: str
    kind: PersonKind
    last_seen: float | None = None
    last_session_time: float | None = None


@dataclass
class FaceDetectionData:
    frame: cv2.typing.MatLike | None = None
    faces: list[Face] = field(default_factory=list)


@dataclass
class PerceptionData:
    frame: cv2.typing.MatLike | None = None
    detected_faces: FaceDetectionData | None = None


@dataclass
class PerceptionConfig:
    enable_face: bool = False
    enable_motion: bool = False
    enable_motion_per_face: bool = False
    enable_emotion: bool = False
    enable_pose: bool = False
    enable_light: bool = False
    enable_sound: bool = False
    enable_fire_hazard: bool = False
