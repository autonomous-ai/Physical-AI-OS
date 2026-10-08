"""Turn admission evidence that lives outside the transcript itself.

A bare "yeah" or "okay" is noise when nobody asked anything, and an answer when
the device just did. The noise guard (`realtime_turn.is_noise_turn`) asks here
before dropping a backchannel-only transcript.
"""

import logging
import os
import re
import threading
import time

from hal import config as hal_config
from hal.drivers.voice._internal import config as voice_cfg
from hal.drivers.voice._internal.main_followup import ends_with_question, pending_main_question
from hal.drivers.voice._internal.session_finalize import last_spoken

logger = logging.getLogger("hal.voice")

# How long after the device finishes a question a bare answer still counts.
DEVICE_QUESTION_WINDOW_S = float(os.environ.get("HAL_DEVICE_QUESTION_WINDOW_S", "12"))
# A partial transcript with at least this many words commits the turn to the
# realtime model before the STT final arrives. Short partials wait: the noise
# guard needs the final to tell a fabrication from a command.
EARLY_COMMIT_MIN_WORDS = int(os.environ.get("HAL_EARLY_COMMIT_MIN_WORDS", "4"))

_lock = threading.Lock()
_tts = None


def register_tts(tts) -> None:
    """Point admission at the speaker so it can see what the device last said."""
    global _tts
    with _lock:
        _tts = tts


def device_question_pending(now: float | None = None) -> bool:
    """Whether the device's own last reply was a question asked moments ago."""
    with _lock:
        tts = _tts
    text, spoken_at = last_spoken(tts)
    if not text or spoken_at <= 0.0:
        return False
    now = time.time() if now is None else now
    return 0.0 <= now - spoken_at <= DEVICE_QUESTION_WINDOW_S and ends_with_question(text)


def short_answer_expected() -> bool:
    """Whether a backchannel-only transcript should be treated as an answer."""
    if pending_main_question():
        return True
    return device_question_pending()


def confident_partial(text: str) -> bool:
    """Whether a provisional STT partial is long enough to commit the turn on."""
    if EARLY_COMMIT_MIN_WORDS <= 0 or not text:
        return False
    words = [w for w in text.split() if any(c.isalnum() for c in w)]
    return len(words) >= EARLY_COMMIT_MIN_WORDS


def facing_evidence() -> bool | None:
    """Whether the user was facing the lamp over the gaze window.

    None when there is no usable face measurement; the gaze module keeps the
    samples, this only reads them.
    """
    try:
        from hal.drivers.tracking import gaze

        ratio, considered = gaze.facing_ratio()
    except Exception:
        return None
    if considered < hal_config.GAZE_MIN_SAMPLES:
        return None
    return ratio >= hal_config.GAZE_MIN_FACING_RATIO


def addressed_hint(*, wake_word: bool, window: bool, question: bool,
                   facing: bool | None, known_voice: str = "") -> str:
    """One line of evidence for the realtime model's turn context.

    The model still decides; this tells it what the device knows, so it does
    not have to infer from scratch each turn whether the room is talking to it.
    """
    reasons = []
    if wake_word:
        reasons.append("wake phrase heard")
    if window:
        reasons.append("open conversation window")
    if question:
        reasons.append("answering your question")
    if facing:
        reasons.append("the user is facing you")
    if known_voice:
        reasons.append(f"known voice: {known_voice}")
    if wake_word or window:
        verdict = "yes"
    elif reasons:
        verdict = "likely"
    elif facing is False:
        verdict = "unlikely"
        reasons.append("the user is facing away")
    else:
        verdict = "unknown"
        reasons.append("no name, no face evidence, no open conversation")
    hint = f"Addressed: {verdict} ({'; '.join(reasons)})"
    if verdict in ("unlikely", "unknown"):
        hint += " — stay silent unless clearly spoken to"
    return hint


def strict_addressed_gate() -> bool:
    """Whether hands-free speech without evidence is dropped before any model."""
    return hal_config.ADDRESSED_GATE == "strict"


def _normalize_words(text: str) -> str:
    """Lowercase alphanumeric words, single-spaced."""
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def device_names(wake_phrases) -> set[str]:
    """The bare names behind the wake phrases ("hey lamp" → "lamp")."""
    names: set[str] = set()
    prefixes = sorted(voice_cfg.WAKE_WORD_PREFIXES, key=len, reverse=True)
    for phrase in wake_phrases or ():
        words = _normalize_words(str(phrase))
        for prefix in prefixes:
            if words.startswith(prefix + " "):
                words = words[len(prefix) + 1:].strip()
                break
        if words:
            names.add(words)
    return names


def name_mentioned(text: str, wake_phrases) -> bool:
    """Whether the device's name is anywhere in the transcript, not only at the start.

    A person says a name at the end ("what time is it, Lamp?") or in the middle as
    often as at the front; the wake gate only looks at sentence edges.
    """
    words = _normalize_words(text)
    if not words:
        return False
    padded = f" {words} "
    return any(f" {name} " in padded for name in device_names(wake_phrases))
