"""End-of-turn finalization for VoiceService._stream_session."""

import logging

from hal.drivers.voice._internal import config as voice_cfg

logger = logging.getLogger("hal.voice")

_MIN_SOLO_ECHO_WORD = 4


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
