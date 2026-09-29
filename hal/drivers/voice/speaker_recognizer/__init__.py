"""Speaker voice recognition package."""

import logging
import threading
from typing import Optional

from .speaker_recognizer import (
    EmbeddingAPIUnavailableError,
    SpeakerRecognizer,
    SpeakerRecognizerError,
)

logger = logging.getLogger("hal.voice.speaker")

# Process-wide singleton, shared by BOTH the voice pipeline (SpeakerDecorator) and the
# HTTP routes, so their in-memory state.
_shared: Optional[SpeakerRecognizer] = None
_shared_lock = threading.Lock()


def get_shared_recognizer() -> Optional[SpeakerRecognizer]:
    """Return the shared SpeakerRecognizer, building it lazily on first use."""
    global _shared
    if _shared is None:
        with _shared_lock:
            if _shared is None:
                try:
                    _shared = SpeakerRecognizer()
                except Exception as e:
                    logger.warning("Shared SpeakerRecognizer init failed: %s", e)
                    return None
    return _shared


__all__ = [
    "EmbeddingAPIUnavailableError",
    "SpeakerRecognizer",
    "SpeakerRecognizerError",
    "get_shared_recognizer",
]
