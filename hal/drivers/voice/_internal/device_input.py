"""Device tap-to-talk controller over the shared, serialized capture owner."""

from hal.drivers.voice._internal.harness_voice import read_voice_mode
from hal.drivers.voice._internal.input_policy import device_manual_mode, device_snapshot


class DeviceTapInput:
    def __init__(self, capture, start_capture, *, read_mode=None):
        self._capture = capture
        # The pipeline supplies its existing privacy/playback admission guard.
        self._start_capture = start_capture
        self._read_mode = read_mode or read_voice_mode
        self._mode = {}

    def observe(self, mode):
        """Cache authoritative routing for nonblocking hardware edge recognition."""
        self._mode = dict(mode)

    @property
    def enabled(self):
        return device_manual_mode(self._mode)

    @property
    def active(self):
        return self._capture.active

    def start(self):
        # Never authorize a new recording from the cached hardware-edge state.
        mode = self._read_mode()
        self.observe(mode)
        if not device_manual_mode(mode):
            return False
        return self._start_capture(device_snapshot(mode))

    def finish(self):
        return self._capture.finish()

    def cancel(self):
        self._capture.cancel()
