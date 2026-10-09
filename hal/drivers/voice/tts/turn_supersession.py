"""Reject late OS speech for captures superseded by an explicit device tap."""

import re
import threading

_stamp = re.compile(r"-(\d{13})$")
_lock = threading.Lock()
_before_ms = 0


def suppress_before(before_ms: int) -> None:
    """Keep the cutoff across TTS provider replacement without opening the mic."""
    global _before_ms
    with _lock:
        _before_ms = max(_before_ms, before_ms)


def owner_superseded(owner: str) -> bool:
    """Only timestamped OS run owners participate; local cues remain available."""
    if not owner or not owner.startswith("run:"):
        return False
    match = _stamp.search(owner)
    if match is None:
        return False
    with _lock:
        return int(match.group(1)) <= _before_ms
