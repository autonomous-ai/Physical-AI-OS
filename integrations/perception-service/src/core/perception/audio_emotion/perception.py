"""Audio emotion perception: model lifecycle, sessions, and single-shot predict_audio for HTTP."""

import asyncio
from typing import cast

from typing_extensions import override

from core.models.audio_emotion import (
    AudioEmotion,
    AudioEmotionDetection,
    AudioEmotionPerceptionSessionConfig,
    RawAudioEmotionDetection,
)
from core.models.media import Audio
from core.perception.audio_emotion.predictors.base import AudioEmotionRecognizer
from core.perception.audio_emotion.session import AudioEmotionPerceptionSession
from core.perception.audio_emotion.utils import AudioEmotionRecognizerFactory
from core.perception.base import PerceptionBase
from core.perception.base.batching import InputBatcher


class AudioEmotionPerception(PerceptionBase[AudioEmotionPerceptionSession]):
    """Audio emotion detection pipeline. Loaded once, shared by all sessions."""

    def __init__(
        self,
        audio_emotion_recognizer_factory: AudioEmotionRecognizerFactory,
        default_config: AudioEmotionPerceptionSessionConfig | None = None,
        batch_size: int | None = None,
        batch_timeout: float | None = None,
    ) -> None:
        super().__init__()

        self._audio_emotion_recognizer_factory: AudioEmotionRecognizerFactory = (
            audio_emotion_recognizer_factory
        )
        self._default_config: AudioEmotionPerceptionSessionConfig | None = default_config

        self._batch_size: int | None = batch_size
        self._batch_timeout: float | None = batch_timeout

        self._audio_emotion_recognizer: AudioEmotionRecognizer | None = None
        self._batcher: InputBatcher[Audio, RawAudioEmotionDetection] | None = None
        self._running: bool = False

    @property
    def labels(self) -> list[str]:
        if self._audio_emotion_recognizer is None:
            return []
        return self._audio_emotion_recognizer.class_names

    @property
    def engine_name(self) -> str:
        if self._audio_emotion_recognizer is None:
            return ""
        return type(self._audio_emotion_recognizer).__name__

    @override
    async def start(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._audio_emotion_recognizer = self._audio_emotion_recognizer_factory.create()
        await asyncio.to_thread(self._audio_emotion_recognizer.start)

        self._batcher = InputBatcher(
            self._audio_emotion_recognizer,
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

        if self._audio_emotion_recognizer is not None:
            await asyncio.to_thread(self._audio_emotion_recognizer.stop)
            self._audio_emotion_recognizer = None

        self._running = False
        self._logger.info("Stopped")

    @override
    def is_ready(self) -> bool:
        return (
            self._running
            and self._batcher is not None
            and self._batcher.is_ready()
        )

    @override
    async def create_session(self) -> AudioEmotionPerceptionSession:
        if self._batcher is None:
            raise RuntimeError("AudioEmotionPerception not started")

        config: AudioEmotionPerceptionSessionConfig = (
            self._default_config or AudioEmotionPerceptionSession.DEFAULT_CONFIG
        )
        return AudioEmotionPerceptionSession(
            batcher=self._batcher,
            config=config,
        )

    async def predict_audio(self, audio: Audio) -> AudioEmotionDetection:
        """Classify emotion from a single audio utterance."""
        if self._batcher is None:
            raise RuntimeError("AudioEmotionPerception not started")

        recognizer = cast(AudioEmotionRecognizer, self._batcher.predictor)
        futures = await self._batcher.submit([audio])
        raw: RawAudioEmotionDetection = await futures[0]

        class_names: list[str] = recognizer.class_names

        emotions: list[AudioEmotion] = [
            AudioEmotion(
                emotion=class_names[i],
                confidence=float(raw.expression_probs[i]),
            )
            for i in range(len(class_names))
        ]
        emotions.sort(key=lambda e: e.confidence, reverse=True)

        return AudioEmotionDetection(emotions=emotions)
