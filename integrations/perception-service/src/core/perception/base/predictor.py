import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

from core.utils.common import get_or_default

INPUT_T = TypeVar("INPUT_T")
OUTPUT_T = TypeVar("OUTPUT_T")
PREDICTOR_T = TypeVar("PREDICTOR_T")

# Global GPU lock: any GPU work (inference, VAD, model load/warmup) must hold it to
# avoid CUDA stream collisions, cuDNN races and OOM from concurrent allocations.
gpu_lock: threading.RLock = threading.RLock()


class PredictorFactory(Generic[PREDICTOR_T], ABC):
    """Base class for predictor factories: store config, create fresh predictors."""

    @abstractmethod
    def create(self) -> PREDICTOR_T:
        """Create and return a new (unstarted) predictor instance."""


class PredictorBase(Generic[INPUT_T, OUTPUT_T], ABC):
    DEFAULT_BATCH_SIZE: int = 1
    _gpu_lock = gpu_lock  # Class-level alias for backward compat

    def __init__(self, batch_size: int | None = None) -> None:
        self._logger: logging.Logger = logging.getLogger(
            f"{self.__class__.__module__}.{self.__class__.__name__}"
        )
        self._batch_size: int = get_or_default(batch_size, self.DEFAULT_BATCH_SIZE)
        self._lock: threading.RLock = threading.RLock()

    @abstractmethod
    def _start_impl(self) -> None:
        pass

    @abstractmethod
    def _stop_impl(self) -> None:
        pass

    @abstractmethod
    def _is_ready_impl(self) -> bool:
        pass

    @abstractmethod
    def preprocess(self, input: list[INPUT_T]) -> list[Any]:
        """Preprocess a batch of inputs for inference."""

    @abstractmethod
    def _predict_impl(
        self, input: list[INPUT_T], *, preprocess: bool = True, **kwargs: Any
    ) -> list[OUTPUT_T]:
        """Internal prediction logic. Subclasses implement this instead of predict."""

    def start(self) -> None:
        with self._lock:
            with self._gpu_lock:
                self._start_impl()

    def stop(self) -> None:
        with self._lock:
            with self._gpu_lock:
                self._stop_impl()

    def is_ready(self) -> bool:
        with self._lock:
            return self._is_ready_impl()

    def predict(
        self, input: list[INPUT_T], *, preprocess: bool = True, **kwargs: Any
    ) -> list[OUTPUT_T]:
        """Predict on a batch, chunked by ``_batch_size``.

        Subclasses hold ``gpu_lock`` only around ``session.run()`` so preprocessing stays concurrent.

        Args:
            preprocess: False when input is already preprocessed.
        """
        with self._lock:
            if not self._is_ready_impl():
                raise RuntimeError(f"{self.__class__.__name__} is not ready")

        if len(input) <= self._batch_size:
            return self._predict_impl(input, preprocess=preprocess, **kwargs)
        results: list[OUTPUT_T] = []
        for i in range(0, len(input), self._batch_size):
            chunk = input[i : i + self._batch_size]
            results.extend(self._predict_impl(chunk, preprocess=preprocess, **kwargs))
        return results
