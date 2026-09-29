"""Defaults & label vocabulary for speech emotion recognition."""

from __future__ import annotations

from enum import Enum


DEFAULT_DL_SER_ENDPOINT: str = "/hal/api/dl/ser/recognize"
DEFAULT_API_TIMEOUT_S: float = 15.0

class SpeechEmotionLabel(str, Enum):
    ANGRY = "angry"
    DISGUSTED = "disgusted"
    FEARFUL = "fearful"
    HAPPY = "happy"
    NEUTRAL = "neutral"
    OTHER = "other"
    SAD = "sad"
    SURPRISED = "surprised"
    UNK = "<unk>"

    @classmethod
    def _missing_(cls, value: str) -> SpeechEmotionLabel:
        return cls.UNK


DEFAULT_MIN_AUDIO_S: float = 3.0

CONFIDENCE_THRESHOLD_BY_LABEL: dict[str, float] = {
    SpeechEmotionLabel.HAPPY:     0.5,
    SpeechEmotionLabel.SURPRISED: 0.6,
    SpeechEmotionLabel.SAD:       0.7,
    SpeechEmotionLabel.ANGRY:     0.6,
    SpeechEmotionLabel.FEARFUL:   0.6,
    SpeechEmotionLabel.DISGUSTED: 0.6,
}
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.5


DEFAULT_FLUSH_S: float = 10.0
DEFAULT_DEDUP_WINDOW_S: float = 300.0

DEFAULT_QUEUE_MAXSIZE: int = 8
DEFAULT_JOB_MAX_AGE_S: float = 30.0

DEFAULT_AUDIO_MAX_FILES: int = 200


LABEL_BUCKETS: dict[str, str] = {
    SpeechEmotionLabel.HAPPY:     "positive",
    SpeechEmotionLabel.SURPRISED: "positive",
    SpeechEmotionLabel.ANGRY:     "negative",
    SpeechEmotionLabel.DISGUSTED: "negative",
    SpeechEmotionLabel.FEARFUL:   "negative",
    SpeechEmotionLabel.SAD:       "negative",
}

NEUTRAL_LABELS: frozenset = frozenset(
    {SpeechEmotionLabel.NEUTRAL, SpeechEmotionLabel.OTHER, SpeechEmotionLabel.UNK,
     "unk", ""}
)

HEDGE_BY_BUCKET: dict[str, str] = {
    "positive": "do not over-celebrate",
    "negative": "do not assume the user is distressed",
    "other": "do not over-react",
}


SENSING_EVENT_TYPE: str = "speech_emotion.detected"
UNKNOWN_USER_LABEL: str = "unknown"


PREFILTER_SAMPLE_RATE: int = 16000
PREFILTER_FRAME_MS: int = 20
PREFILTER_PAD_MS: int = 100

PREFILTER_TRIM_RMS: float = 3500.0
PREFILTER_VOICED_RMS: float = 2500.0
PREFILTER_MIN_TRIMMED_S: float = 2.0
PREFILTER_MIN_VOICED_S: float = 1.0
PREFILTER_MIN_VOICED_RATIO: float = 0.3

# Stage 2 — Silero VAD
PREFILTER_SILERO_THRESHOLD: float = 0.5
PREFILTER_VAD_MIN_VOICED_S: float = 1.0
PREFILTER_VAD_FALLBACK_MIN_VOICED_S: float = 3.0
PREFILTER_SILERO_CHUNK_SAMPLES: int = 512
PREFILTER_SILERO_CONTEXT_SAMPLES: int = 64

# emotion2vec was trained on short utterances (acted corpora: 2.4 s median, 7.1 s max)
# and grows confidently wrong on longer input. The server also bounds SER input to 2-8 s
# (perception-service #492).
SER_MAX_CLIP_S: float = 8.0
