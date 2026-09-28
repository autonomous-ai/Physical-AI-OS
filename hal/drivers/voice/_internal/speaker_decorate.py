"""Post-STT transcript decoration.

Wraps three closely-related concerns that all run after STT produces a final
transcript:

  1. Wake-word resolution    — strip "hey <name>" prefix, classify event type
  2. Speaker identification  — prefix "<Name>: " from voice embedding
  3. Speech-emotion submit   — async SER call on the full mic session

All speaker-recog + SER state lives here so VoiceService doesn't carry it.
"""

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
    """Whether one heard word is the expected NAME with at most one typo.

    Levenshtein distance 1 (one substitution, insertion or deletion), computed
    on words short enough that the loop is free. Deliberately not a phonetic
    matcher: the failure this exists for is STT swapping a single letter in a
    name it half-heard, and a looser measure would start accepting real words.
    """
    if heard == expected:
        return True
    if abs(len(heard) - len(expected)) > 1:
        return False
    if len(heard) < 3 or len(expected) < 3:
        # A one-letter slip in a two-letter name is a different word, not a typo.
        return False
    if len(heard) == len(expected):
        return sum(a != b for a, b in zip(heard, expected)) == 1
    # One insertion/deletion: the shorter must be the longer with one char cut.
    short, long = sorted((heard, expected), key=len)
    i = 0
    for j, ch in enumerate(long):
        if i < len(short) and short[i] == ch:
            i += 1
        elif j - i:  # a second mismatch
            return False
    return True


