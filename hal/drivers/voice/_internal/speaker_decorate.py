"""Post-STT transcript decoration."""

import logging
import os
import re
import threading
import time
from typing import Optional

from hal.drivers.voice._internal.config import (
    SPEAKER_ID_CACHE_FOLLOWUP_S,
    SPEAKER_ID_CACHE_S,
    SPEAKER_MIN_AUDIO_S,
    SPEAKER_RECOGNITION_ENABLED,
    SPEECH_EMOTION_ENABLED,
    STT_RATE,
)

logger = logging.getLogger("hal.voice")


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return " ".join(re.sub(r"[^\w\s]", "", text.casefold()).split())


def _sentences(transcript: str) -> list[str]:
    """Split a transcript into normalized sentences (empty ones dropped)."""
    parts = (_normalize(p) for p in re.split(r"[.!?]+", transcript))
    return [p for p in parts if p]


def _name_is_near(heard: str, expected: str) -> bool:
    """Whether one heard word is the expected NAME with at most one typo."""
    if heard == expected:
        return True
    if abs(len(heard) - len(expected)) > 1:
        return False
    if len(heard) < 3 or len(expected) < 3:
        return False
    if len(heard) == len(expected):
        return sum(a != b for a, b in zip(heard, expected)) == 1
    # One insertion/deletion: the shorter must be the longer with one char cut.
    short, long = sorted((heard, expected), key=len)
    i = 0
    for j, ch in enumerate(long):
        if i < len(short) and short[i] == ch:
            i += 1
        elif j - i:
            return False
    return True


def _phrase_matches_loosely(sentence_part: str, phrase: str) -> bool:
    """Whether a wake phrase matches with one typo allowed in its LAST word.

    The prefix ("hello", "hey", ...) must match exactly — it is a common word STT gets
    right, and loosening it would start matching ordinary speech.
    """
    heard = sentence_part.split()
    expected = phrase.split()
    if len(heard) != len(expected) or not expected:
        return False
    if heard[:-1] != expected[:-1]:
        return False
    return _name_is_near(heard[-1], expected[-1])


def merge_wake_words(*word_lists: list[str]) -> list[str]:
    """Merge wake-word aliases case-insensitively while preserving order."""
    merged: list[str] = []
    seen: set[str] = set()
    for words in word_lists:
        for word in words:
            normalized = word.strip().casefold()
            if normalized and normalized not in seen:
                seen.add(normalized)
                merged.append(normalized)
    return merged


def merge_stt_hypothesis(previous: str, current: str) -> str:
    """Merge cumulative and delta-style STT transcript updates.

    Providers do not agree on interim semantics.
    """
    previous_words = re.findall(r"\w+", previous.casefold())
    current_words = re.findall(r"\w+", current.casefold())
    if not previous_words:
        return " ".join(current_words)
    if not current_words:
        return " ".join(previous_words)
    if current_words[:len(previous_words)] == previous_words:
        return " ".join(current_words)
    if previous_words[:len(current_words)] == current_words:
        return " ".join(current_words)

    overlap = min(len(previous_words), len(current_words))
    while overlap and previous_words[-overlap:] != current_words[:overlap]:
        overlap -= 1
    return " ".join(previous_words + current_words[overlap:])


UNKNOWN_LABEL = "unknown"


