"""Object detection pipeline: model lifecycle and session management."""

import asyncio

from typing_extensions import override

from core.models.object import (
    ObjectDetection,
    ObjectDetectionItem,
    ObjectPerceptionSessionConfig,
    RawObjectDetection,
)
from core.perception.base import PerceptionBase
from core.perception.base.batching import InputBatcher
from core.perception.object.predictors.base import ObjectDetector
from core.perception.object.session import ObjectPerceptionSession
from core.perception.object.utils import ObjectDetectorFactory

import cv2.typing as cv2t


class ObjectPerception(PerceptionBase[ObjectPerceptionSession]):
    """Object detection pipeline for a single detector. Loaded once, shared by all WS sessions."""

    def __init__(
        self,
        object_detector_factory: ObjectDetectorFactory,
        default_config: ObjectPerceptionSessionConfig | None = None,
        batch_size: int | None = None,
        batch_timeout: float | None = None,
    ) -> None:
        super().__init__()

        self._object_detector_factory: ObjectDetectorFactory = object_detector_factory
        self._default_config: ObjectPerceptionSessionConfig | None = default_config

        self._batch_size: int | None = batch_size
        self._batch_timeout: float | None = batch_timeout

        self._object_detector: ObjectDetector | None = None
        self._batcher: InputBatcher[cv2t.MatLike, RawObjectDetection] | None = None
        self._running: bool = False

    @override
    async def start(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._object_detector = self._object_detector_factory.create()
        await asyncio.to_thread(self._object_detector.start)

        self._batcher = InputBatcher(
            self._object_detector,
            batch_size=self._batch_size,
            batch_timeout=self._batch_timeout,
        )
        await self._batcher.start()

        self._running = True
        self._logger.info("Ready")

    @override
    async def stop(self) -> None:
        if self._batcher is not None:
            await self._batcher.stop()
            self._batcher = None

        if self._object_detector is not None:
            await asyncio.to_thread(self._object_detector.stop)
            self._object_detector = None

        self._running = False
        self._logger.info("Stopped")

    @override
    def is_ready(self) -> bool:
        if not self._running or self._batcher is None:
            return False
        return self._batcher.is_ready()

    @override
    async def create_session(self) -> ObjectPerceptionSession:
        if self._batcher is None:
            raise RuntimeError("ObjectPerception not started")

        config = self._default_config or ObjectPerceptionSession.DEFAULT_CONFIG
        return ObjectPerceptionSession(
            batcher=self._batcher,
            config=config,
        )


    async def predict_image(
        self,
        image: cv2t.MatLike,
        classes: list[str] | None = None,
    ) -> ObjectDetection:
        """Detect objects in a single image."""
        if self._batcher is None:
            raise RuntimeError("ObjectPerception not started")

        H, W = image.shape[:2]

        kwargs = {}
        if classes is not None:
            kwargs["classes"] = classes

        futures = await self._batcher.submit([image], **kwargs)
        raw: RawObjectDetection = await futures[0]

        # Rescale [0,1] -> pixel xywh
        detections: list[ObjectDetectionItem] = []
        for i in range(len(raw.class_names)):
            xywh = raw.bbox_xywh[i].copy()
            xywh[0] *= W
            xywh[1] *= H
            xywh[2] *= W
            xywh[3] *= H
            detections.append(ObjectDetectionItem(
                class_name=raw.class_names[i],
                xywh=xywh.tolist(),
                confidence=float(raw.confidence[i]),
            ))

        return ObjectDetection(detections=detections)
