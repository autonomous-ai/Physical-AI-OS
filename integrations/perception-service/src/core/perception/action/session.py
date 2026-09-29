import asyncio
import time
from collections import deque
from typing import Any, cast

import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
from typing_extensions import override

from core.models.action import (
    ActionPerceptionSessionConfig,
    HumanAction,
    HumanActionDetection,
    RawHumanActionDetection,
)
from core.models.media import Video
from core.models.person import RawPersonDetection
from core.perception.action.predictors import HumanActionRecognizer
from core.perception.base import PerceptionSessionBase
from core.perception.base.batching import InputBatcher
from core.perception.person.predictors import PersonDetector
from core.types import Omit, omit
from core.utils.common import get_or_default


class ActionPerceptionSession(
    PerceptionSessionBase[
        cv2t.MatLike,
        HumanActionDetection,
        ActionPerceptionSessionConfig,
    ]
):
    DEFAULT_CONFIG: ActionPerceptionSessionConfig = ActionPerceptionSessionConfig()

    def __init__(
        self,
        action_batcher: InputBatcher[Video, RawHumanActionDetection],
        person_batcher: InputBatcher[cv2t.MatLike, RawPersonDetection] | None,
        config: ActionPerceptionSessionConfig | None = None,
    ) -> None:
        config = get_or_default(config, ActionPerceptionSessionConfig())
        super().__init__(config)

        self._action_batcher: InputBatcher[Video, RawHumanActionDetection] = action_batcher
        self._person_batcher: InputBatcher[cv2t.MatLike, RawPersonDetection] | None = person_batcher

        action_recognizer = cast(HumanActionRecognizer, self._action_batcher.predictor)
        self._class_mask: npt.NDArray[np.bool_] = action_recognizer.default_class_mask.copy()
        self._frame_buffer: deque[cv2t.MatLike] = deque()

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
        if not self._action_batcher.is_ready():
            return False
        return self._running

    @override
    async def update(self, input: cv2t.MatLike) -> HumanActionDetection | None:
        """Buffer a frame and optionally run inference.

        Returns classes above threshold; empty when person detection finds no person.
        """
        cur_ts: float = time.time()
        if cur_ts - self._last_update_ts >= self._config.frame_interval:
            if self._person_batcher is not None and self._config.person_detection_enabled:
                person_detector = cast(PersonDetector, self._person_batcher.predictor)
                person_futures = await self._person_batcher.submit([input])
                person_raw: RawPersonDetection = await person_futures[0]
                crops = person_detector.extract_largest_crop_from_raw(
                    [input], [person_raw], self._config.person_min_area_ratio,
                )
                crop = crops[0]

                if crop is None:
                    return HumanActionDetection(actions=[])

                input = crop

            action_recognizer = cast(HumanActionRecognizer, self._action_batcher.predictor)
            preprocessed_input = await asyncio.to_thread(
                action_recognizer.preprocess_single_frame, input,
            )

            self._frame_buffer.append(preprocessed_input)
            while len(self._frame_buffer) > action_recognizer.max_frames:
                _ = self._frame_buffer.popleft()

            futures = await self._action_batcher.submit(
                [Video(frames=list(self._frame_buffer))],
                preprocess=False,
                class_mask=self._class_mask,
            )
            raw_prediction: RawHumanActionDetection = await futures[0]

            action_ids = np.where(raw_prediction.prob_np > self._config.threshold)[0]

            detected_actions = [
                HumanAction(
                    class_name=action_recognizer.class_names[i],
                    conf=raw_prediction.prob_np[i].item(),
                )
                for i in action_ids
            ]
            detected_actions = sorted(detected_actions, key=lambda x: x.conf, reverse=True)

            self._last_prediction = HumanActionDetection(actions=detected_actions)
            self._last_update_ts = cur_ts

        if self._last_prediction is not None:
            detected_actions = self._last_prediction.actions
            if detected_actions:
                self._logger.info(
                    "[session %s] Detected top-%d: %s",
                    self._session_id,
                    min(3, len(detected_actions)),
                    ", ".join(f"{d.class_name} ({d.conf:.2f})" for d in detected_actions[:3]),
                )

        return self._last_prediction

    @override
    def update_config(
        self,
        *,
        frame_interval: float | Omit = omit,
        whitelist: list[str] | None | Omit = omit,
        threshold: float | Omit = omit,
        person_detection_enabled: bool | None | Omit = omit,
        person_min_area_ratio: float | Omit = omit,
        **kwargs: Any,
    ) -> None:
        super().update_config(
            frame_interval=frame_interval,
            whitelist=whitelist,
            threshold=threshold,
            person_detection_enabled=person_detection_enabled,
            person_min_area_ratio=person_min_area_ratio,
        )

    @override
    def _post_config_update(self) -> None:
        action_recognizer = cast(HumanActionRecognizer, self._action_batcher.predictor)
        if self._config.whitelist is None:
            self._class_mask = action_recognizer.default_class_mask.copy()
        else:
            allowed: set[str] = set(self._config.whitelist)
            self._class_mask = np.array(
                [name in allowed for name in action_recognizer.class_names], dtype=np.bool_
            )

        self._logger.info(
            "[session %s] Config updated — %d classes enabled, threshold=%f",
            self._session_id,
            int(self._class_mask.sum()),
            round(self._config.threshold, 2),
        )
