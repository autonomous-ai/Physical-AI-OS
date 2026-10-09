"""Abstract STT (Speech-to-Text) provider interface."""

import logging
from abc import ABC, abstractmethod
from typing import Callable, List, Optional

logger = logging.getLogger("hal.voice.stt")


class STTSession(ABC):
    """A single streaming STT session (connect → send audio → get transcripts → close)."""

    @abstractmethod
    def start(self, on_transcript: Callable[[str, bool], None]) -> bool:
        """Open connection and start listening for transcripts.

        Args:
            on_transcript: callback(text, is_final) called from provider thread

        Returns:
            True if session started successfully.
        """

    @abstractmethod
    def send_audio(self, data: bytes):
        """Send raw audio chunk to STT provider. May raise on dead connection."""

    @abstractmethod
    def close(self):
        """Close connection and clean up resources. Safe to call multiple times."""

    @abstractmethod
    def is_closed(self) -> bool:
        """Whether the session has been closed (by us or by the provider)."""


class STTProvider(ABC):
    """Factory that creates STT sessions. One provider instance per VoiceService."""

    @abstractmethod
    def create_session(self, language: Optional[str] = None) -> STTSession:
        """Create a new streaming session.

        Args:
            language: app language code (e.g. "ja") for this session only; None
                uses the provider's configured language.
        """

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether this provider is properly configured and ready."""

    @property
    def name(self) -> str:
        return self.__class__.__name__
