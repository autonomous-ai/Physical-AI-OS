"""Audio embedder base class using WeSpeaker ONNX models with sliding window."""

import hashlib
from pathlib import Path
from typing import Any

import kaldi_native_fbank as knf
import numpy as np
import numpy.typing as npt
import onnxruntime as ort
from typing_extensions import override

from core.enums.audio import AudioEmbedderEnum
from core.models.audio import RawAudioEmbedding
from core.models.media import Audio
from core.perception.audio.processors import CompositeAudioProcessor
from core.perception.audio.processors.utils import AudioProcessorFactory
from core.perception.base import PredictorBase
from core.utils.common import get_or_default
from core.utils.files import ensure_downloaded
from core.utils.runtime import prepare_ort_session


class AudioEmbedder(PredictorBase[Audio, RawAudioEmbedding]):
    """Base audio embedder using WeSpeaker ONNX models.

    80-dim fbank -> chunks (see _sliding_windows) -> ONNX -> L2-normalized mean.
    """

    MODEL_NAME: AudioEmbedderEnum | None = None

    DEFAULT_MODEL_PATH: Path | None = None
    DEFAULT_REMOTE_URL: str | None = None
    DEFAULT_PROCESSOR_FACTORY: AudioProcessorFactory = AudioProcessorFactory()

    DEFAULT_WINDOW_FRAMES: int = 600
    DEFAULT_HOP_FRAMES: int = 400
    DEFAULT_CHUNK_THRESHOLD_FRAMES: int = 1000
    DEFAULT_SAMPLE_RATE: int = 16000
    DEFAULT_NUM_MEL_BINS: int = 80
    ONNX_INPUT_NAME: str = "feats"

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        processor_factory: AudioProcessorFactory | None = None,
        window_frames: int | None = None,
        hop_frames: int | None = None,
        chunk_threshold_frames: int | None = None,
        sample_rate: int | None = None,
        num_mel_bins: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        super().__init__(batch_size=batch_size)

        self._model_path: Path | None = get_or_default(model_path, self.DEFAULT_MODEL_PATH)
        self._remote_url: str | None = get_or_default(remote_url, self.DEFAULT_REMOTE_URL)
        self._processor_factory: AudioProcessorFactory = get_or_default(
            processor_factory, self.DEFAULT_PROCESSOR_FACTORY
        )
        self._window_frames: int = get_or_default(window_frames, self.DEFAULT_WINDOW_FRAMES)
        self._hop_frames: int = get_or_default(hop_frames, self.DEFAULT_HOP_FRAMES)
        self._chunk_threshold_frames: int = get_or_default(
            chunk_threshold_frames, self.DEFAULT_CHUNK_THRESHOLD_FRAMES
        )
        if self._window_frames > self._chunk_threshold_frames:
            raise ValueError(
                f"window_frames ({self._window_frames}) must be <= "
                f"chunk_threshold_frames ({self._chunk_threshold_frames})"
            )
        self._sample_rate: int = get_or_default(sample_rate, self.DEFAULT_SAMPLE_RATE)
        self._num_mel_bins: int = get_or_default(num_mel_bins, self.DEFAULT_NUM_MEL_BINS)

        self._session: ort.InferenceSession | None = None
        self._processor: CompositeAudioProcessor | None = None
        self._input_name: str = self.ONNX_INPUT_NAME
        self._model_version: str | None = None

    @property
    def model_version(self) -> str | None:
        """Identity of the loaded weights (``<name>:<sha256[:12]>``), or None until started.

        Lets clients detect stale stored embeddings after a checkpoint swap.
        """
        return self._model_version

    @override
    def _start_impl(self) -> None:
        if self._session is not None:
            self._logger.info("Already running")
            return

        if self._model_path is None:
            raise RuntimeError(f"{self.__class__.__name__} has no model_path configured")

        self._model_path = ensure_downloaded(self._model_path, remote=self._remote_url)
        self._processor = self._processor_factory.create()
        self._processor.start()
        self._logger.info("Loading audio embedder from %s", self._model_path)
        session = prepare_ort_session(self._model_path)
        input_names = [i.name for i in session.get_inputs()]
        if self.ONNX_INPUT_NAME in input_names:
            self._input_name = self.ONNX_INPUT_NAME
        else:
            self._input_name = input_names[0]
            self._logger.warning(
                "ONNX input %r not found (model inputs: %s) — falling back to %r",
                self.ONNX_INPUT_NAME, input_names, self._input_name,
            )
        session.run(None, {self._input_name: np.zeros(
            (self._batch_size, self._window_frames, self._num_mel_bins), dtype=np.float32,
        )})
        self._session = session
        self._model_version = self._compute_model_version()
        self._logger.info(
            "Audio embedder started (input=%r, model_version=%s)",
            self._input_name, self._model_version,
        )

    def _compute_model_version(self) -> str:
        """Fingerprint the loaded weights: ``<MODEL_NAME>:<sha256(file)[:12]>``.

        Hashes file content so a same-dimension checkpoint swap still changes the version.
        """
        name = self.MODEL_NAME.value if self.MODEL_NAME is not None else self.__class__.__name__
        digest = "unknown"
        if self._model_path is not None:
            try:
                h = hashlib.sha256()
                with open(self._model_path, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                digest = h.hexdigest()[:12]
            except OSError as e:
                self._logger.warning("Failed to hash model weights: %s", e)
        return f"{name}:{digest}"

    @override
    def _stop_impl(self) -> None:
        self._session = None
        if self._processor is not None:
            self._processor.stop()
            self._processor = None
        self._logger.info("Audio embedder stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._session is not None and self._processor is not None

    @override
    def preprocess(self, input: list[Audio]) -> list[Audio]:
        """Run the composite audio processor on each input."""
        return [self._processor.process(audio) for audio in input]

    def _compute_fbank(self, audio: Audio) -> npt.NDArray[np.float32]:
        """Compute fbank features using kaldi-native-fbank.

        Returns:
            Feature array of shape (T, num_mel_bins) after CMN.
        """
        opts = knf.FbankOptions()
        opts.frame_opts.samp_freq = float(self._sample_rate)
        opts.frame_opts.frame_length_ms = 25.0
        opts.frame_opts.frame_shift_ms = 10.0
        opts.frame_opts.dither = 0.0
        opts.frame_opts.window_type = "hamming"
        opts.mel_opts.num_bins = self._num_mel_bins

        fbank = knf.OnlineFbank(opts)
        fbank.accept_waveform(self._sample_rate, audio.waveform)
        fbank.input_finished()

        num_frames = fbank.num_frames_ready
        if num_frames == 0:
            return np.zeros((0, self._num_mel_bins), dtype=np.float32)

        feat = np.stack(
            [np.array(fbank.get_frame(i), dtype=np.float32) for i in range(num_frames)]
        )  # (T, num_mel_bins)

        # Cepstral mean normalization
        feat = feat - feat.mean(axis=0)
        return feat

    def _sliding_windows(
        self, feat: npt.NDArray[np.float32], *, use_sliding_window: bool = True
    ) -> npt.NDArray[np.float32]:
        """Split fbank features into chunks for per-chunk embedding.

        One (1, T, M) chunk when sliding is off or T <= chunk_threshold_frames;
        otherwise windows of window_frames with hop_frames stride (last one shifted back).

        Args:
            feat: Shape (T, num_mel_bins), ~100 frames per second.
            use_sliding_window: False never splits (enroll path).

        Returns:
            Array of shape (N, W, num_mel_bins) — W == T in the single-chunk case,
            W == window_frames in the sliding case.
        """
        T = feat.shape[0]

        if T == 0:
            return np.zeros((1, self._window_frames, self._num_mel_bins), dtype=np.float32)

        if not use_sliding_window or T <= self._chunk_threshold_frames:
            return feat[np.newaxis]  # (1, T, M)

        windows: list[npt.NDArray[np.float32]] = []
        start = 0
        while start + self._window_frames <= T:
            windows.append(feat[start : start + self._window_frames])
            start += self._hop_frames

        last_end = start - self._hop_frames + self._window_frames
        if last_end < T:
            windows.append(feat[T - self._window_frames : T])

        return np.stack(windows)  # (N, W, M)

    def _infer_batch(
        self, windows: npt.NDArray[np.float32]
    ) -> npt.NDArray[np.float32]:
        """Run ONNX inference on windows in mini-batches.

        Args:
            windows: Shape (N, window_frames, num_mel_bins).

        Returns:
            L2-normalized embeddings of shape (N, D).
        """
        parts: list[npt.NDArray[np.float32]] = []

        for i in range(0, len(windows), self._batch_size):
            batch = windows[i : i + self._batch_size]  # (B, W, M)
            with self._gpu_lock:
                (output,) = self._session.run(None, {self._input_name: batch})
            output = np.asarray(output, dtype=np.float32)  # (B, D)

            norms = np.linalg.norm(output, axis=1, keepdims=True)
            norms = np.maximum(norms, 1e-10)
            output = output / norms

            parts.append(output)

        return np.concatenate(parts, axis=0)  # (N, D)

    def _mean_aggregate(
        self, chunk_embeddings: npt.NDArray[np.float32]
    ) -> npt.NDArray[np.float32]:
        """Mean-aggregate chunk embeddings with L2 normalization.

        Args:
            chunk_embeddings: L2-normalized embeddings of shape (N, D).

        Returns:
            L2-normalized mean embedding of shape (D,).
        """
        mean = chunk_embeddings.mean(axis=0)
        norm = np.linalg.norm(mean)

        return (mean / (norm + 1e-10)).astype(np.float32)

    @override
    def _predict_impl(
        self,
        input: list[Audio],
        *,
        preprocess: bool = True,
        use_sliding_window: bool = True,
        **kwargs: Any,
    ) -> list[RawAudioEmbedding]:
        """Embed a batch of audio inputs; ``use_sliding_window=False`` embeds each whole utterance."""
        if preprocess:
            input = self.preprocess(input)

        results: list[RawAudioEmbedding] = []

        for audio in input:
            feat = self._compute_fbank(audio)
            windows = self._sliding_windows(
                feat, use_sliding_window=use_sliding_window
            )
            chunk_embeddings = self._infer_batch(windows)
            embedding = self._mean_aggregate(chunk_embeddings)

            results.append(
                RawAudioEmbedding(
                    embedding=embedding,
                    chunk_embeddings=chunk_embeddings,
                )
            )

        return results
