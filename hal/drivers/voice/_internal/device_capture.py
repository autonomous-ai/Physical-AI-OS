"""Local tap feedback and bounded buffering, independent of cloud STT latency."""

import logging
import time

logger = logging.getLogger("hal.voice")


class DeviceCapture:
    """Own local capture feedback; transcript delivery remains with the pipeline."""

    def __init__(self, capture, tts, set_capturing, valid):
        self.capture = capture
        self.tts = tts
        self.set_capturing = set_capturing
        self.valid = valid
        self.ready = False
        self.stopped = False
        self.finished_at = None

    def _cue(self, finished=False):
        edge = "finish" if finished else "ready"
        logger.info("[tap-latency] event=%s_cue_begin at=%.6f", edge, time.monotonic())
        if self.tts:
            self.tts.play_device_capture_chime(finished=finished)
        logger.info("[tap-latency] event=%s_cue_done at=%.6f", edge, time.monotonic())

    def stop(self):
        """Acknowledge local end of input without waiting for final STT text."""
        if self.ready and not self.stopped:
            self.stopped = True
            self.finished_at = time.monotonic()
            self.set_capturing(False)
            if self.capture.cancelled.is_set():
                return
            logger.info("[tap-latency] event=finish_observed at=%.6f", self.finished_at)
            self._cue(finished=True)

    def prepare(self, mic, frame_size, device_rate, connected, convert, *, timeout=10):
        """Return post-cue PCM, or None on invalid/failed/expired capture.

        Opening arecord only spawns its process. Read a frame before reporting
        readiness, then buffer speech during the network handshake. Preserve
        every subsequent frame, including speech overlapping the short cue;
        the shared playback path supplies its normal AEC reference. A finish tap stops local reads even if STT is still connecting.
        The timeout bounds memory to at most ten seconds of PCM.
        """
        if self.capture.finished.is_set() or not self.valid():
            return None
        # Prove the recorder is live with 10 ms, not a full STT upload frame.
        mic.read(min(frame_size, max(1, device_rate // 100)))
        if self.capture.finished.is_set() or self.capture.cancelled.is_set():
            return None
        logger.info("[tap-latency] event=mic_ready at=%.6f", time.monotonic())
        self.set_capturing(True)
        if self.capture.cancelled.is_set() or self.capture.finished.is_set():
            return None
        self.ready = True
        self._cue()
        frames = []
        deadline = time.monotonic() + timeout
        checked_at = 0.0
        max_frames = max(1, int(timeout * device_rate / frame_size))
        while True:
            now = time.monotonic()
            if self.capture.cancelled.is_set():
                return None
            if self.capture.finished.is_set():
                self.stop()
            if now - checked_at >= 0.25:
                checked_at = now
                if not self.valid():
                    self.capture.cancelled.set()
                    return None
            if connected.is_set():
                return frames
            if now >= deadline or len(frames) >= max_frames:
                self.capture.cancelled.set()
                return None
            if self.stopped:
                connected.wait(timeout=0.02)
                continue
            data, overflowed = mic.read(frame_size)
            if not overflowed:
                frames.append(convert(data))
