"""Voice activity detection filter — strips non-voice and gates low-quality audio."""

from typing import Any

import numpy as np
import numpy.typing as npt
from typing_extensions import override

from hal.drivers.voice.ten_vad_lite import TenVad

from .base import Audio, AudioProcessorBase, gpu_lock
from .exceptions import (
    REJECT_LOW_VOICE_RATIO,
    REJECT_TOO_SHORT,
    REJECT_VAD_REMOVED_ALL,
    PreprocessRejected,
)

DEFAULT_MIN_DURATION_SEC: float = 0.5
# Lowered from 0.4 (the silero-era value): the speaker-band gate below removes
# non-speech from *inside* the kept span, which splits segments and mechanically
# lowers this ratio. At 0.4 it rejected clips that hold plenty of speech.
DEFAULT_MIN_VOICE_RATIO: float = 0.25
DEFAULT_MIN_SPEECH_SEC: float = 0.2
DEFAULT_MIN_SILENCE_SEC: float = 0.3
DEFAULT_SPEECH_PAD_SEC: float = 0.1
# TEN-VAD speech-probability threshold.
DEFAULT_SPEECH_PROB_THRESHOLD: float = 0.5

# TEN-VAD is trained for 16 kHz only (16 ms hops). The Resampler upstream in the
# pipeline already normalises to this rate.
REQUIRED_SAMPLE_RATE: int = 16000

# Every VAD fires on doors, taps and room tone.
DEFAULT_SPEAKER_BAND: bool = True
DEFAULT_MAX_LEVEL_DROP_DB: float | None = 20.0


class VoiceActivityFilter(AudioProcessorBase):
    """Strip leading/trailing non-voice regions and reject low-quality audio."""

    def __init__(
        self,
        min_duration_sec: float = DEFAULT_MIN_DURATION_SEC,
        min_voice_ratio: float = DEFAULT_MIN_VOICE_RATIO,
        min_speech_sec: float = DEFAULT_MIN_SPEECH_SEC,
        min_silence_sec: float = DEFAULT_MIN_SILENCE_SEC,
        speech_pad_sec: float = DEFAULT_SPEECH_PAD_SEC,
        speech_prob_threshold: float = DEFAULT_SPEECH_PROB_THRESHOLD,
        speaker_band: bool = DEFAULT_SPEAKER_BAND,
        max_level_drop_db: float | None = DEFAULT_MAX_LEVEL_DROP_DB,
    ) -> None:
        super().__init__()
        self._min_duration_sec: float = min_duration_sec
        self._min_voice_ratio: float = min_voice_ratio
        self._min_speech_sec: float = min_speech_sec
        self._min_silence_sec: float = min_silence_sec
        self._speech_pad_sec: float = speech_pad_sec
        self._speech_prob_threshold: float = float(speech_prob_threshold)
        self._speaker_band: bool = speaker_band
        self._max_level_drop_db: float | None = max_level_drop_db

        self._model: Any = None

    @override
    def _start_impl(self) -> None:
        if self._model is not None:
            self._logger.info("Already running")
            return
        # No GPU involved (onnxruntime CPU provider), but the lock is kept so
        # model load stays serialised with the rest of the processors.
        with gpu_lock:
            self._model = TenVad()
        self._running = True
        self._logger.info("Processor started")

    @override
    def _stop_impl(self) -> None:
        self._model = None
        self._running = False
        self._logger.info("Processor stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._model is not None

    def _get_speech_timestamps(
        self, waveform: npt.NDArray[np.float32], sample_rate: int
    ) -> list[dict]:
        """Run TEN-VAD and return [{start, end}] in sample indices."""
        if sample_rate != REQUIRED_SAMPLE_RATE:
            self._logger.warning(
                "ten-vad requires %d Hz audio, got %d Hz — treating as no speech",
                REQUIRED_SAMPLE_RATE,
                sample_rate,
            )
            return []
        try:
            samples = np.asarray(waveform, dtype=np.float32) * np.float32(32768.0)
            # gpu_lock is held across inference for the same reason the silero stage
            # held it.
            with gpu_lock:
                ts = self._model.get_speech_timestamps(
                    samples,
                    sample_rate=sample_rate,
                    threshold=self._speech_prob_threshold,
                    min_speech_duration_ms=int(self._min_speech_sec * 1000),
                    min_silence_duration_ms=int(self._min_silence_sec * 1000),
                    speech_pad_ms=int(self._speech_pad_sec * 1000),
                    return_seconds=False,
                    speaker_band=self._speaker_band,
                    max_level_drop_db=self._max_level_drop_db,
                )
            return list(ts) if ts else []
        except Exception as exc:
            self._logger.warning("ten-vad inference failed: %s", exc)
            return []

    def _strip_nonvoice(
        self, waveform: npt.NDArray[np.float32], sample_rate: int
    ) -> tuple[npt.NDArray[np.float32], float]:
        """Trim leading/trailing non-voice, return (stripped, voice_ratio)."""
        if waveform.shape[0] == 0:
            return np.zeros(0, dtype=np.float32), 0.0

        segs = self._get_speech_timestamps(waveform, sample_rate)
        if not segs:
            return np.zeros(0, dtype=np.float32), 0.0

        first_start: int = max(0, int(segs[0].get("start", 0)))
        last_end: int = min(waveform.shape[0], int(segs[-1].get("end", waveform.shape[0])))
        if last_end <= first_start:
            return np.zeros(0, dtype=np.float32), 0.0

        stripped: npt.NDArray[np.float32] = waveform[first_start:last_end]

        intervals: list[tuple[int, int]] = []
        for ts in segs:
            s: int = max(first_start, int(ts.get("start", 0)))
            e: int = min(last_end, int(ts.get("end", 0)))
            if e > s:
                intervals.append((s, e))
        intervals.sort()

        speech_samples: int = 0
        prev_end: int = first_start
        for s, e in intervals:
            s = max(s, prev_end)
            if e > s:
                speech_samples += e - s
                prev_end = e

        ratio: float = float(speech_samples) / float(max(1, stripped.shape[0]))
        return stripped.astype(np.float32, copy=False), min(1.0, max(0.0, ratio))

    @override
    def _process_impl(self, input: Audio) -> Audio:
        if input.waveform.shape[0] == 0:
            return input

        input_duration: float = input.waveform.shape[0] / float(input.sample_rate)

        stripped, voice_ratio = self._strip_nonvoice(input.waveform, input.sample_rate)
        stripped_duration: float = stripped.shape[0] / float(input.sample_rate)

        if stripped.shape[0] == 0:
            raise PreprocessRejected(
                REJECT_VAD_REMOVED_ALL,
                input_duration_sec=input_duration,
                stripped_duration_sec=0.0,
                voice_ratio=0.0,
                min_duration_sec=self._min_duration_sec,
                min_voice_ratio=self._min_voice_ratio,
            )

        if stripped_duration < self._min_duration_sec:
            raise PreprocessRejected(
                REJECT_TOO_SHORT,
                input_duration_sec=input_duration,
                stripped_duration_sec=stripped_duration,
                voice_ratio=voice_ratio,
                min_duration_sec=self._min_duration_sec,
                min_voice_ratio=self._min_voice_ratio,
            )

        if voice_ratio < self._min_voice_ratio:
            raise PreprocessRejected(
                REJECT_LOW_VOICE_RATIO,
                input_duration_sec=input_duration,
                stripped_duration_sec=stripped_duration,
                voice_ratio=voice_ratio,
                min_duration_sec=self._min_duration_sec,
                min_voice_ratio=self._min_voice_ratio,
            )

        return Audio(waveform=stripped, sample_rate=input.sample_rate)
