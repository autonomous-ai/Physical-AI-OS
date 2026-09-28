"""Emotion perception: model lifecycle, session management, and single-shot prediction.

Wraps an EmotionRecognizer + FaceDetector behind InputBatchers.
Each WebSocket connection creates an EmotionPerceptionSession via create_session().
Single-shot methods (predict_face, predict_image) are provided for HTTP endpoints.
"""

import asyncio
from typing import cast

import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
from typing_extensions import override

from core.models.face import FaceCrop, RawFaceDetection
from core.models.facial_emotion import (
    Emotion,
    EmotionDetection,
    EmotionPerceptionSessionConfig,
    RawEmotionDetection,
)
from core.perception.base import PerceptionBase
from core.perception.base.batching import InputBatcher
from core.perception.face.predictors.base import FaceDetector
from core.perception.face.utils import FaceDetectorFactory
from core.perception.facial_emotion.label_gating import resolve_label
from core.perception.facial_emotion.predictors.base import EmotionRecognizer
from core.perception.facial_emotion.session import EmotionPerceptionSession
from core.perception.facial_emotion.utils import EmotionRecognizerFactory


class EmotionPerception(PerceptionBase[EmotionPerceptionSession]):
    """Emotion detection pipeline. Loaded once, shared by all WS sessions."""

    def __init__(
        self,
        emotion_recognizer_factory: EmotionRecognizerFactory,
        face_detector_factory: FaceDetectorFactory,
        default_config: EmotionPerceptionSessionConfig | None = None,
        batch_size: int | None = None,
        batch_timeout: float | None = None,
    ) -> None:
        super().__init__()

        self._emotion_recognizer_factory: EmotionRecognizerFactory = emotion_recognizer_factory
        self._face_detector_factory: FaceDetectorFactory = face_detector_factory
        self._default_config: EmotionPerceptionSessionConfig | None = default_config

        self._batch_size: int | None = batch_size
        self._batch_timeout: float | None = batch_timeout

        self._emotion_recognizer: EmotionRecognizer | None = None
        self._face_detector: FaceDetector | None = None
        self._emotion_batcher: InputBatcher[cv2t.MatLike, RawEmotionDetection] | None = None
        self._face_batcher: InputBatcher[cv2t.MatLike, RawFaceDetection] | None = None
        self._running: bool = False

    @property
    def labels(self) -> list[str]:
        if self._emotion_recognizer is None:
            return []
        return self._emotion_recognizer.class_names

    @override
    async def start(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._emotion_recognizer = self._emotion_recognizer_factory.create()
        await asyncio.to_thread(self._emotion_recognizer.start)

        self._face_detector = self._face_detector_factory.create()
        await asyncio.to_thread(self._face_detector.start)

        self._emotion_batcher = InputBatcher(self._emotion_recognizer, batch_size=self._batch_size, batch_timeout=self._batch_timeout)
        await self._emotion_batcher.start()

        self._face_batcher = InputBatcher(self._face_detector, batch_size=self._batch_size, batch_timeout=self._batch_timeout)
        await self._face_batcher.start()

        self._running = True
        self._logger.info("Ready")

    @override
    async def stop(self) -> None:
        if self._emotion_batcher is not None:
            await self._emotion_batcher.stop()
            self._emotion_batcher = None

        if self._face_batcher is not None:
            await self._face_batcher.stop()
            self._face_batcher = None

        if self._emotion_recognizer is not None:
            await asyncio.to_thread(self._emotion_recognizer.stop)
            self._emotion_recognizer = None

        if self._face_detector is not None:
            await asyncio.to_thread(self._face_detector.stop)
            self._face_detector = None

        self._running = False
        self._logger.info("Stopped")

    @override
    def is_ready(self) -> bool:
        return (
            self._running
            and self._emotion_batcher is not None
            and self._emotion_batcher.is_ready()
            and self._face_batcher is not None
            and self._face_batcher.is_ready()
        )

    @override
    async def create_session(self) -> EmotionPerceptionSession:
        if self._emotion_batcher is None or self._face_batcher is None:
            raise RuntimeError("EmotionPerception not started")

        config: EmotionPerceptionSessionConfig = (
            self._default_config or EmotionPerceptionSession.DEFAULT_CONFIG
        )
        return EmotionPerceptionSession(
            emotion_batcher=self._emotion_batcher,
            face_batcher=self._face_batcher,
            config=config,
        )

    # --- Single-shot prediction (for HTTP endpoints) ---

    @property
    def _label_thresholds(self) -> dict[str, float] | None:
        return self._default_config.label_thresholds if self._default_config else None

    async def predict_face(self, face_crop: cv2t.MatLike, *, gate: bool = True) -> Emotion | None:
        """Classify emotion from a single pre-cropped face image.

        ``gate=False`` skips the per-label gate and returns the raw argmax, for
        clients (HAL) that apply their own gate from ``probabilities``.
        """
        if self._emotion_batcher is None:
            raise RuntimeError("EmotionPerception not started")

        recognizer = cast(EmotionRecognizer, self._emotion_batcher.predictor)

        futures = await self._emotion_batcher.submit([face_crop])
        raw: RawEmotionDetection = await futures[0]

        probabilities: dict[str, float] = {
            name: float(p) for name, p in zip(recognizer.class_names, raw.expression_probs)
        }
        if gate:
            resolved = resolve_label(
                raw.expression_probs, recognizer.class_names, self._label_thresholds
            )
            label, confidence = resolved.label, resolved.confidence
        else:
            idx = int(np.argmax(raw.expression_probs))
            label, confidence = recognizer.class_names[idx], float(raw.expression_probs[idx])
        H, W = face_crop.shape[:2]

        return Emotion(
            emotion=label,
            confidence=confidence,
            face_confidence=1.0,
            bbox=[0, 0, W, H],
            valence=raw.valence,
            arousal=raw.arousal,
            probabilities=probabilities,
        )

    async def predict_image(self, frame: npt.NDArray[np.uint8]) -> EmotionDetection:
        """Detect faces in a full frame and classify emotion for each."""
        if self._face_batcher is None or self._emotion_batcher is None:
            raise RuntimeError("EmotionPerception not started")

        recognizer = cast(EmotionRecognizer, self._emotion_batcher.predictor)

        # Face detection through batcher
        face_futures = await self._face_batcher.submit([frame])
        face_raw: RawFaceDetection = await face_futures[0]
        face_crops: list[FaceCrop] = FaceDetector.extract_crops_from_raw(
            [frame], [face_raw]
        )[0]

        if not face_crops:
            return EmotionDetection(emotions=[])

        # Emotion classification through batcher
        crops: list[cv2t.MatLike] = [fc.crop for fc in face_crops]
        emotion_futures = await self._emotion_batcher.submit(crops)
        raw_results: list[RawEmotionDetection] = [await f for f in emotion_futures]

        emotions: list[Emotion] = []
        for fc, raw in zip(face_crops, raw_results):
            resolved = resolve_label(
                raw.expression_probs, recognizer.class_names, self._label_thresholds
            )
            emotions.append(
                Emotion(
                    emotion=resolved.label,
                    confidence=resolved.confidence,
                    face_confidence=fc.confidence,
                    bbox=fc.bbox_xyxy,
                    valence=raw.valence,
                    arousal=raw.arousal,
                )
            )

        return EmotionDetection(emotions=emotions)
