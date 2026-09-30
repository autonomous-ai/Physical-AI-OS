import logging
import threading
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from hal import cpu_affinity
from hal.drivers.sensing.perceptions.typing import SendEventCallable
from hal.drivers.sensing.perceptions.utils import PerceptionStateObservers

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Perception[T](ABC):
    """Base class for a single camera-frame perception check."""

    # Face ID is HAL's biggest idle CPU user; keep it off the cores the voice path uses.
    _pool: ThreadPoolExecutor = ThreadPoolExecutor(
        max_workers=2,
        initializer=cpu_affinity.pin_current_thread,
        initargs=(cpu_affinity.SLOW,),
    )

    def __init__(
        self,
        perception_state: PerceptionStateObservers,
        send_event: SendEventCallable,
        *args: Any,
        **kwargs: Any,
    ):
        self._perception_state: PerceptionStateObservers = perception_state
        self._send_event: SendEventCallable = send_event
        self._busy: bool = False
        self._lock: threading.RLock = threading.RLock()

    def check(self, data: T) -> None:
        """Non-blocking entry point. Skips if a previous check is still queued or running."""
        with self._lock:
            if self._busy:
                return
            self._busy = True

        try:
            _ = Perception._pool.submit(self._run, data)
        except RuntimeError:
            with self._lock:
                self._busy = False

    def _run(self, data: T) -> None:
        try:
            self._check_impl(data)
        except Exception:
            logger.exception("[%s] check error", type(self).__name__)
        finally:
            with self._lock:
                self._busy = False

    @abstractmethod
    def _check_impl(self, data: T) -> None:
        """Run detection on a single frame. Called in the shared thread pool."""

    def to_dict(self) -> dict[str, Any]:
        """Return current state for monitoring. Override in subclasses."""
        return {"type": type(self).__name__}

    @abstractmethod
    def cleanup(self) -> None:
        """Release external resources (WS connections, files, etc.). Called on shutdown."""
