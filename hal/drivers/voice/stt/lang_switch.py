"""Language-switching STT: open in the likely language, correct it from audio.

A session opens immediately in the predicted language (the last confidently
identified one, else the primary), so a same-language turn pays no extra
latency. Identification works per utterance: only speech frames count, and a
non-speech gap starts a new utterance, so a long-lived session (live mode keeps
one STT session across many turns) follows each utterance's language.

Within an utterance, identification re-runs off the mic thread at every
checkpoint of accumulated speech (default each second from 1 s to 10 s). Whenever
it confidently disagrees with the current session, a session in the detected
language is connected in the background, the utterance's audio is replayed into
it, and it replaces the current one. The last checkpoint is final: its top guess
is trusted at a lower bar (`final_probability`) and identification stops until
the next utterance. Audio whose configured languages hold little of the full
softmax (another language, or noise) never causes a switch.

Transcripts from a replaced session are dropped. An owner with a switch listener
(per-turn capture) resets its whole transcript, so the whole session's audio is
replayed; without one (live mode) only the current utterance is.
"""

import logging
import threading
import time
from typing import Callable, Optional, Sequence

from hal.drivers.voice.lang_id import LanguageGuess, LanguageIdentifier
from hal.drivers.voice.stt.provider import STTProvider, STTSession

logger = logging.getLogger("hal.voice.stt")

_BYTES_PER_SECOND = 16000 * 2  # mono PCM16 at the STT rate
_PRE_ROLL_BYTES = _BYTES_PER_SECOND // 2  # audio kept before an utterance starts
_MAX_SESSION_REPLAY_BYTES = 180 * _BYTES_PER_SECOND  # hands-free turn cap
_LID_JOIN_TIMEOUT_S = 1.0
_SWAP_JOIN_TIMEOUT_S = 5.0

SwitchListener = Callable[[str, str], None]


