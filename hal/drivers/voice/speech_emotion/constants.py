"""Defaults & label vocabulary for speech emotion recognition.

Bucketing mirrors the face emotion processor so downstream skills/dedup
share the same polarity language. emotion2vec_plus_large labels (from
perception-service `/api/dl/ser/labels`) are enumerated by `SpeechEmotionLabel`
below.
"""

from __future__ import annotations

from enum import Enum

# --- API contract ---------------------------------------------------------

DEFAULT_DL_SER_ENDPOINT: str = "/hal/api/dl/ser/recognize"
DEFAULT_API_TIMEOUT_S: float = 15.0

# --- Label vocabulary ----------------------------------------------------
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

# --- Input gating ---------------------------------------------------------

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

# --- Buffering / dedup ---------------------------------------------------

DEFAULT_FLUSH_S: float = 10.0
DEFAULT_DEDUP_WINDOW_S: float = 300.0

# A deep queue serves no purpose here: output is capped at one event per user
# per bucket per DEDUP_WINDOW_S, so a long backlog can only ever produce stale
# readings. Kept small enough that the whole queue is worth less wall-clock
# than one dedup window, and small enough to bound retained WAV memory.
DEFAULT_QUEUE_MAXSIZE: int = 8
# Audio older than this is no longer "how the user feels now" — recognizing it
# would emit a stale mood and then let the dedup suppress the current one.
DEFAULT_JOB_MAX_AGE_S: float = 30.0

# --- Debug audio retention ------------------------------------------------
# SPEECH_EMOTION_AUDIO_DIR defaults under /tmp, which is tmpfs (RAM) on the
# target images, and only one clip per flush is ever referenced by the Flow
# Monitor — the rest are orphaned the moment they are written. Cap the
# directory so a talkative day cannot exhaust RAM and take every other tmpfs
# writer down with it (including the dedup sidecar at /tmp/hal-ser-state.json).
DEFAULT_AUDIO_MAX_FILES: int = 200  # 0 = unbounded

# --- Polarity buckets -----------------------------------------------------
# Matches face emotion processor's EMOTION_BUCKETS shape so (user, bucket)
# dedup keys are interpretable across modalities.

LABEL_BUCKETS: dict[str, str] = {
    SpeechEmotionLabel.HAPPY:     "positive",
    SpeechEmotionLabel.SURPRISED: "positive",
    SpeechEmotionLabel.ANGRY:     "negative",
    SpeechEmotionLabel.DISGUSTED: "negative",
    SpeechEmotionLabel.FEARFUL:   "negative",
    SpeechEmotionLabel.SAD:       "negative",
    # Anything not in the map collapses to "other" via utils.bucket_for().
}

# Bare "unk" and "" stay as raw-string defensive fallbacks so is_neutral()
# still answers True if a caller bypasses the SpeechEmotionLabel coercion
# in _process_job and hands us a raw model string.
NEUTRAL_LABELS: frozenset = frozenset(
    {SpeechEmotionLabel.NEUTRAL, SpeechEmotionLabel.OTHER, SpeechEmotionLabel.UNK,
     "unk", ""}
)

HEDGE_BY_BUCKET: dict[str, str] = {
    "positive": "do not over-celebrate",
    "negative": "do not assume the user is distressed",
    "other": "do not over-react",
}

# --- Wire format ----------------------------------------------------------

SENSING_EVENT_TYPE: str = "speech_emotion.detected"
UNKNOWN_USER_LABEL: str = "unknown"

# --- Prefilter: anti-sparse-audio --------------------------------------------
# Two-tier gate run by the engine before recognition:
#   Stage 1: RMS — strip head/tail silence, require enough voiced
#             duration + ratio across the trimmed buffer.
#   Stage 2: Silero VAD on the trimmed audio, require enough
#            truly-speech-like duration to reject TV/music/noise
#            that has loudness but isn't human voice.

PREFILTER_SAMPLE_RATE: int = 16000
PREFILTER_FRAME_MS: int = 20           # RMS analysis window
PREFILTER_PAD_MS: int = 100            # padding kept around the trim cut

# Stage 1 — RMS
PREFILTER_TRIM_RMS: float = 3500.0     # head/tail trim threshold (strict)
PREFILTER_VOICED_RMS: float = 2500.0   # per-frame voiced threshold (lenient)
PREFILTER_MIN_TRIMMED_S: float = 2.0   # drop if trimmed shorter than this
PREFILTER_MIN_VOICED_S: float = 1.0    # min total voiced duration
PREFILTER_MIN_VOICED_RATIO: float = 0.3  # min voiced/trimmed ratio

# Stage 2 — Silero VAD
PREFILTER_SILERO_THRESHOLD: float = 0.5  # per-chunk speech-prob threshold
PREFILTER_VAD_MIN_VOICED_S: float = 1.0  # min Silero voiced duration
PREFILTER_VAD_FALLBACK_MIN_VOICED_S: float = 3.0  # stricter RMS bar when Silero unavailable
PREFILTER_SILERO_CHUNK_SAMPLES: int = 512  # Silero v5 requires 512 @ 16kHz
PREFILTER_SILERO_CONTEXT_SAMPLES: int = 64  # Silero v5 context prepend

# --- Upload length -----------------------------------------------------------
# emotion2vec was trained on short utterances (acted corpora: 2.4 s median,
# 7.1 s max) and grows confidently wrong on longer input. The server also
# bounds SER input to 2-8 s (perception-service #492). Only the most-voiced
# contiguous span of this length is uploaded; audio past it would be dropped
# server-side anyway.
SER_MAX_CLIP_S: float = 8.0
