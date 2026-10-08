"""End-of-turn finalization for VoiceService._stream_session."""

import logging
import os
import time

from hal.drivers.voice._internal import config as voice_cfg

logger = logging.getLogger("hal.voice")

_MIN_SOLO_ECHO_WORD = 4

# How long after playback ends the device's own words can still open a capture
# (room reverb and the AEC tail). Beyond this the user's own words are not an echo.
ECHO_PREFIX_WINDOW_S = float(os.environ.get("HAL_ECHO_PREFIX_WINDOW_S", "4.0"))


def last_spoken(tts) -> tuple[str, float]:
    """``(text, finished_at)`` of the device's last reply; empty when unknown.

    Tolerates a speaker that exposes neither value (or exposes stand-ins in tests).
    """
    if tts is None:
        return "", 0.0
    text = getattr(tts, "last_spoken_text", "")
    if not isinstance(text, str):
        return "", 0.0
    try:
        spoken_at = float(getattr(tts, "last_spoken_time", 0.0) or 0.0)
    except (TypeError, ValueError):
        return "", 0.0
    return text, spoken_at


def recent_spoken_text(tts, now=None, window_s=None) -> str:
    """The device's last reply, only while its echo can still be in a capture.

    ``now`` is the capture start; an utterance that began long after playback
    ended keeps every word, even ones the device also said earlier.
    """
    text, spoken_at = last_spoken(tts)
    if not text or spoken_at <= 0.0:
        return ""
    window = ECHO_PREFIX_WINDOW_S if window_s is None else window_s
    now = time.time() if now is None else now
    if now - spoken_at > window:
        return ""
    return text


def _words(text):
    """Lowercased alphanumeric tokens — punctuation and case carry no signal."""
    return [
        "".join(c for c in token if c.isalnum())
        for token in text.lower().split()
        if any(c.isalnum() for c in token)
    ]


def strip_echo_prefix(transcript: str, spoken: str) -> str:
    """Drop a leading run of `transcript` that the device itself just said.

    A single short word is not enough evidence — "the" appears in every reply — so a
    one-token match must be a long word.
    """
    if not transcript or not spoken:
        return transcript
    said = _words(spoken)
    heard = _words(transcript)
    if not said or not heard:
        return transcript

    matched = 0
    for count in range(min(len(heard), len(said)), 0, -1):
        prefix = heard[:count]
        if any(
            said[i: i + count] == prefix for i in range(len(said) - count + 1)
        ):
            matched = count
            break
    if matched == 0:
        return transcript
    if matched == 1 and len(heard[0]) < _MIN_SOLO_ECHO_WORD:
        return transcript
    if matched == len(heard):
        # Entirely the device's own voice. Leave it whole so the existing
        # similarity filter makes that call and logs it as the echo it is.
        return transcript

    seen, cut, in_token = 0, len(transcript), False
    for i, char in enumerate(transcript):
        if char.isalnum():
            in_token = True
        elif in_token:
            in_token = False
            seen += 1
            if seen == matched:
                cut = i
                break
    kept = transcript[cut:].lstrip(" .,!?;:—-").strip()
    if not kept:
        return transcript
    logger.info(
        "Echo prefix stripped (%d word(s)): %r → %r [device said %r]",
        matched, transcript[:60], kept[:60], spoken[-60:],
    )
    return kept


def finalize_session(
    audio_buffer, last_partial, final_segments, last_speech_idx, spoken_text="",
):
    """Return ``(combined_transcript, ser_audio_buffer, buf_duration_s)``."""
    if last_partial[0]:
        final_segments.append(last_partial[0])
    combined = " ".join(final_segments).strip()
    combined = strip_echo_prefix(combined, spoken_text)

    if combined and not any(char.isalnum() for char in combined):
        logger.info(
            "Session transcript has no spoken content; treating it as empty: %r",
            combined,
        )
        combined = ""

    ser_audio_buffer = list(audio_buffer)

    if last_speech_idx >= 0:
        tail_frames = int(200 / voice_cfg.FRAME_DURATION_MS) + 1
        trim_end = min(last_speech_idx + tail_frames + 1, len(audio_buffer))
        dropped = len(audio_buffer) - trim_end
        if dropped > 0:
            del audio_buffer[trim_end:]
            logger.info(
                "Session TRIM — dropped %d trailing-silence frames (~%.2fs) "
                "[speaker-recog buffer only; SER keeps full %d frames]",
                dropped,
                dropped * voice_cfg.FRAME_DURATION_MS / 1000,
                len(ser_audio_buffer),
            )

    buf_bytes = sum(len(b) for b in audio_buffer)
    buf_duration = buf_bytes / (voice_cfg.STT_RATE * 2)
    return combined, ser_audio_buffer, buf_duration