def _rms(pcm16: bytes) -> float:
    import numpy as np

    samples = np.frombuffer(pcm16[: (len(pcm16) // 2) * 2], dtype=np.int16)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


class LanguageSwitchingSTT(STTProvider):
    """Wrap a provider whose create_session accepts a per-session language."""

    def __init__(self, base: STTProvider, identifier: LanguageIdentifier, primary: str, *,
                 checkpoints_s: Sequence[float] = tuple(float(s) for s in range(1, 11)),
                 switch_probability: float = 0.85, final_probability: float = 0.0,
                 min_in_set: float = 0.5, speech_rms: float = 300.0,
                 utterance_gap_s: float = 1.0, sticky_s: float = 120.0):
        self._base = base
        self._identifier = identifier
        self._primary = primary
        self._session_options = dict(
            checkpoints_s=sorted(c for c in checkpoints_s if c > 0),
            switch_probability=switch_probability,
            final_probability=final_probability,
            min_in_set=min_in_set,
            speech_rms=speech_rms,
            utterance_gap_s=utterance_gap_s,
        )
        self._sticky_s = sticky_s
        self._lock = threading.Lock()
        self._last_language: Optional[str] = None
        self._last_language_at = 0.0
        threading.Thread(target=identifier.warm_up, daemon=True, name="lang-id-warmup").start()

    def predicted_language(self) -> str:
        """Last confidently identified language while fresh, else the primary."""
        with self._lock:
            if (self._last_language
                    and time.monotonic() - self._last_language_at <= self._sticky_s):
                return self._last_language
        return self._primary

    def remember(self, language: str) -> None:
        with self._lock:
            self._last_language = language
            self._last_language_at = time.monotonic()

    def create_session(self, language: Optional[str] = None) -> STTSession:
        if language:
            # An explicit language bypasses identification.
            return self._base.create_session(language)
        return LanguageSwitchingSession(
            self._base, self._identifier, self.predicted_language(),
            on_confident=self.remember, **self._session_options,
        )

    @property
    def available(self) -> bool:
        return self._base.available

    @property
    def name(self) -> str:
        return f"LanguageSwitching({self._base.name}, {'/'.join(self._identifier.languages)})"


class LanguageSwitchingSession(STTSession):
    """An STT session whose inner session follows each utterance's language."""

    def __init__(self, base: STTProvider, identifier: LanguageIdentifier, language: str, *,
                 checkpoints_s: Sequence[float], switch_probability: float,
                 final_probability: float, min_in_set: float, speech_rms: float,
                 utterance_gap_s: float, on_confident: Callable[[str], None]):
        self._base = base
        self._identifier = identifier
        self.language = language
        self._checkpoint_bytes = [int(c * _BYTES_PER_SECOND) for c in checkpoints_s]
        self._switch_probability = switch_probability
        self._final_probability = final_probability
        self._min_in_set = min_in_set
        self._speech_rms = speech_rms
        self._utterance_gap_s = utterance_gap_s
        self._on_confident = on_confident
        self._lock = threading.Lock()
        self._inner = base.create_session(language)
        self._generation = 0
        # Utterance state (guarded by _lock).
        self._utterance = 0
        self._in_utterance = False
        self._silence_s = 0.0
        self._recent = bytearray()  # rolling pre-roll while no utterance is open
        self._utterance_audio = bytearray()  # replayed when there is no listener
        self._session_audio = bytearray()  # replayed when a listener resets everything
        self._speech = bytearray()  # speech frames only, classified
        self._checkpoints: list[int] = []
        self._decided = True
        self._closing = False
        self._lid_thread: Optional[threading.Thread] = None
        self._swap_thread: Optional[threading.Thread] = None
        self._switch_listener: Optional[SwitchListener] = None
        # Read at dispatch time: the keepalive path reassigns it after start().
        self._on_transcript_cb: Callable[[str, bool], None] = lambda text, is_final: None

    def set_switch_listener(self, listener: Optional[SwitchListener]) -> None:
        """listener(old, new) runs when transcripts so far are invalidated."""
        self._switch_listener = listener

    def _transcript_callback(self, generation: int) -> Callable[[str, bool], None]:
        def dispatch(text: str, is_final: bool) -> None:
            if generation == self._generation:
                self._on_transcript_cb(text, is_final)
        return dispatch

    def start(self, on_transcript: Callable[[str, bool], None]) -> bool:
        self._on_transcript_cb = on_transcript
        return self._inner.start(self._transcript_callback(self._generation))

    def send_audio(self, data: bytes):
        with self._lock:
            self._track_utterance_locked(data)
            # Sent under the lock so a swap's replay cannot interleave with it.
            self._inner.send_audio(data)
            self._maybe_identify_locked()

    def _track_utterance_locked(self, data: bytes) -> None:
        if self._switch_listener is not None:
            self._session_audio += data
            del self._session_audio[:-_MAX_SESSION_REPLAY_BYTES]
        if _rms(data) >= self._speech_rms:
            if not self._in_utterance:
                self._in_utterance = True
                self._utterance += 1
                self._utterance_audio = bytearray(self._recent)
                self._speech = bytearray()
                self._checkpoints = list(self._checkpoint_bytes)
                self._decided = self._closing or not self._checkpoints
            self._silence_s = 0.0
            self._speech += data
        else:
            self._silence_s += len(data) / _BYTES_PER_SECOND
            if self._in_utterance and self._silence_s >= self._utterance_gap_s:
                self._in_utterance = False
                self._recent = bytearray()
        if self._in_utterance:
            self._utterance_audio += data
        else:
            self._recent += data
            del self._recent[:-_PRE_ROLL_BYTES]

    def _maybe_identify_locked(self) -> None:
        if self._decided or not self._checkpoints or len(self._speech) < self._checkpoints[0]:
            return
        if self._lid_thread is not None and self._lid_thread.is_alive():
            return
        if self._swap_thread is not None and self._swap_thread.is_alive():
            return
        # Skip checkpoints passed while busy: always classify the latest speech.
        while self._checkpoints and len(self._speech) >= self._checkpoints[0]:
            self._checkpoints.pop(0)
        final = not self._checkpoints
        if final:
            self._decided = True
        self._lid_thread = threading.Thread(
            target=self._identify, args=(bytes(self._speech), final, self._utterance),
            daemon=True, name="lang-id",
        )
        self._lid_thread.start()

    def _identify(self, pcm: bytes, final: bool, utterance: int) -> None:
        started = time.monotonic()
        try:
            guess = self._identifier.identify(pcm)
        except Exception as e:
            logger.warning("Language ID failed: %s", e)
            guess = None
        if guess is None:
            return
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info(
            "Language ID: %s p=%.2f in_set=%.2f on %.1fs speech (session=%s, utterance=%d%s, %.0fms)",
            guess.language, guess.probability, guess.in_set, guess.seconds, self.language,
            utterance, ", final" if final else "", elapsed_ms,
        )
        if utterance != self._utterance:
            return  # a newer utterance started; this guess no longer applies
        if guess.in_set < self._min_in_set:
            return  # another language or not speech: the restricted guess is forced
        bar = self._final_probability if final else self._switch_probability
        if guess.probability < bar:
            return
        if guess.language == self.language:
            self._decided = True
            self._on_confident(guess.language)
            return
        self._swap_thread = threading.Thread(
            target=self._swap, args=(guess, utterance), daemon=True, name="stt-lang-swap",
        )
        self._swap_thread.start()

    def _swap(self, guess: LanguageGuess, utterance: int) -> None:
        old_language = self.language
        new_language = guess.language
        generation = self._generation + 1
        replacement = self._base.create_session(new_language)
        if not replacement.start(self._transcript_callback(generation)):
            logger.warning(
                "Language switch %s -> %s failed to connect; keeping %s",
                old_language, new_language, old_language,
            )
            return
        with self._lock:
            old = self._inner
            self._generation = generation
            self._inner = replacement
            self.language = new_language
            listener = self._switch_listener
            if listener is not None:
                try:
                    listener(old_language, new_language)
                except Exception as e:
                    logger.warning("Language switch listener failed: %s", e)
            replay = bytes(self._session_audio if listener is not None else self._utterance_audio)
            replacement.send_audio(replay)
        self._on_confident(new_language)
        logger.info(
            "Language switch %s -> %s (p=%.2f, utterance=%d): replayed %.1fs into new STT session",
            old_language, new_language, guess.probability, utterance,
            len(replay) / _BYTES_PER_SECOND,
        )
        threading.Thread(target=old.close, daemon=True, name="stt-lang-old-close").start()

    def send_keepalive(self):
        send = getattr(self._inner, "send_keepalive", None)
        if send is not None:
            send()

    def close(self):
        # Finish a pending decision first so the final transcript comes from the
        # session in the right language.
        with self._lock:
            self._closing = True
        lid = self._lid_thread
        if lid is not None:
            lid.join(timeout=_LID_JOIN_TIMEOUT_S)
        with self._lock:
            self._decided = True
        swap = self._swap_thread
        if swap is not None:
            swap.join(timeout=_SWAP_JOIN_TIMEOUT_S)
        self._inner.close()

    def is_closed(self) -> bool:
        return self._inner.is_closed()


def wrap_with_language_id(base: STTProvider, configured: object,
                          primary: Optional[str]) -> STTProvider:
    """Return base wrapped for language switching, or base when not applicable.

    configured: config.json `stt_languages` (list of app codes). Identification
    is off unless it names two or more supported languages and the model exists.
    """
    from hal.drivers.voice._internal import config as voice_cfg
    from hal.presets import DEFAULT_LANG, SUPPORTED_LANGS, normalize_language

    if not isinstance(configured, list):
        return base
    languages = []
    for code in configured:
        code = normalize_language(str(code))
        if code in SUPPORTED_LANGS and code not in languages:
            languages.append(code)
    if len(languages) < 2:
        return base
    primary = normalize_language(primary) or languages[0]
    if primary not in languages:
        languages.insert(0, primary)
    identifier = LanguageIdentifier(
        voice_cfg.LANG_ID_MODEL_PATH, voice_cfg.LANG_ID_LABELS_PATH, languages,
        threads=voice_cfg.LANG_ID_THREADS, max_seconds=voice_cfg.LANG_ID_WINDOW_S,
    )
    if not identifier.usable:
        logger.warning(
            "Language ID unavailable (model=%s); STT stays %s",
            voice_cfg.LANG_ID_MODEL_PATH, primary or DEFAULT_LANG,
        )
        return base
    logger.info("Language ID enabled: languages=%s primary=%s", identifier.languages, primary)
    return LanguageSwitchingSTT(
        base, identifier, primary,
        checkpoints_s=voice_cfg.lang_id_checkpoints_s(),
        switch_probability=voice_cfg.LANG_ID_SWITCH_PROB,
        final_probability=voice_cfg.LANG_ID_FINAL_PROB,
        min_in_set=voice_cfg.LANG_ID_MIN_IN_SET,
        speech_rms=voice_cfg.LANG_ID_SPEECH_RMS,
        utterance_gap_s=voice_cfg.LANG_ID_UTTERANCE_GAP_S,
        sticky_s=voice_cfg.LANG_ID_STICKY_S,
    )
