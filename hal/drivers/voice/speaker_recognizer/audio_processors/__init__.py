"""On-device audio preprocessing pipeline for speaker recognition."""

from .base import Audio, AudioProcessorBase
from .composite import CompositeAudioProcessor
from .exceptions import (
    REJECT_EMPTY_INPUT,
    REJECT_LOW_INTELLIGIBILITY,
    REJECT_LOW_VOICE_RATIO,
    REJECT_TOO_SHORT,
    REJECT_VAD_REMOVED_ALL,
    PreprocessRejected,
)
from .factory import AudioProcessorFactory
from .stoi_filter import SpeechIntelligibilityFilter

__all__ = [
    "Audio",
    "AudioProcessorBase",
    "AudioProcessorFactory",
    "CompositeAudioProcessor",
    "SpeechIntelligibilityFilter",
    "PreprocessRejected",
    "REJECT_EMPTY_INPUT",
    "REJECT_VAD_REMOVED_ALL",
    "REJECT_TOO_SHORT",
    "REJECT_LOW_VOICE_RATIO",
    "REJECT_LOW_INTELLIGIBILITY",
]
