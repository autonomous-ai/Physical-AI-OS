"""Base audio emotion predictor — classifies emotion from audio waveforms.

Takes a batch of Audio inputs, preprocesses (mono + resample), runs ONNX
inference with zero-padding for variable-length batching, and returns raw
expression probability distributions.

Concrete subclasses (Emotion2Vec) override class-level defaults
(model path, labels file, sample rate).
"""

from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import onnxruntime as ort
from typing_extensions import override

from core.models.audio_emotion import RawAudioEmotionDetection
from core.models.media import Audio
from core.perception.audio.processors import CompositeAudioProcessor
from core.perception.audio.processors.utils import AudioProcessorFactory
from core.perception.audio_emotion.length import fit_length
from core.perception.base import PredictorBase
from core.utils.common import get_or_default
from core.utils.files import ensure_downloaded
from core.utils.runtime import prepare_ort_session


class AudioEmotionRecognizer(PredictorBase[Audio, RawAudioEmotionDetection]):
    """Base class for audio emotion classifiers.

    Subclasses override class-level defaults. The base handles ONNX
    lifecycle, preprocessing, and inference.
    """

    DEFAULT_MODEL_PATH: Path | None = None
    DEFAULT_REMOTE_URL: str | None = None
    DEFAULT_LABELS_PATH: Path | None = None
    DEFAULT_SAMPLE_RATE: int = 16000
    DEFAULT_PROCESSOR_FACTORY: AudioProcessorFactory = AudioProcessorFactory(
        target_sample_rate=16000,
        enable_resample=True,
        enable_high_pass=False,
        enable_noise_reduce=False,
        enable_vad=False,
        enable_rms_normalize=False,
    )
    ONNX_INPUT_NAME: str = "input"
    ONNX_OUTPUT_NAME: str = "logits"
    # Hard input-length bound (seconds). Must match the TensorRT profile built
    # in _start_impl — see length.py for why.
    MIN_AUDIO_S: float = 2.0
    MAX_AUDIO_S: float = 8.0
    # Batched inputs are zero-padded to the longest member, so a short clip
    # batched with a long one would be classified as mostly silence.
    MAX_BATCH_SIZE: int = 1

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        labels_path: Path | None = None,
        processor_factory: AudioProcessorFactory | None = None,
        sample_rate: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        super().__init__(batch_size=batch_size)

        model_path = get_or_default(model_path, self.DEFAULT_MODEL_PATH)
        if model_path is None:
            raise RuntimeError("model_path must not be None")

        labels_path = get_or_default(labels_path, self.DEFAULT_LABELS_PATH)
        if labels_path is None:
            raise RuntimeError("labels_path must not be None")

        self._model_path: Path = model_path
        self._remote_url: str | None = get_or_default(remote_url, self.DEFAULT_REMOTE_URL)
        self._labels_path: Path = labels_path
        self._processor_factory: AudioProcessorFactory = get_or_default(
            processor_factory, self.DEFAULT_PROCESSOR_FACTORY
        )
        self._sample_rate: int = get_or_default(sample_rate, self.DEFAULT_SAMPLE_RATE)

        self._class_names: list[str] = []
        self._running: bool = False
        self._session: ort.InferenceSession | None = None
        self._processor: CompositeAudioProcessor | None = None

        if self._batch_size > self.MAX_BATCH_SIZE:
            self._logger.warning(
                "batch_size=%d exceeds MAX_BATCH_SIZE=%d for SER; clamping",
                self._batch_size,
                self.MAX_BATCH_SIZE,
            )
            self._batch_size = self.MAX_BATCH_SIZE

    @property
    def class_names(self) -> list[str]:
        return self._class_names

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def min_samples(self) -> int:
        return int(self._sample_rate * self.MIN_AUDIO_S)

    @property
    def max_samples(self) -> int:
        return int(self._sample_rate * self.MAX_AUDIO_S)

    @override
    def _start_impl(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._model_path = ensure_downloaded(self._model_path, remote=self._remote_url)
        self._processor = self._processor_factory.create()
        self._processor.start()
        self._logger.info("Loading model from %s", self._model_path)
        # Warm up at both ends of the input bound. Every request is fit to
        # [min_samples, max_samples] at batch 1, and both shapes sit inside
        # the engine's shape range, so no request can trigger a TensorRT
        # rebuild (#492). With a cached engine (prod: 32000-549120 samples,
        # batch 1-4) nothing is rebuilt at startup either; on a host without
        # a cache the builds happen here, before any traffic.
        name = self.ONNX_INPUT_NAME
        warmup = [
            {name: np.zeros((1, self.min_samples), dtype=np.float32)},
            {name: np.zeros((1, self.max_samples), dtype=np.float32)},
        ]
        self._session = prepare_ort_session(self._model_path, warmup_inputs=warmup)
        self._class_names = self._load_classes(self._labels_path)
        self._running = True
        self._logger.info("Ready — %d emotion classes", len(self._class_names))

    @override
    def _stop_impl(self) -> None:
        self._session = None
        if self._processor is not None:
            self._processor.stop()
            self._processor = None
        self._running = False
        self._logger.info("Stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._session is not None and self._processor is not None

    @override
    def preprocess(self, input: list[Audio]) -> list[Audio]:
        """Run the composite audio processor on each input."""
        return [self._processor.process(audio) for audio in input]

    @override
    def _predict_impl(
        self,
        input: list[Audio],
        *,
        preprocess: bool = True,
        **kwargs: Any,
    ) -> list[RawAudioEmotionDetection]:
        """Classify emotion for a batch of audio utterances.

        Bounds each waveform to [MIN_AUDIO_S, MAX_AUDIO_S], zero-pads to max length in batch,
        stacks into [N, T_max], and runs ONNX inference in one pass.
        """
        if preprocess:
            input = self.preprocess(input)

        waveforms = []
        for audio in input:
            n = audio.waveform.shape[0]
            if n > self.max_samples or n < self.min_samples:
                self._logger.info(
                    "SER input %.2fs outside [%.1f, %.1f]s — fitting",
                    n / self._sample_rate,
                    self.MIN_AUDIO_S,
                    self.MAX_AUDIO_S,
                )
            waveforms.append(fit_length(audio.waveform, self.min_samples, self.max_samples))
        max_t = max(w.shape[0] for w in waveforms)

        batch = np.zeros((len(waveforms), max_t), dtype=np.float32)
        for i, w in enumerate(waveforms):
            batch[i, : w.shape[0]] = w

        with self._gpu_lock:
            raw_outputs: list[npt.NDArray[np.float32]] = cast(
                list[npt.NDArray[np.float32]],
                self._session.run([self.ONNX_OUTPUT_NAME], {self.ONNX_INPUT_NAME: batch}),
            )
        return self._postprocess_batch(raw_outputs, len(input))

    def _postprocess_batch(
        self, raw_outputs: list[npt.NDArray[np.float32]], N: int
    ) -> list[RawAudioEmotionDetection]:
        """Convert batched ONNX output to per-sample RawAudioEmotionDetection.

        Softmax is baked into the ONNX graph — output is probs directly.
        """
        probs: npt.NDArray[np.float32] = np.asarray(raw_outputs[0], dtype=np.float32)

        return [RawAudioEmotionDetection(expression_probs=probs[i]) for i in range(N)]

    @staticmethod
    def _load_classes(classes_path: Path) -> list[str]:
        return classes_path.read_text().strip().split("\n")