class SpeakerDecorator:
    """Owns wake-word list + speaker recognizer + speech-emotion service."""

    def __init__(self, wake_words: list, nudge_cooldown_s: float, enable_people_perception: bool = True):
        self._wake_words: list = list(wake_words)
        self._wake_words_lock = threading.Lock()

        self._last_nudge_time: dict[str, float] = {}
        self._nudge_cooldown_s: float = nudge_cooldown_s

        self._identity_cache: Optional[tuple[str, Optional[str], float]] = None
        self._identity_cache_lock = threading.Lock()

        self._speaker = None
        self._speech_emotion = None
        self._init_stop = threading.Event()
        self._init_lock = threading.Lock()
        self._init_threads = []
        # Optional perception must not hold up HAL or the basic voice pipeline.
        for attribute, enabled, factory in (
            ("_speaker", SPEAKER_RECOGNITION_ENABLED, self._init_speaker),
            ("_speech_emotion", SPEECH_EMOTION_ENABLED, self._init_speech_emotion),
        ):
            if enable_people_perception and enabled:
                worker = threading.Thread(
                    target=self._init_optional_service,
                    args=(attribute, factory),
                    name=f"voice-init{attribute}", daemon=True,
                )
                try:
                    worker.start()
                except Exception:
                    self.close()
                    raise
                self._init_threads.append(worker)

    def _init_optional_service(self, attribute, factory):
        while not self._init_stop.is_set():
            try:
                service = factory()
            except Exception:
                logger.exception("Optional voice service %s initialization failed", attribute)
                service = None
            if service is not None:
                with self._init_lock:
                    if not self._init_stop.is_set():
                        setattr(self, attribute, service)
                        logger.info("Optional voice service %s initialization complete", attribute)
                        return
                if attribute == "_speech_emotion":
                    service.stop()
                return
            logger.warning("Optional voice service %s unavailable; retrying in 30s", attribute)
            if self._init_stop.wait(30.0):
                return

    def close(self):
        """Dispose optional workers without waiting for an in-flight key fetch."""
        with self._init_lock:
            self._init_stop.set()
            if self._speech_emotion is not None:
                self._speech_emotion.stop()

    @staticmethod
    def _init_speaker(enable_people_perception: bool = True):
        if not enable_people_perception:
            logger.info("Speaker recognition off — device does not declare 'audio' (no mic for voice people-perception)")
            return None
        if not SPEAKER_RECOGNITION_ENABLED:
            logger.info(
                "Speaker recognizer disabled by HAL_SPEAKER_RECOGNITION_ENABLED=false "
                "(default is true — this is an explicit opt-out).",
            )
            return None
        try:
            # Share the ONE process-wide instance with the HTTP routes so their
            # commit locks / migration state / stranger clusters stay unified.
            from hal.drivers.voice.speaker_recognizer import get_shared_recognizer
            recognizer = get_shared_recognizer()
            if recognizer is None:
                logger.warning("Speaker recognizer unavailable (shared init failed)")
                return None
            if not recognizer.available:
                logger.info(
                    "Speaker recognizer idle — SPEAKER_EMBEDDING_API_URL not set "
                    "(service instance exists but embedding calls will return 'unknown' with an error)",
                )
            else:
                logger.info("Speaker recognizer enabled — will prefix every STT final with speaker name")
            return recognizer
        except Exception as e:
            logger.warning("Speaker recognizer init failed: %s", e)
            return None

    @staticmethod
    def _init_speech_emotion(enable_people_perception: bool = True):
        if not enable_people_perception:
            logger.info("Speech emotion recognition off — device does not declare 'audio' (no mic for voice people-perception)")
            return None
        if not SPEECH_EMOTION_ENABLED:
            logger.info("Speech emotion recognition disabled by HAL_SPEECH_EMOTION_ENABLED=false")
            return None
        try:
            from hal.drivers.voice.speech_emotion import SpeechEmotionService
            service = SpeechEmotionService()
            if not service.available:
                logger.info("Speech emotion service idle — DL backend URL not set")
            else:
                logger.info("Speech emotion service enabled")
            return service
        except Exception as e:
            logger.warning("Speech emotion service init failed: %s", e)
            return None

    def set_wake_words(self, words: list) -> None:
        """Update wake word list at runtime (called when agent is renamed)."""
        with self._wake_words_lock:
            self._wake_words = [w.lower() for w in words]
        logger.info("Wake words updated: %s", self._wake_words)

    def _normalized_wake_phrases(self) -> list[str]:
        """Configured wake phrases, normalized the same way transcripts are."""
        with self._wake_words_lock:
            wake_words = list(self._wake_words)
        phrases = [_normalize(w) for w in wake_words]
        return [p for p in phrases if p]

    def starts_with_wake_word(self, transcript: str) -> bool:
        """Return true when a wake phrase opens or closes any SENTENCE.

        Matching only the head of the whole thing threw that turn away entirely: the
        gate rejected it, the question never reached the agent, and the user just saw
        silence (device-observed 18/08/2026).
        """
        phrases = self._normalized_wake_phrases()
        if not phrases:
            return False
        for sentence in _sentences(transcript):
            for phrase in phrases:
                if (
                    sentence == phrase
                    or sentence.startswith(phrase + " ")
                    or sentence.endswith(" " + phrase)
                ):
                    return True
        return False

    def matches_wake_word_loosely(self, transcript: str) -> bool:
        """Same sentence-position rule as starts_with_wake_word, one typo allowed.

        Used ONLY to confirm a gate that an exactly-matching partial already opened —
        never to open one.
        """
        phrases = self._normalized_wake_phrases()
        if not phrases:
            return False
        for sentence in _sentences(transcript):
            words = sentence.split()
            for phrase in phrases:
                span = len(phrase.split())
                if span > len(words):
                    continue
                head = " ".join(words[:span])
                tail = " ".join(words[-span:])
                if _phrase_matches_loosely(head, phrase) or _phrase_matches_loosely(
                    tail, phrase
                ):
                    return True
        return False

    def classify_wake_word(self, combined: str) -> tuple[str, str]:
        """Classify a leading wake phrase without modifying the transcript."""
        if not combined:
            return "", "voice"

        if self.starts_with_wake_word(combined):
            return combined, "voice_command"
        return combined, "voice"

    @staticmethod
    def _should_request_speaker_enroll(
        transcript: str,
        duration_s: float = 0.0,
        min_words: int = 10,
        min_duration_s: float = 2.0,
    ) -> bool:
        """Whether audio is sufficient to include conditional enrollment guidance."""
        return len(transcript.split()) >= min_words and duration_s >= min_duration_s

    def _format_unknown_speaker_message(
        self,
        transcript: str,
        audio_path: str,
        duration_s: float = 0.0,
        voiceprint_hash: Optional[str] = None,
    ) -> str:
        """Format OS server message for an unrecognized speaker (enroll hints, cooldown)."""
        now = time.time()
        in_cooldown = False
        if voiceprint_hash:
            last = self._last_nudge_time.get(voiceprint_hash, 0.0)
            if now - last < self._nudge_cooldown_s:
                in_cooldown = True
                logger.info(
                    "Enroll nudge skipped for %s — asked %.0fs ago "
                    "(cooldown %.0fs); path + tag still surfaced",
                    voiceprint_hash, now - last, self._nudge_cooldown_s,
                )

        hash_tag = f" [voice:{voiceprint_hash}]" if voiceprint_hash else ""
        audio_hint = f" (audio saved at {audio_path})" if audio_path else ""

        if in_cooldown:
            return f"Unknown Speaker:{hash_tag} {transcript}{audio_hint}"

        if audio_path and self._should_request_speaker_enroll(transcript, duration_s):
            if voiceprint_hash:
                self._last_nudge_time[voiceprint_hash] = now
            return (
                f"Unknown Speaker:{hash_tag} {transcript} "
                f"(audio save at {audio_path}; enrollment is relevant only for "
                f"a clear self-introduction, an explicit voice enrollment request, "
                f"or a reply continuing that enrollment. Otherwise handle the "
                f"user's request without asking their name.)"
            )

        # No trailing "otherwise ask them to introduce themselves".
        return (
            f"Unknown Speaker:{hash_tag} {transcript} "
            f"(audio saved at {audio_path}. Note: audio is too short for "
            f"single enrollment. If prior turns tagged the same {voiceprint_hash or 'voice cluster'}, "
            f"combine their saved paths with this one when enrolling.)"
        )

    def _cached_identity(self, in_followup: bool) -> Optional[tuple[str, Optional[str]]]:
        """Return (name, display) when the last result is still good enough.

        Recognition is an external call and voices do not change mid-sentence, let alone
        mid-conversation.
        """
        with self._identity_cache_lock:
            entry = self._identity_cache
        if entry is None:
            return None
        name, display, ts = entry
        ttl = SPEAKER_ID_CACHE_S
        if in_followup and SPEAKER_ID_CACHE_FOLLOWUP_S > ttl:
            ttl = SPEAKER_ID_CACHE_FOLLOWUP_S
        if ttl <= 0:
            return None
        age = time.time() - ts
        if age > ttl:
            return None
        logger.info(
            "Speaker ID: reusing cached identity %r (age=%.1fs, ttl=%.0fs%s)",
            display or name, age, ttl, ", follow-up window" if in_followup else "",
        )
        return name, display

    def _remember_identity(self, name: str, display: Optional[str]) -> None:
        with self._identity_cache_lock:
            self._identity_cache = (name, display, time.time())

    def forget_identity(self) -> None:
        """Drop the cached speaker. The voice twin of clearing a face cooldown."""
        with self._identity_cache_lock:
            self._identity_cache = None

    def identify_and_decorate(
        self, transcript: str, audio_buffer: list[bytes], in_followup: bool = False,
    ) -> tuple[str, Optional[str], Optional[str]]:
        """Run speaker recognition; return (OS server message, SER user, display)."""
        logger.info("Identify and decorate transcript: raw transcript is: '%s'", transcript)
        cached = self._cached_identity(in_followup)
        if cached is not None:
            name, display = cached
            if name != UNKNOWN_LABEL:
                return f"Speaker - {display}: {transcript}", name, display
            return transcript, UNKNOWN_LABEL, None
        if self._speaker is None:
            logger.info(
                "Skip speaker ID: recognizer not initialized "
                "(HAL_SPEAKER_RECOGNITION_ENABLED or init failure)",
            )
            return transcript, None, None
        if not audio_buffer:
            logger.warning("Skip speaker ID: audio buffer is empty (no frames captured this session)")
            return transcript, None, None
        try:
            from hal.drivers.voice.speech_emotion.constants import UNKNOWN_USER_LABEL
            from hal.drivers.voice.speaker_recognizer.speaker_recognizer import pcm16_bytes_to_wav
        except Exception as e:
            logger.warning("Skip speaker ID: helper import failed: %s", e)
            return transcript, None, None

        total_bytes = sum(len(b) for b in audio_buffer)
        duration_s = total_bytes / (STT_RATE * 2)
        if duration_s < SPEAKER_MIN_AUDIO_S:
            logger.info(
                "Skip speaker ID: only %.2fs of audio buffered (<%.2fs)",
                duration_s, SPEAKER_MIN_AUDIO_S,
            )
            return transcript, None, None

        try:
            wav_bytes = pcm16_bytes_to_wav(b"".join(audio_buffer), STT_RATE)
            import base64 as _b64
            audio_b64 = _b64.b64encode(wav_bytes).decode("ascii")
            result = self._speaker.recognize(audio_b64, source_type="base64")
        except Exception as e:
            logger.warning("Speaker recognize failed: %s", e)
            return transcript, None, None

        logger.info("Speaker recognize result: %r", result)
        err = result.get("error")
        audio_path = result.get("unknown_audio_path", "")
        vp_hash = result.get("voiceprint_hash")
        if err:
            logger.warning("Speaker ID skipped — embedding server issue: %s", err)
            if audio_path:
                return self._format_unknown_speaker_message(
                    transcript, audio_path, duration_s, vp_hash,
                ), None, None
            return transcript, None, None

        name = result.get("name", "unknown")
        confidence = result.get("confidence", 0.0)
        if result.get("match") and name and name != "unknown":
            display = result.get("display_name") or name.capitalize()
            logger.info(
                "Speaker ID: %s (confidence=%.2f, audio=%s)",
                name, confidence, audio_path or "-",
            )
            self._remember_identity(name, display)
            return f"Speaker - {display}: {transcript}", name, display

        logger.info(
            "Speaker ID: unknown (best=%.2f, audio=%s, hash=%s)",
            confidence, audio_path or "-", vp_hash or "-",
        )
        self._remember_identity(UNKNOWN_LABEL, None)
        return self._format_unknown_speaker_message(
            transcript, audio_path, duration_s, vp_hash,
        ), UNKNOWN_USER_LABEL, None

    @staticmethod
    def _session_wav_for_ser(audio_buffer: list[bytes]) -> Optional[tuple[bytes, float]]:
        """Build mono 16 kHz WAV + duration from the STT session buffer (for SER)."""
        if not audio_buffer:
            return None
        duration_s = sum(len(b) for b in audio_buffer) / (STT_RATE * 2)
        if duration_s < SPEAKER_MIN_AUDIO_S:
            return None
        try:
            from hal.drivers.voice.speaker_recognizer.speaker_recognizer import (
                pcm16_bytes_to_wav,
            )
        except Exception as e:
            logger.warning("Session WAV for SER skipped — helper import failed: %s", e)
            return None
        try:
            return pcm16_bytes_to_wav(b"".join(audio_buffer), STT_RATE), duration_s
        except Exception as e:
            logger.warning("Session WAV for SER failed: %s", e)
            return None

    def submit_speech_emotion_from_session(
        self, audio_buffer: list[bytes], user: str = "unknown",
    ) -> None:
        """Submit SER on the full mic-session buffer (async via the service)."""
        if self._speech_emotion is None or not self._speech_emotion.available:
            logger.info(
                "Speech emotion submit skipped: service_init=%s available=%s",
                self._speech_emotion is not None,
                bool(self._speech_emotion and self._speech_emotion.available),
            )
            return
        session_audio = self._session_wav_for_ser(audio_buffer)
        if session_audio is None:
            return
        wav_bytes, duration_s = session_audio

        logger.info(
            "Speech emotion submit (session-end): user=%r duration=%.2fs wav=%d bytes",
            user, duration_s, len(wav_bytes),
        )
        try:
            self._speech_emotion.submit(
                user=user, wav_bytes=wav_bytes, duration_s=duration_s,
            )
        except Exception as e:
            logger.warning("Speech emotion submit failed: %s", e)
