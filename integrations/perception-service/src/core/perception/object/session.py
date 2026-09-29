"""Per-connection object detection session."""

import time
from typing import Any

import cv2.typing as cv2t
from typing_extensions import override

from core.models.object import (
    ObjectDetection,
    ObjectDetectionItem,
    ObjectPerceptionSessionConfig,
    RawObjectDetection,
)
from core.perception.base import PerceptionSessionBase
from core.perception.base.batching import InputBatcher
from core.types import Omit, omit
from core.utils.common import get_or_default


class ObjectPerceptionSession(
    PerceptionSessionBase[
        cv2t.MatLike,
        ObjectDetection,
        ObjectPerceptionSessionConfig,
    ]
):
    DEFAULT_CONFIG: ObjectPerceptionSessionConfig = ObjectPerceptionSessionConfig()

    def __init__(
        self,
        batcher: InputBatcher[cv2t.MatLike, RawObjectDetection],
        config: ObjectPerceptionSessionConfig | None = None,
    ) -> None:
        config = get_or_default(config, ObjectPerceptionSessionConfig())
        super().__init__(config)

        self._batcher: InputBatcher[cv2t.MatLike, RawObjectDetection] = batcher
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
        return self._running and self._batcher.is_ready()

    @override
    async def update(self, input: cv2t.MatLike) -> ObjectDetection | None:
        """Run object detection on a single frame; None if rate-limited."""
        cur_ts: float = time.time()
        if cur_ts - self._last_update_ts < self._config.frame_interval:
            return self._last_prediction

        H, W = input.shape[:2]

        kwargs: dict[str, Any] = {}
        if self._config.classes is not None:
            kwargs["classes"] = self._config.classes

        futures = await self._batcher.submit([input], **kwargs)
        raw: RawObjectDetection = await futures[0]

        # Filter by threshold, rescale [0,1] -> pixel xywh
        detections: list[ObjectDetectionItem] = []
        for i in range(len(raw.class_names)):
            if raw.confidence[i] >= self._config.threshold:
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

        result: ObjectDetection = ObjectDetection(detections=detections)

        self._last_update_ts = cur_ts
        self._last_prediction = result

        if detections:
            self._logger.info(
                "[session %s] Detected %d objects: %s",
                self._session_id,
                len(detections),
                ", ".join(f"{d.class_name} ({d.confidence:.2f})" for d in detections[:5]),
            )

        return result

    @override
    def update_config(
        self,
        *,
        frame_interval: float | Omit = omit,
        classes: list[str] | None | Omit = omit,
        threshold: float | Omit = omit,
        **kwargs: Any,
    ) -> None:
        super().update_config(
            frame_interval=frame_interval,
            classes=classes,
            threshold=threshold,
        )

    @override
    def _post_config_update(self) -> None:
        self._logger.info(
            "[session %s] Config updated — frame_interval=%.2f, threshold=%.2f, classes=%s",
            self._session_id,
            self._config.frame_interval,
            self._config.threshold,
            len(self._config.classes) if self._config.classes else "default",
        )
