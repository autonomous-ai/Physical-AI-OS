"""Contract for handing device hardware between HAL and a vendor process."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MediaOwner(Protocol):
    """A process HAL borrows camera and microphone from."""

    def release(self) -> bool:
        """Take the hardware from the owner. Called before HAL opens anything."""
        ...

    def acquire(self) -> bool:
        """Give the hardware back, once HAL has closed its own handles."""
        ...
