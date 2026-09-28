"""Serialize live barge-in with pending reply enqueueing.

Only provider-confirmed interruption cancels a reply; local energy detection
ducks playback reversibly. Provider user-turn identities prevent late fragments
from restarting a cancelled reply.
"""
from collections import deque
from threading import RLock


class LiveReplyGuard:
    def __init__(self):
        self._lock = RLock()
        self._cancelled = deque(maxlen=32)
        self._current = None

    def observe(self, key):
        with self._lock:
            if key in self._cancelled:
                return False
            self._current = key
            return True

    def allowed(self, key):
        with self._lock:
            return key not in self._cancelled

    def play(self, key, callback):
        # Hold through enqueue so cancellation cannot race a late queue write.
        with self._lock:
            if key in self._cancelled:
                return False
            self._current = key
            callback()
            return True

    def cancel(self, stop, key=None):
        with self._lock:
            target = self._current if key is None else key
            if target is not None and target not in self._cancelled:
                self._cancelled.append(target)
            # A late provider cancellation for an older turn must not stop
            # a newer reply that already owns the speaker.
            affected = key is None or target == self._current
            if affected:
                stop()
            return affected
