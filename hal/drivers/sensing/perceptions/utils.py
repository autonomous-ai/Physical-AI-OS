from __future__ import annotations

import logging
import re
import threading
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, TypeVar

if TYPE_CHECKING:
    import cv2

import hal.config as config
from hal.drivers.sensing.perceptions.models import FaceDetectionData

T = TypeVar("T")

_logger = logging.getLogger(__name__)
_dl_stall_lock = threading.Lock()


def record_dl_stall(task: str, detail: str) -> None:
    """Append one line to the perception-service stall log so recurring WS recv timeouts
    can be audited over time.
    """
    line = f"{datetime.now(timezone.utc).isoformat()}\t{task}\t{detail}\n"
    try:
        with _dl_stall_lock:
            with open(config.DL_STALL_LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError as e:
        _logger.warning(
            "could not write DL stall log (%s): %s", config.DL_STALL_LOG_FILE, e
        )


class DataObserver[T]:
    def __init__(self):
        self._lock: threading.RLock = threading.RLock()
        self._data: T | None = None
        self._subscriptors: set[Callable[[T], None]] = set()

    def _on_update(self):
        data = self.data

        if data is not None:
            for s in self._subscriptors:
                s(data)

    def register(self, subscriptor: Callable[[T], None]):
        with self._lock:
            self._subscriptors.add(subscriptor)

    def unregister(self, subscriptor: Callable[[T], None]):
        with self._lock:
            self._subscriptors.discard(subscriptor)

    @property
    def data(self):
        with self._lock:
            return copy(self._data)

    @data.setter
    def data(self, data: T):
        with self._lock:
            self._data = data

        self._on_update()


@dataclass
class PerceptionStateObservers:
    frame: DataObserver[cv2.typing.MatLike] = field(default_factory=DataObserver)
    detected_faces: DataObserver[FaceDetectionData] = field(
        default_factory=DataObserver
    )
    current_user: DataObserver[str] = field(default_factory=DataObserver)


def normalize_label(label: str) -> str:
    """Lowercase folder-safe label (a-z0-9_-)."""
    s = label.strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "_", s)
    s = s.strip("_")
    return s[:64] if s else "person"
