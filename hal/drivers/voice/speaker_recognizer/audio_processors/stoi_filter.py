"""Speech-intelligibility gate (SQUIM-STOI) — rejects noisy / broken-voice audio."""

from __future__ import annotations

import os
from typing import Any, Optional

import numpy as np
import numpy.typing as npt
from typing_extensions import override

from .base import Audio, AudioProcessorBase, gpu_lock
from .exceptions import REJECT_LOW_INTELLIGIBILITY, PreprocessRejected

DEFAULT_THRESHOLD: float = 0.70
DEFAULT_CHUNK_SEC: float = 5.0
DEFAULT_MIN_TAIL_SEC: float = 1.0


class SpeechIntelligibilityFilter(AudioProcessorBase):
    """STOI quality gate — reject a clip whose mean chunk STOI < ``threshold``."""

    def __init__(
        self,
        model_path: str,
        threshold: float = DEFAULT_THRESHOLD,
        chunk_sec: float = DEFAULT_CHUNK_SEC,
        expected_sample_rate: int = 16000,
    ) -> None:
        super().__init__()
        self._model_path: str = model_path
        self._threshold: float = float(threshold)
        self._chunk_sec: float = float(chunk_sec)
        self._expected_sr: int = int(expected_sample_rate)
        self._session: Any = None
        self._input_name: Optional[str] = None
        self.last_score: float = float("nan")

    @override
    def _start_impl(self) -> None:
        if self._session is not None:
            return
        if not os.path.isfile(self._model_path):
            raise FileNotFoundError(f"STOI model not found: {self._model_path}")

        import onnxruntime as ort

        opts = ort.SessionOptions()
        # The pooled arena reserves large slabs and never returns them, so peak
        # RSS would ratchet up to the worst clip seen. Off keeps memory flat.
        opts.enable_cpu_mem_arena = False
        with gpu_lock:
            self._session = ort.InferenceSession(
                self._model_path, opts, providers=["CPUExecutionProvider"],
            )
        self._input_name = self._session.get_inputs()[0].name
        self._running = True
        self._logger.info(
            "STOI gate started (model=%s threshold=%.2f chunk=%.1fs)",
            os.path.basename(self._model_path), self._threshold, self._chunk_sec,
        )

    @override
    def _stop_impl(self) -> None:
        self._session = None
        self._input_name = None
        self._running = False
        self._logger.info("STOI gate stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._session is not None

    def _split_chunks(
        self, waveform: npt.NDArray[np.float32]
    ) -> list[npt.NDArray[np.float32]]:
        """Fixed ``chunk_sec`` windows (no overlap); drop a too-short trailing tail."""
        chunk_n = max(1, int(self._expected_sr * self._chunk_sec))
        min_tail = int(self._expected_sr * min(DEFAULT_MIN_TAIL_SEC, self._chunk_sec / 2.0))
        n = waveform.shape[0]
        chunks: list[npt.NDArray[np.float32]] = []
        for start in range(0, n, chunk_n):
            piece = waveform[start:start + chunk_n]
            if piece.shape[0] < min_tail and chunks:
                break
            chunks.append(piece)
            if start + chunk_n >= n:
                break
        return chunks or [waveform]

    def _score(self, chunk: npt.NDArray[np.float32]) -> float:
        """STOI estimate for one chunk; NaN on inference failure (→ rejects)."""
        blob = np.ascontiguousarray(chunk.reshape(1, -1), dtype=np.float32)
        try:
            with gpu_lock:
                out = self._session.run(None, {self._input_name: blob})
            return float(np.asarray(out[0]).ravel()[0])
        except Exception as exc:
            self._logger.warning("STOI inference failed on a chunk: %s", exc)
            return float("nan")

    @override
    def _process_impl(self, input: Audio) -> Audio:
        wf = input.waveform
        if wf.shape[0] == 0:
            return input  # nothing to score (VAD-empty handled upstream)

        scores = np.asarray(
            [self._score(c) for c in self._split_chunks(wf)], dtype=np.float64
        )
        finite = scores[~np.isnan(scores)]
        mean_score = float(np.mean(finite)) if finite.size else float("nan")
        self.last_score = mean_score

        if not (mean_score >= self._threshold):
            duration = wf.shape[0] / float(input.sample_rate)
            raise PreprocessRejected(
                REJECT_LOW_INTELLIGIBILITY,
                input_duration_sec=duration,
                stripped_duration_sec=duration,
                stoi_score=(mean_score if not np.isnan(mean_score) else 0.0),
                stoi_threshold=self._threshold,
            )

        self._logger.debug(
            "STOI gate pass: mean=%.3f (chunks=%d)", mean_score, scores.size
        )
        return input
