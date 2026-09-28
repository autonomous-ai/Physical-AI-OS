"""Pure helpers for the speech emotion pipeline.

Kept free of I/O and threading so they can be unit-tested without spinning
up the service or hitting perception-service.
"""

from __future__ import annotations

import io
import wave

import numpy as np
import numpy.typing as npt

from hal.drivers.voice.speech_emotion.constants import (
    CONFIDENCE_THRESHOLD_BY_LABEL,
    DEFAULT_CONFIDENCE_THRESHOLD,
    HEDGE_BY_BUCKET,
    LABEL_BUCKETS,
    NEUTRAL_LABELS,
    SpeechEmotionLabel,
)

def normalize_label(label: str) -> str:
    return (label or "").strip().lower()


def is_neutral(label: SpeechEmotionLabel | str) -> bool:
    return label in NEUTRAL_LABELS


def bucket_for(label: SpeechEmotionLabel | str) -> str:
    return LABEL_BUCKETS.get(label, "other")


def threshold_for(label: SpeechEmotionLabel | str) -> float:
    return CONFIDENCE_THRESHOLD_BY_LABEL.get(
        label, DEFAULT_CONFIDENCE_THRESHOLD,
    )


def hedge_for(bucket: str) -> str:
    return HEDGE_BY_BUCKET.get(bucket, "do not over-react")


def format_message(label: SpeechEmotionLabel, confidence: float, bucket: str) -> str:
    """Hedged sensing message — symmetric with face emotion processor.

    Skill parsers on the OS server extract the raw label via regex on the
    "Speech emotion detected: <Label>." prefix; everything inside the
    parentheses is human-readable hint for the agent.
    """
    nice = label.value.capitalize() or "Unknown"
    return (
        f"Speech emotion detected: {nice}. "
        f"(weak voice cue; confidence={confidence:.2f}; "
        f"bucket={bucket}; treat as uncertain, {hedge_for(bucket)}.)"
    )

def wav_to_pcm16(wav_bytes: bytes) -> tuple[npt.NDArray[np.int16], int]:
    """Decode a WAV blob into (mono int16 samples, sample_rate).

    Multi-channel inputs are collapsed to mono by averaging.
    Raises ValueError if the WAV is malformed or not 16-bit PCM.
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        sample_rate = w.getframerate()
        sample_width = w.getsampwidth()
        n_channels = w.getnchannels()
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)
    if sample_width != 2:
        raise ValueError(f"expected 16-bit PCM, got sampwidth={sample_width}")
    samples = np.frombuffer(raw, dtype=np.int16)
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1).astype(np.int16)
    return samples, sample_rate


def pcm16_to_wav(samples: npt.NDArray[np.int16], sample_rate: int) -> bytes:
    """Wrap mono int16 samples in a WAV header."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def compute_frame_rms(
    samples: npt.NDArray[np.int16], frame_samples: int,
) -> npt.NDArray[np.float64]:
    """Per-frame RMS in int16 units. Trailing partial frame is dropped."""
    if samples.size == 0 or frame_samples <= 0:
        return np.zeros(0, dtype=np.float64)
    n_frames = samples.size // frame_samples
    if n_frames == 0:
        return np.zeros(0, dtype=np.float64)
    truncated = samples[: n_frames * frame_samples].astype(np.float64)
    reshaped = truncated.reshape(n_frames, frame_samples)
    return np.sqrt(np.mean(reshaped ** 2, axis=1))


def compute_trim_and_voiced(
    samples: npt.NDArray[np.int16],
    sample_rate: int,
    trim_rms: float,
    voiced_rms: float,
    frame_ms: int,
    pad_ms: int,
) -> tuple[npt.NDArray[np.int16], float, float]:
    """Single-pass RMS analysis: trim head/tail silence AND compute voiced
    metrics on the padded-trim range from the same RMS envelope.

    Returns ``(trimmed_samples, voiced_seconds, voiced_ratio)``.
    Empty input or no frame above ``trim_rms`` → ``(empty, 0.0, 0.0)``.

    Two thresholds are used by design:
      * ``trim_rms`` (strict) decides head/tail boundary — must be confident
        the frame is voiced before we anchor the trim there.
      * ``voiced_rms`` (lenient) counts how many frames inside the trim
        range carry energy — meant to also pick up whisper/breathy.

    ``voiced_ratio`` denominator is the padded-trim span (not the full
    input), so a long silence prefix doesn't artificially deflate ratio.
    """
    if samples.size == 0:
        return samples[:0], 0.0, 0.0
    frame_samples = max(1, int(sample_rate * frame_ms / 1000))
    rms = compute_frame_rms(samples, frame_samples)
    if rms.size == 0:
        return samples[:0], 0.0, 0.0

    voiced_strict = rms >= trim_rms
    if not bool(voiced_strict.any()):
        return samples[:0], 0.0, 0.0

    first = int(np.argmax(voiced_strict))
    last = int(rms.size - 1 - np.argmax(voiced_strict[::-1]))

    pad_frames = max(0, int(pad_ms / max(1, frame_ms)))
    pad_first = max(0, first - pad_frames)
    pad_last = min(rms.size - 1, last + pad_frames)

    start = pad_first * frame_samples
    end = min(samples.size, (pad_last + 1) * frame_samples)
    trimmed = samples[start:end]

    # Reuse the same RMS array — count voiced frames only inside the
    # padded-trim range so the ratio reflects content density, not the
    # leading/trailing silence that we already removed.
    span_rms = rms[pad_first : pad_last + 1]
    voiced_count = int(np.count_nonzero(span_rms >= voiced_rms))
    frame_s = frame_ms / 1000.0
    voiced_s = voiced_count * frame_s
    span_total_s = span_rms.size * frame_s
    ratio = voiced_s / span_total_s if span_total_s > 0 else 0.0
    return trimmed, voiced_s, ratio


def select_voiced_span(
    samples: npt.NDArray[np.int16],
    sample_rate: int,
    voiced_rms: float,
    frame_ms: int,
    max_s: float,
) -> npt.NDArray[np.int16]:
    """Return the contiguous ``max_s`` span with the most voiced frames.

    Clips no longer than ``max_s`` are returned unchanged. The span is a plain
    slice — voiced pieces are never stitched together, because cutting gaps
    out of speech changes what the model hears. Ties go to the latest span
    (closest to "now").
    """
    max_samples = int(sample_rate * max_s)
    if samples.size <= max_samples:
        return samples
    frame_samples = max(1, int(sample_rate * frame_ms / 1000))
    rms = compute_frame_rms(samples, frame_samples)
    win = max_samples // frame_samples
    if win <= 0 or rms.size <= win:
        return samples[-max_samples:]
    voiced = (rms >= voiced_rms).astype(np.int64)
    csum = np.concatenate(([0], np.cumsum(voiced)))
    counts = csum[win:] - csum[:-win]  # counts[i] = voiced frames in [i, i + win)
    best = int(counts.size - 1 - np.argmax(counts[::-1]))  # last index of the max
    if best == counts.size - 1:
        # The latest span; anchor it to the true end so the partial trailing
        # frame dropped by compute_frame_rms is kept.
        return samples[-max_samples:]
    start = best * frame_samples
    return samples[start : start + max_samples]
