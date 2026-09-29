"""Base for audio processors (ported from perception-service)."""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Generic, TypeVar

import numpy as np
import numpy.typing as npt
from typing_extensions import override

INPUT_T = TypeVar("INPUT_T")
OUTPUT_T = TypeVar("OUTPUT_T")

# Global GPU lock. ANY code that touches the GPU (TorchScript VAD, model
# loading/warmup) must hold this lock. Same semantics as perception-service —
# kept so the ported logic is identical.
gpu_lock: threading.RLock = threading.RLock()


@dataclass
class Audio:
    waveform: npt.NDArray[np.float32]
    """Shape: (T,) — mono float32 waveform (may be (T, C) before MonoConverter)."""

    sample_rate: int


class InputProcessorBase(Generic[INPUT_T, OUTPUT_T], ABC):
    """Base for input processors with lifecycle management."""

    def __init__(self) -> None:
        self._logger: logging.Logger = logging.getLogger(
            f"{self.__class__.__module__}.{self.__class__.__name__}"
        )
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
    def _process_impl(self, input: INPUT_T) -> OUTPUT_T:
        """Process a single input. Subclasses implement this."""

    def start(self) -> None:
        with self._lock:
            self._start_impl()

    def stop(self) -> None:
        with self._lock:
            self._stop_impl()

    def is_ready(self) -> bool:
        with self._lock:
            return self._is_ready_impl()

    def process(self, input: INPUT_T) -> OUTPUT_T:
        """Process a single input. Thread-safe via lock."""
        with self._lock:
            if not self._is_ready_impl():
                raise RuntimeError(f"{self.__class__.__name__} is not ready")

        return self._process_impl(input)


class AudioProcessorBase(InputProcessorBase[Audio, Audio], ABC):
    """Base for audio processors. Input and output are both Audio."""

    def __init__(self) -> None:
        super().__init__()
        self._running: bool = False

    @override
    def _start_impl(self) -> None:
        self._running = True
        self._logger.info("Processor started")

    @override
    def _stop_impl(self) -> None:
        self._running = False
        self._logger.info("Processor stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running
