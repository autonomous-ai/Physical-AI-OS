"""Per-connection emotion detection session (threshold filtering and rate limiting)."""

import time
from typing import Any, cast

import cv2.typing as cv2t
from typing_extensions import override

from core.models.face import FaceCrop, RawFaceDetection
from core.models.facial_emotion import (
    Emotion,
    EmotionDetection,
    EmotionPerceptionSessionConfig,
    RawEmotionDetection,
)
from core.perception.base import PerceptionSessionBase
from core.perception.base.batching import InputBatcher
from core.perception.face.predictors.base import FaceDetector
from core.perception.facial_emotion.label_gating import resolve_label
from core.perception.facial_emotion.predictors.base import EmotionRecognizer
from core.types import Omit, omit
from core.utils.common import get_or_default


class EmotionPerceptionSession(
    PerceptionSessionBase[
        cv2t.MatLike,
        EmotionDetection,
        EmotionPerceptionSessionConfig,
    ]
):
    """Per-connection session for emotion detection."""

    DEFAULT_CONFIG: EmotionPerceptionSessionConfig = EmotionPerceptionSessionConfig()

    def __init__(
        self,
        emotion_batcher: InputBatcher[cv2t.MatLike, RawEmotionDetection],
        face_batcher: InputBatcher[cv2t.MatLike, RawFaceDetection],
        config: EmotionPerceptionSessionConfig | None = None,
    ) -> None:
        config = get_or_default(config, EmotionPerceptionSessionConfig())
        super().__init__(config)
        self._emotion_batcher: InputBatcher[cv2t.MatLike, RawEmotionDetection] = emotion_batcher
        self._face_batcher: InputBatcher[cv2t.MatLike, RawFaceDetection] = face_batcher
        self._running: bool = False

    @override
    async def start(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return
        self._running = True

    @override
    async def stop(self) -> None:
        self._running = False

    @override
    def is_ready(self) -> bool:
        return (
            self._running
            and self._emotion_batcher.is_ready()
            and self._face_batcher.is_ready()
        )

    @override
    async def update(self, input: cv2t.MatLike) -> EmotionDetection | None:
        """Detect faces, classify emotions, filter by threshold."""
        cur_ts: float = time.time()
        if cur_ts - self._last_update_ts < self._config.frame_interval:
            return self._last_prediction

        face_futures = await self._face_batcher.submit([input])
        face_raw: RawFaceDetection = await face_futures[0]

        face_crops: list[FaceCrop] = FaceDetector.extract_crops_from_raw(
            [input], [face_raw]
        )[0]

        if not face_crops:
            self._last_prediction = EmotionDetection(emotions=[])
            self._last_update_ts = cur_ts
            return self._last_prediction

        crops: list[cv2t.MatLike] = [fc.crop for fc in face_crops]
        emotion_futures = await self._emotion_batcher.submit(crops)
        raw_detections: list[RawEmotionDetection] = [await f for f in emotion_futures]

        recognizer = cast(EmotionRecognizer, self._emotion_batcher.predictor)

        # A Neutral fallback from per-label gating bypasses confidence_threshold.
        emotions: list[Emotion] = []
        for face_crop, raw in zip(face_crops, raw_detections):
            resolved = resolve_label(
                raw.expression_probs,
                recognizer.class_names,
                self._config.label_thresholds,
            )

            if not resolved.is_fallback and resolved.confidence < self._config.confidence_threshold:
                continue

            emotions.append(
                Emotion(
                    emotion=resolved.label,
                    confidence=resolved.confidence,
                    face_confidence=face_crop.confidence,
                    bbox=face_crop.bbox_xyxy,
                    valence=raw.valence,
                    arousal=raw.arousal,
                )
            )

        self._last_prediction = EmotionDetection(emotions=emotions)
        self._last_update_ts = cur_ts

        if emotions:
            self._logger.info(
                "[session %s] Detected %d face(s): %s",
                self._session_id,
                len(emotions),
                ", ".join(f"{e.emotion} ({e.confidence:.2f})" for e in emotions),
            )

        return self._last_prediction

    @override
    def update_config(
        self,
        *,
        confidence_threshold: float | Omit = omit,
        frame_interval: float | Omit = omit,
        **kwargs: Any,
    ) -> None:
        super().update_config(
            confidence_threshold=confidence_threshold,
            frame_interval=frame_interval,
        )

    @override
    def _post_config_update(self) -> None:
        self._logger.info(
            "[session %s] Config updated — threshold=%.2f, frame_interval=%.2f",
            self._session_id,
            self._config.confidence_threshold,
            self._config.frame_interval,
        )
