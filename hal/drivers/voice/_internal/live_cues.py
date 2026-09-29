"""Live turn feedback using the same HW emotion calls as regular realtime turns."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time

from hal.drivers.voice._internal.speaker_decorate import merge_stt_hypothesis

logger = logging.getLogger("hal.voice")


class LiveVoiceCues:
    def __init__(self, *, timeout_s=25.0, clock=time.monotonic,
                 show=None, clear=None, addressed=None):
        self._executor = None
        self._pending = None
        if show is None or clear is None:
            # Emotion effects can join a hardware thread. Keep those waits off
            # the continuously streaming microphone, preserving call order.
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-emotion")
            show = self._show_emotion
            clear = self._clear_emotion
        self._show = show
        self._clear = clear
        self._clock = clock
        self._addressed_check = addressed or (lambda text: True)
        self._timeout_s = max(1.0, timeout_s)
        self._lock = threading.RLock()
        self._owner = object()
        self._phase = ""
        self._key = ""
        self._retired = deque(maxlen=64)
        self._last_input = 0.0
        self._transcript = ""
        self._addressed = False
        self._ended = False
        self._thinking_at = 0.0
        self._closed = False

    @staticmethod
    def _show_emotion(owner, phase):
        from hal.drivers.voice.voice_service import VoiceService
        from hal.drivers.voice._internal.realtime_turn import _thinking_cue_start

        if phase == "thinking":
            _thinking_cue_start()
        else:
            VoiceService._set_emotion_local("listening")

    @staticmethod
    def _clear_emotion(owner):
        from hal import app_state
        from hal.drivers.voice._internal.realtime_turn import _thinking_cue_clear

        _thinking_cue_clear()
        app_state.clear_listening_cue()

    def _paint(self, phase):
        if self._closed or phase == self._phase:
            return
        self._phase = phase

        def apply(owner=self._owner):
            try:
                if phase:
                    self._show(owner, phase)
                else:
                    self._clear(owner)
            except Exception as exc:
                logger.warning("[live] HW emotion cue failed: %s", exc)

        if self._executor is None:
            apply()
        else:
            self._pending = self._executor.submit(apply)

    def input(self, key, endpoint_at=None, *, transcript="", transcript_finished=False):
        """Use recognized words and addressing evidence, never raw mic activity."""
        with self._lock:
            if self._closed or not key or key in self._retired:
                return
            if key != self._key:
                if self._key:
                    self._retired.append(self._key)
                self._paint("")
                self._owner = object()
                self._key = key
                self._transcript = ""
                self._addressed = False
                self._ended = False
            if transcript.strip():
                self._transcript = merge_stt_hypothesis(self._transcript, transcript)
                self._last_input = self._clock()
            self._ended |= endpoint_at is not None or transcript_finished
            self._update_emotion()

    def _update_emotion(self):
        if not self._transcript:
            return
        self._addressed = self._addressed or self._addressed_check(self._transcript)
        if not self._addressed:
            return
        if not self._phase:
            self._paint("listening")
        if self._ended and self._phase != "thinking":
            self._thinking_at = self._clock()
            self._paint("thinking")

    def tick(self):
        """Recheck focus and expire cues; silence never starts an emotion."""
        with self._lock:
            if self._closed:
                return
            now = self._clock()
            expired = (
                self._phase == "thinking" and now - self._thinking_at >= self._timeout_s
            ) or (
                self._phase != "thinking" and self._transcript
                and now - self._last_input >= 8.0
            )
            if expired:
                if self._key:
                    self._retired.append(self._key)
                self._key = ""
                self._transcript = ""
                self._paint("")
            else:
                self._update_emotion()

    def finish(self, key):
        """Old replies/terminals must not clear a newer user's cue."""
        with self._lock:
            if self._closed or key != self._key:
                return
            if key:
                self._retired.append(key)
            self._key = ""
            self._transcript = ""
            self._paint("")
            pending = self._pending
        # Finish before playback starts, so queued thinking cannot overwrite
        # the speaking emotion. This runs on the output pump, never the mic.
        if pending is not None:
            pending.result()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._paint("")
            self._closed = True
        if self._executor is not None:
            self._executor.shutdown(wait=True)
