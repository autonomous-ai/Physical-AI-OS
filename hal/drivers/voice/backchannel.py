"""Backchannel — active listening cues during STT sessions."""

import logging
import math
import os
import random
import threading
import time
from typing import Optional

from hal.i18n import DEFAULT_FILLERS_BY_LANG
from hal.presets import DEFAULT_LANG, normalize_language

logger = logging.getLogger("hal.voice.backchannel")


def _default_fillers_for_active_lang() -> str:
    """Pick the default filler list based on the OS server's stt_language."""
    try:
        from hal.config import _os_cfg_get
        lang = normalize_language(_os_cfg_get("stt_language"))
    except Exception:
        lang = ""
    return DEFAULT_FILLERS_BY_LANG.get(lang, DEFAULT_FILLERS_BY_LANG[DEFAULT_LANG])


_fillers_env = os.environ.get("HAL_BACKCHANNEL_FILLERS", _default_fillers_for_active_lang())
FILLERS = [w.strip() for w in _fillers_env.split(",") if w.strip()]
# How long (seconds) the partial transcript must stay unchanged before playing a cue.
STALL_TIMEOUT_S = float(os.environ.get("HAL_BACKCHANNEL_STALL_S", "8.0"))
MIN_INTERVAL_S = float(os.environ.get("HAL_BACKCHANNEL_INTERVAL_S", "5.0"))
# Volume multiplier for cue audio relative to normal TTS (0.0 = silent, 1.0 = full).
# Kept at 0.5 so backchannel bleed doesn't saturate the mic and corrupt the
# speaker-ID embedding of whoever is still talking.
VOLUME = float(os.environ.get("HAL_BACKCHANNEL_VOLUME", "0.5"))
ECHO_TAIL_S = float(os.environ.get("HAL_BACKCHANNEL_ECHO_TAIL_S", "0.4"))


class Backchannel:
    """Monitor STT partials and play filler words when user pauses mid-speech."""

    def __init__(self, tts_service):
        self._tts = tts_service
        self._last_partial: str = ""
        self._last_cue_time: float = 0.0
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._session_epoch: int = 0
        self._session_active = False
        self._self_audio_until: float = 0.0

        if FILLERS:
            logger.info("Backchannel enabled: fillers=%s, stall=%.1fs, interval=%.1fs",
                        FILLERS, STALL_TIMEOUT_S, MIN_INTERVAL_S)

    @property
    def enabled(self) -> bool:
        return len(FILLERS) > 0

    @property
    def self_audio_active(self) -> bool:
        """True while a cue is playing (plus reverb tail).

        Deliberately NOT the TTS `speaking` flag: that flag ends the running STT
        session, which is exactly what backchannel must not do.
        """
        return time.monotonic() < self._self_audio_until

    def on_partial(self, text: str) -> None:
        """Called on each STT partial. Schedules a cue if partial stalls."""
        if not FILLERS:
            return
        with self._lock:
            if text == self._last_partial:
                return
            self._last_partial = text
            self._cancel_timer()
            session_epoch = self._session_epoch
            if STALL_TIMEOUT_S <= 0:
                fire_now = True
            else:
                fire_now = False
                self._timer = threading.Timer(
                    STALL_TIMEOUT_S, self._fire_cue, args=(session_epoch,)
                )
                self._timer.daemon = True
                self._timer.start()
        if fire_now:
            self._fire_cue(session_epoch)

    def begin_session(self) -> None:
        """Mark the STT session whose partials may receive a listening cue."""
        with self._lock:
            self._session_epoch += 1
            self._session_active = True

    def _session_is_current(self, session_epoch: int) -> bool:
        with self._lock:
            return self._session_active and session_epoch == self._session_epoch

    def reset(self) -> None:
        """Reset state when STT session ends and invalidate queued cue audio."""
        with self._lock:
            self._session_epoch += 1
            self._session_active = False
            self._cancel_timer()
            self._last_partial = ""

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _fire_cue(self, session_epoch: int) -> None:
        """Check interval, then play a random filler in background thread."""
        if not self._session_is_current(session_epoch):
            logger.info("Backchannel skipped: originating STT session ended")
            return
        now = time.time()
        if (now - self._last_cue_time) < MIN_INTERVAL_S:
            logger.info("Backchannel skipped: interval cooldown (%.1fs < %.1fs)",
                        now - self._last_cue_time, MIN_INTERVAL_S)
            return
        if not self._last_partial.strip():
            return
        if self._tts is not None and self._tts.speaking:
            logger.info("Backchannel skipped: TTS is speaking")
            return
        self._last_cue_time = now
        filler = random.choice(FILLERS)
        logger.info("Backchannel: '%s'", filler)
        threading.Thread(
            target=self._play, args=(filler, session_epoch), daemon=True, name="bc-cue"
        ).start()

    def _play(self, text: str, session_epoch: int) -> None:
        """Play a short TTS cue directly, bypassing tts_service.speak()."""
        import hal.app_state as _state
        from hal import privacy
        if not self._session_is_current(session_epoch):
            logger.info("Backchannel skipped: originating STT session ended before playback")
            return
        if _state._speaker_muted or privacy.speaker_muted:
            return
        tts = self._tts
        if tts is None or tts._backend is None or not tts._backend.available or tts._sd is None:
            return
        try:
            import numpy as np
            dst_rate = tts._device_rate or 24000
            src_rate = tts._backend.sample_rate
            raw = b""
            for chunk in tts._backend.stream_pcm(
                text=text,
                voice=tts._voice,
                model=tts._model,
                speed=tts._speed,
            ):
                raw += chunk
            if len(raw) < 2:
                return
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            samples *= max(0.0, min(1.0, VOLUME))
            if dst_rate != src_rate:
                ratio = dst_rate / src_rate
                n_out = math.ceil(len(samples) * ratio)
                x_old = np.linspace(0, 1, len(samples))
                x_new = np.linspace(0, 1, n_out)
                samples = np.interp(x_new, x_old, samples).astype(np.float32)
            samples_2d = samples.reshape(-1, 1)
            # Arm the self-audio window BEFORE the first sample leaves, and size it from
            # the actual clip length.
            duration_s = len(samples) / float(dst_rate)
            with self._lock:
                if (
                    not self._session_active
                    or session_epoch != self._session_epoch
                ):
                    logger.info(
                        "Backchannel skipped: originating STT session ended before output"
                    )
                    return
                self._self_audio_until = time.monotonic() + duration_s + ECHO_TAIL_S
            try:
                with tts._stream_lock:
                    if not self._session_is_current(session_epoch):
                        logger.info(
                            "Backchannel skipped: originating STT session ended while "
                            "waiting for TTS output"
                        )
                        return
                    if _state._speaker_muted or privacy.speaker_muted:
                        return
                    stream = tts._ensure_stream(dst_rate)
                    stream.write(samples_2d)
            finally:
                with self._lock:
                    self._self_audio_until = max(
                        self._self_audio_until, time.monotonic() + ECHO_TAIL_S
                    )
            logger.info(
                "Backchannel played: '%s' (%.2fs, mic self-audio window +%.1fs)",
                text, duration_s, ECHO_TAIL_S,
            )
        except Exception as e:
            logger.warning("Backchannel play failed: %s", e)
