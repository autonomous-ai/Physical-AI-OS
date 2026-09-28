"""Per-connection audio emotion detection session."""

from typing import Any, cast

import numpy as np
from typing_extensions import override

from core.models.audio_emotion import (
    AudioEmotion,
    AudioEmotionDetection,
    AudioEmotionPerceptionSessionConfig,
    RawAudioEmotionDetection,
)
from core.models.media import Audio
from core.perception.audio_emotion.predictors.base import AudioEmotionRecognizer
from core.perception.base import PerceptionSessionBase
from core.perception.base.batching import InputBatcher
from core.types import Omit, omit
from core.utils.common import get_or_default


class AudioEmotionPerceptionSession(
    PerceptionSessionBase[
        Audio,
        AudioEmotionDetection,
        AudioEmotionPerceptionSessionConfig,
    ]
):
    """Per-connection session for audio emotion detection."""

    DEFAULT_CONFIG: AudioEmotionPerceptionSessionConfig = AudioEmotionPerceptionSessionConfig()

    def __init__(
        self,
        batcher: InputBatcher[Audio, RawAudioEmotionDetection],
        config: AudioEmotionPerceptionSessionConfig | None = None,
    ) -> None:
        config = get_or_default(config, AudioEmotionPerceptionSessionConfig())
        super().__init__(config)
        self._batcher: InputBatcher[Audio, RawAudioEmotionDetection] = batcher
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
    async def update(self, input: Audio) -> AudioEmotionDetection | None:
        """Classify emotion from audio, filter by threshold."""
        futures = await self._batcher.submit([input])
        raw: RawAudioEmotionDetection = await futures[0]

        recognizer = cast(AudioEmotionRecognizer, self._batcher.predictor)
        class_names: list[str] = recognizer.class_names

        indices = np.where(raw.expression_probs >= self._config.confidence_threshold)[0]
        emotions: list[AudioEmotion] = [
            AudioEmotion(
                emotion=class_names[i],
                confidence=float(raw.expression_probs[i]),
            )
            for i in indices
        ]
        emotions.sort(key=lambda e: e.confidence, reverse=True)

        self._last_prediction = AudioEmotionDetection(emotions=emotions)

        if emotions:
            self._logger.info(
                "[session %s] Detected: %s",
                self._session_id,
                ", ".join(f"{e.emotion} ({e.confidence:.2f})" for e in emotions),
            )

        return self._last_prediction

    @override
    def update_config(
        self,
        *,
        confidence_threshold: float | Omit = omit,
        **kwargs: Any,
    ) -> None:
        super().update_config(confidence_threshold=confidence_threshold)

    @override
    def _post_config_update(self) -> None:
        self._logger.info(
            "[session %s] Config updated — threshold=%.2f",
            self._session_id,
            self._config.confidence_threshold,
        )
