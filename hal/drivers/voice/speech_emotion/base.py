"""Abstract speech emotion recognizer."""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(slots=True)
class SpeechEmotionResult:
    """One classifier output for one utterance."""

    label: str
    confidence: float


class BaseSpeechEmotionRecognizer(abc.ABC):
    """Engine interface — stateless, one call per utterance."""

    @property
    @abc.abstractmethod
    def available(self) -> bool:
        """False if the engine is not configured (missing URL, key, …)."""

    @abc.abstractmethod
    def recognize(self, wav_bytes: bytes) -> SpeechEmotionResult | None:
        """Classify one utterance. Return None on transport/parse failure."""