def _phrase_matches_loosely(sentence_part: str, phrase: str) -> bool:
    """Whether a wake phrase matches with one typo allowed in its LAST word.

    The prefix ("hello", "hey", ...) must match exactly — it is a common word
    STT gets right, and loosening it would start matching ordinary speech. Only
    the name is allowed to be off by one, because that is the word STT has no
    reason to expect. Device-observed 04/09/2026 on lamp-0c89: "hello lamp" was
    transcribed correctly in the partial and re-transcribed as "hello lamb" in
    the final.
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

    Providers do not agree on interim semantics. One may send ``Hello`` then
    ``Hello Luna``; another may send ``Hello`` then only the new token
    ``Luna``. The wake-word gate needs a single leading hypothesis for both
    shapes, without waiting for end-of-speech.
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

    # Delta-style updates can repeat their boundary word ("hello luna" then
    # "luna what time is it"). Keep the longest shared suffix/prefix once.
    overlap = min(len(previous_words), len(current_words))
    while overlap and previous_words[-overlap:] != current_words[:overlap]:
        overlap -= 1
    return " ".join(previous_words + current_words[overlap:])


# Sentinel for "the recognizer ran and could not place this voice".
UNKNOWN_LABEL = "unknown"


class SpeakerDecorator:
    """Owns wake-word list + speaker recognizer + speech-emotion service."""

    def __init__(self, wake_words: list, nudge_cooldown_s: float, enable_people_perception: bool = True):
        self._wake_words: list = list(wake_words)
        self._wake_words_lock = threading.Lock()

        # Enroll-nudge cooldown per voiceprint_hash. In-memory only — resets on
        # restart (acceptable; worst case is one extra prompt after reboot).
        self._last_nudge_time: dict[str, float] = {}
        self._nudge_cooldown_s: float = nudge_cooldown_s

        # Last recognizer verdict, reused for a short while instead of paying an
        # external inference call per turn. Holds unknowns too — see
        # _cached_identity. (name, display, monotonic-ish wall clock).
        self._identity_cache: Optional[tuple[str, Optional[str], float]] = None
        self._identity_cache_lock = threading.Lock()

        self._speaker = self._init_speaker(enable_people_perception)
        self._speech_emotion = self._init_speech_emotion(enable_people_perception)

    # ------------------------------------------------------------------
    # Lazy service init
    # ------------------------------------------------------------------
    @staticmethod
    def _init_speaker(enable_people_perception: bool = True):
        # Speaker recognition (identifying WHO is speaking from their voiceprint)
        # is voice people-perception — gated on the `audio` capability (the mic).
        # It needs only a mic, so any device that declares `audio` runs it.
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
        # Speech emotion (reading the user's emotion from voice) is voice
        # people-perception — gated on the `audio` capability (the mic), not the
        # camera. Any device with a mic runs it; it is not a hard requirement.
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

    # ------------------------------------------------------------------
    # Wake-word management
    # ------------------------------------------------------------------
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

        Three positions, deliberately not four:

          * sentence start — "hey luna, what time is it"
          * sentence end   — "what time is it, hey luna" (vocative; how people
            actually talk once a thought arrives before they remember to
            address the device)
          * a LATER sentence — a mic session is one continuous stretch of
            speech, not one sentence, so STT hands back
            "What was the score? Hi lamp, can you hear me?" as ONE transcript.
            Matching only the head of the whole thing threw that turn away
            entirely: the gate rejected it, the question never reached the
            agent, and the user just saw silence (device-observed 18/08/2026).

        Mid-sentence stays rejected — the agent's name landing inside a
        sentence is ordinary conversation ABOUT the device ("this lamp is
        nice"), and letting that open the gate is how a device barges into a
        chat between two other people.

        Case and punctuation are ignored so Deepgram variants such as
        ``Hey Luna, ...`` and ``hey luna ...`` both match.

        Name kept as-is: every caller reads it as "is this turn addressed to
        us", and the sentence-level rule is what that question always meant.
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

        Used ONLY to confirm a gate that an exactly-matching partial already
        opened — never to open one. That is what keeps it safe: to reach this
        check at all, the speaker must already have said the name correctly
        enough for STT to transcribe it exactly once, so an ambient sentence
        containing a near-miss word can not wake anything.

        It exists because the confirmation step compares against the FINAL
        transcript, and STT rewrites its own hypothesis there. Device-observed
        04/09/2026 on lamp-0c89: partial 'hello lamp' opened the gate, the final
        came back 'Hello, lamb. Can you hear me?', exact confirmation failed and
        the whole turn was dropped — no realtime turn, no thinking cue, and the
        question fell through to the much slower main agent.
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
        """Classify a leading wake phrase without modifying the transcript.

        Returns (final_text, event_type):
          * final_text — original text sent to the OS server, including the
            wake phrase.
          * event_type — "voice_command" if a wake word matched at the start,
                         else "voice".

        Empty combined → ("", "voice"); caller typically skips the POST then.
        """
        if not combined:
            return "", "voice"

        if self.starts_with_wake_word(combined):
            return combined, "voice_command"
        return combined, "voice"

    # ------------------------------------------------------------------
    # Speaker identification
    # ------------------------------------------------------------------
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

        # No trailing "otherwise ask them to introduce themselves". This branch is
        # the one a meaningless fragment lands in — too short to enrol IS the
        # common case for a lone word STT scraped off someone talking nearby —
        # and that clause is an imperative glued onto the turn text itself. The
        # model then obeys the nearest, most specific instruction over the
        # silence rule and greets a passer-by ("I don't think we've met, what's
        # your name?") on the strength of one word. Surface the path and the tag
        # so a LATER real turn can still enrol; never ask on this one.
        return (
            f"Unknown Speaker:{hash_tag} {transcript} "
            f"(audio saved at {audio_path}. Note: audio is too short for "
            f"single enrollment. If prior turns tagged the same {voiceprint_hash or 'voice cluster'}, "
            f"combine their saved paths with this one when enrolling.)"
        )

    # ------------------------------------------------------------------
    # Identity cache
    # ------------------------------------------------------------------
    def _cached_identity(self, in_followup: bool) -> Optional[tuple[str, Optional[str]]]:
        """Return (name, display) when the last result is still good enough.

        Recognition is an external call and voices do not change mid-sentence,
        let alone mid-conversation. Without this the same speaker is re-derived
        from scratch on every turn, and each derivation sits in front of the
        model receiving the audio.
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
        """Run speaker recognition; return (OS server message, SER user, display).

        - OS server message — transcript decorated with the speaker prefix.
        - SER user — known label or "unknown" (only when recognize completes
          without `error`); None skips SER.
        - display — the matched speaker's display name (e.g. "Darren") on a
          confident match, else None. Used to name the voice speaker in the
          realtime turn context WITHOUT re-running recognition; None on
          unknown / gate-reject / server error so the caller falls back cleanly.

        `in_followup` marks a turn inside a wake-word follow-up window, where a
        cached identity is held for the whole window: those turns are one
        conversation by definition.
        """
        logger.info("Identify and decorate transcript: raw transcript is: '%s'", transcript)
        cached = self._cached_identity(in_followup)
        if cached is not None:
            name, display = cached
            if name != UNKNOWN_LABEL:
                return f"Speaker - {display}: {transcript}", name, display
            # A cached unknown returns the plain transcript: the decorated
            # "unknown speaker" message exists to hand the enrolment UI the WAV
            # path of THIS utterance, and a cache hit produced no new one.
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
        duration_s = total_bytes / (STT_RATE * 2)  # int16 mono
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

    # ------------------------------------------------------------------
    # Speech-emotion submission
    # ------------------------------------------------------------------
    @staticmethod
    def _session_wav_for_ser(audio_buffer: list[bytes]) -> Optional[tuple[bytes, float]]:
        """Build mono 16 kHz WAV + duration from the STT session buffer (for SER)."""
        if not audio_buffer:
            return None
        duration_s = sum(len(b) for b in audio_buffer) / (STT_RATE * 2)
        # SPEAKER_MIN_AUDIO_S (0.8s) is INERT on the SER path: SpeechEmotionService
        # .submit() applies its own SPEECH_EMOTION_MIN_AUDIO_S (3.0s) floor, and
        # only the larger of the two can bind. Raising or lowering the speaker knob
        # looks like it should change what SER sees, and does not — tune
        # HAL_SPEECH_EMOTION_MIN_AUDIO_S for that. Kept rather than deleted because
        # this helper's floor is correct for the speaker path it is named after.
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
        """Submit SER on the full mic-session buffer (async via the service).

        A turn ignored by the wake-word gate does not run speaker
        identification, so its SER record intentionally uses ``unknown``.
        """
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
