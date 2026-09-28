"""Hardware AEC residual-echo gate; all levels are normalized RMS.

This is an energy heuristic, not a speech classifier. Capture stays open; only
unconfirmed audio during playback/tail is replaced with silence for the provider.
Call from one audio consumer and reset when the capture stream/session changes.
"""

from collections import deque
import math

import numpy as np


class AdaptiveLiveGate:
    IDLE_FLOOR = 10 ** (-42 / 20)
    PLAYBACK_FLOOR = 10 ** (-27 / 20)
    AEC_WARMUP_S = 3.0

    def __init__(self):
        self.reset()

    def reset(self):
        self.noise = 0.003
        self.threshold = max(self.IDLE_FLOOR, self.noise * 4)
        self.speaking = False
        self._vad_speaking = False
        self._barge_allowed = False
        self.duck = False
        self.coupling_db = -8.0
        self._time = 0.0
        self._dt = 0.02
        self._tail_until = 0.0
        self._above = self._below = 0.0
        self._unduck_at = None
        self._envelope = deque()
        self._prefix = deque()
        self._prefix_samples = 0
        self._burst_peak = None
        self._rate = None
        self._risk = False
        self.speech_started = False
        self.barge_in = False
        self.replayed_samples = 0

    @property
    def risk(self):
        """Whether playback or its acoustic tail requires echo suppression."""
        return self._risk

    @property
    def buffered_samples(self):
        return self._prefix_samples

    @property
    def output_envelope(self):
        return max((value for _, value in self._envelope), default=0.0)

    def clear_duck(self):
        """Release attenuation after provider interruption without losing speech."""
        self.duck = False
        self._unduck_at = None

    def idle_threshold(self, rms, duration=None):
        """Track ambient only: ignore samples above the existing speech threshold.

        Like the demo, a sudden persistent noise jump above this threshold is
        deliberately not learned: this energy heuristic cannot distinguish it
        from speech. Such an environment needs better AEC or a speech classifier.
        """
        if duration is not None and (not math.isfinite(duration) or duration <= 0):
            raise ValueError("duration must be finite and positive")
        threshold = max(self.IDLE_FLOOR, self.noise * 4)
        if not self._vad_speaking and rms <= threshold:
            self._track_noise(rms, self._dt if duration is None else duration)
        self.threshold = max(self.IDLE_FLOOR, self.noise * 4)
        return self.threshold

    def _track_noise(self, rms, duration):
        # The demo tracks subthreshold residual echo as well as idle ambient.
        retention = 0.9 if rms < self.noise else 0.995
        alpha = 1 - retention ** (duration / 0.02)
        self.noise = min(0.05, max(0.0005, self.noise + alpha * (rms - self.noise)))

    def _remember(self, frame, rate):
        self._prefix.append(frame.copy())
        self._prefix_samples += frame.size
        limit = round(rate * 0.3)
        while self._prefix_samples > limit:
            excess = self._prefix_samples - limit
            first = self._prefix.popleft()
            if first.size > excess:
                self._prefix.appendleft(first[excess:])
                self._prefix_samples -= excess
            else:
                self._prefix_samples -= first.size

    def _clear_prefix(self):
        self._prefix.clear()
        self._prefix_samples = 0

    def process(self, data, rate, playback, output_level, playback_seconds=0.0):
        """Return flat mono int16 PCM, optionally prepended with withheld onset.

        Accept mono (N,) or (N, 1). Pass output RMS after duck gain, and actual
        local playback state. speech_started and barge_in are one-call pulses;
        consume barge_in once to interrupt locally, rather than testing speaking.
        Output can exceed input by at most 300 ms; send it once without refeeding.
        """
        frame = np.asarray(data)
        if frame.ndim == 2 and frame.shape[1] == 1:
            frame = frame[:, 0]
        if frame.dtype != np.int16 or frame.ndim != 1:
            raise ValueError("expected mono int16 PCM")
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("rate must be finite and positive")
        if not math.isfinite(output_level):
            raise ValueError("output level must be finite")
        self.speech_started = False
        self.barge_in = False
        self.replayed_samples = 0
        if frame.size == 0:
            return frame
        if self._rate is not None and rate != self._rate:
            self.reset()
        self._rate = rate
        self._dt = frame.size / rate
        self._time += self._dt
        pcm = frame.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(pcm * pcm)))
        if playback:
            self._tail_until = self._time + 0.3
        risk = bool(playback or self._time < self._tail_until)
        if risk != self._risk:
            # Do not carry an unconfirmed idle burst into a playback burst.
            self._above = 0.0
            self._burst_peak = None
            if risk:
                # Idle speech may be stale or echo at the start of playback.
                # Reconfirm against playback thresholds before opening/ducking.
                self.speaking = False
                self._vad_speaking = False
                self._barge_allowed = False
                self._below = 0.0
                self._clear_prefix()
        self._risk = risk
        level = max(0.0, float(output_level)) if playback else 0.0
        self._envelope.append((self._time, level))
        while self._envelope and self._envelope[0][0] <= self._time - 0.5:
            self._envelope.popleft()
        envelope = self.output_envelope

        if playback:
            self.threshold = max(self.PLAYBACK_FLOOR, self.noise * 5,
                                 envelope * 10 ** ((self.coupling_db + 8) / 20))
            need = 0.24
        else:
            self.threshold = max(self.IDLE_FLOOR, self.noise * 4)
            need = 0.16
        above = rms > self.threshold
        # Count VAD even during warm-up, but latch admission at its onset.
        # A rejected echo burst must end before a later burst can be admitted;
        # crossing the warm-up deadline is not itself a fresh speech onset.
        warming_up = playback and playback_seconds < self.AEC_WARMUP_S
        was_speaking = self.speaking
        if playback and not self._vad_speaking and envelope > 0.003:
            ratio = 20 * math.log10(max(rms, 1e-9) / envelope)
            if above:
                self._burst_peak = max(ratio, self._burst_peak if self._burst_peak is not None else ratio)
            else:
                if self._burst_peak is not None:
                    target = min(0.0, self._burst_peak - 8 + 2)
                    self.coupling_db += 0.7 * max(0.0, target - self.coupling_db)
                    self._burst_peak = None
                alpha = 1 - 0.95 ** (self._dt / 0.02)
                self.coupling_db += alpha * max(0.0, min(0.0, ratio) - self.coupling_db)
                self.coupling_db = max(-30.0, self.coupling_db - self._dt)
        if above:
            self._above += self._dt
            self._below = 0.0
        else:
            self._above = 0.0
            self._below += self._dt
            if not self._vad_speaking:
                self._track_noise(rms, self._dt)
        if not self._vad_speaking and self._above + 1e-9 >= need:
            self._vad_speaking = True
            self._barge_allowed = not warming_up
            self.speech_started = self._barge_allowed
            self._burst_peak = None
            if playback and self._barge_allowed:
                self.barge_in = True
                self.duck = True
                self._unduck_at = self._time + 1.5
        elif self._vad_speaking and self._below + 1e-9 >= 0.5:
            self._vad_speaking = False
            self._barge_allowed = False
        self.speaking = self._vad_speaking and self._barge_allowed
        if self.duck:
            if not risk:
                self.clear_duck()
            elif self._unduck_at is not None and self._time + 1e-9 >= self._unduck_at:
                # Match the demo's timer: first check 1.5 s after onset, then
                # every 400 ms until VAD releases after 500 ms of quiet.
                if self._vad_speaking:
                    self._unduck_at = self._time + 0.4
                else:
                    self.clear_duck()

        if warming_up or (risk and self._vad_speaking and not self._barge_allowed):
            self._clear_prefix()
            return np.zeros_like(frame)
        if risk and not self.speaking:
            self._remember(frame, rate)
            return np.zeros_like(frame)
        if risk and self.speaking and not was_speaking and self._prefix:
            # Current frame has never entered prefix, so its samples appear once.
            result = np.concatenate((*self._prefix, frame))
            self.replayed_samples = self._prefix_samples
            self._clear_prefix()
            return result
        self._clear_prefix()
        return frame
