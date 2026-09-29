"""Voice activity detection filter — strips non-voice and gates low-quality audio."""

from typing import Any

import numpy as np
import numpy.typing as npt
import torch
from silero_vad import get_speech_timestamps, load_silero_vad
from typing_extensions import override

from core.models.media import Audio
from core.perception.base.predictor import gpu_lock
from core.perception.audio.processors.exceptions import (
    REJECT_LOW_VOICE_RATIO,
    REJECT_TOO_SHORT,
    REJECT_VAD_REMOVED_ALL,
    PreprocessRejected,
)

from .base import AudioProcessorBase


class VoiceActivityFilter(AudioProcessorBase):
    """Strip leading/trailing non-voice (silero-vad) and reject low-quality audio.

    Raises PreprocessRejected when no speech remains, it is too short, or the voice ratio is too low.
    """

    def __init__(
        self,
        min_duration_sec: float = 0.5,
        min_voice_ratio: float = 0.4,
        min_speech_sec: float = 0.2,
        min_silence_sec: float = 0.3,
        speech_pad_sec: float = 0.1,
    ) -> None:
        super().__init__()
        self._min_duration_sec: float = min_duration_sec
        self._min_voice_ratio: float = min_voice_ratio
        self._min_speech_sec: float = min_speech_sec
        self._min_silence_sec: float = min_silence_sec
        self._speech_pad_sec: float = speech_pad_sec

        self._model: Any = None

    @override
    def _start_impl(self) -> None:
        if self._model is not None:
            self._logger.info("Already running")
            return
        with gpu_lock:
            self._model = load_silero_vad()
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
        """Run silero-vad and return [{start, end}] in sample indices."""
        try:
            with gpu_lock:
                ts = get_speech_timestamps(
                    torch.from_numpy(waveform),
                    self._model,
                    sampling_rate=sample_rate,
                    min_speech_duration_ms=int(self._min_speech_sec * 1000),
                    min_silence_duration_ms=int(self._min_silence_sec * 1000),
                    speech_pad_ms=int(self._speech_pad_sec * 1000),
                    return_seconds=False,
                )
            return list(ts) if ts else []
        except Exception as exc:
            self._logger.warning("silero-vad inference failed: %s", exc)
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

        # Merge overlapping intervals to compute voice ratio accurately
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
