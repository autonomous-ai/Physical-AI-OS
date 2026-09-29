"""Action analysis: model lifecycle, optional person detection, and session creation."""

import asyncio

from typing_extensions import override

from core.models.action import ActionPerceptionSessionConfig, RawHumanActionDetection
from core.models.media import Video
from core.perception.action.predictors.base import HumanActionRecognizer
from core.perception.action.session import ActionPerceptionSession
from core.perception.action.utils import ActionRecognizerFactory
from core.perception.base import PerceptionBase
from core.perception.base.batching import InputBatcher
from core.models.person import RawPersonDetection
from core.perception.person.predictors import PersonDetector
from core.perception.person.utils import PersonDetectorFactory

import cv2.typing as cv2t


class ActionPerception(PerceptionBase[ActionPerceptionSession]):
    """Action recognition pipeline. Loaded once, shared by all WS sessions."""

    def __init__(
        self,
        action_recognizer_factory: ActionRecognizerFactory,
        person_detector_factory: PersonDetectorFactory | None = None,
        default_config: ActionPerceptionSessionConfig | None = None,
        batch_size: int | None = None,
        batch_timeout: float | None = None,
    ):
        super().__init__()

        self._action_recognizer_factory: ActionRecognizerFactory = action_recognizer_factory
        self._person_detector_factory: PersonDetectorFactory | None = person_detector_factory
        self._default_config: ActionPerceptionSessionConfig | None = default_config

        self._batch_size: int | None = batch_size
        self._batch_timeout: float | None = batch_timeout

        self._action_recognizer: HumanActionRecognizer | None = None
        self._person_detector: PersonDetector | None = None
        self._action_batcher: InputBatcher[Video, RawHumanActionDetection] | None = None
        self._person_batcher: InputBatcher[cv2t.MatLike, RawPersonDetection] | None = None
        self._running: bool = False

    @override
    async def start(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._action_recognizer = self._action_recognizer_factory.create()
        await asyncio.to_thread(self._action_recognizer.start)

        self._action_batcher = InputBatcher(self._action_recognizer, batch_size=self._batch_size, batch_timeout=self._batch_timeout)
        await self._action_batcher.start()

        if self._person_detector_factory is not None:
            self._person_detector = self._person_detector_factory.create()
            await asyncio.to_thread(self._person_detector.start)

            self._person_batcher = InputBatcher(self._person_detector, batch_size=self._batch_size, batch_timeout=self._batch_timeout)
            await self._person_batcher.start()

        self._running = True
        self._logger.info("Ready")

    @override
    async def stop(self) -> None:
        if self._action_batcher is not None:
            await self._action_batcher.stop()
            self._action_batcher = None

        if self._person_batcher is not None:
            await self._person_batcher.stop()
            self._person_batcher = None

        if self._action_recognizer is not None:
            await asyncio.to_thread(self._action_recognizer.stop)
            self._action_recognizer = None

        if self._person_detector is not None:
            await asyncio.to_thread(self._person_detector.stop)
            self._person_detector = None

        self._running = False
        self._logger.info("Stopped")

    @override
    def is_ready(self) -> bool:
        if not self._running or self._action_batcher is None:
            return False
        if not self._action_batcher.is_ready():
            return False
        return True

    @override
    async def create_session(self) -> ActionPerceptionSession:
        if self._action_batcher is None:
            raise RuntimeError("ActionPerception not started")

        config = self._default_config or ActionPerceptionSession.DEFAULT_CONFIG
        return ActionPerceptionSession(
            action_batcher=self._action_batcher,
            person_batcher=self._person_batcher,
            config=config,
        )
