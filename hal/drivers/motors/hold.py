"""Who is holding the body still, so one owner's release cannot drop another's hold.

`_hold_mode` stays the flag every backend and reader checks. It is true exactly while
at least one owner is left.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, FrozenSet, Optional, Set

logger = logging.getLogger(__name__)

SCENE = "scene"
TRACKING = "tracking"
EXPLICIT = "explicit"

# Most specific first: this is the reason the skip logs name.
_PRIORITY = (EXPLICIT, SCENE, TRACKING)

_lock = threading.Lock()


def _live_owners(svc: Any) -> Set[str]:
    """The owner set, emptied when the flag was cleared behind our back (resume)."""
    owners = getattr(svc, "_hold_owners", None)
    if owners is None:
        owners = set()
        svc._hold_owners = owners
    if not getattr(svc, "_hold_mode", False):
        owners.clear()
    return owners


def claim(svc: Any, owner: str) -> None:
    """Hold the body on behalf of `owner`."""
    if svc is None:
        return
    with _lock:
        _live_owners(svc).add(owner)
        svc._hold_mode = True
        if owner == EXPLICIT:
            svc._hold_explicit = True


def release(svc: Any, owner: str) -> bool:
    """Drop `owner`'s claim. The body is freed only when no owner is left."""
    if svc is None:
        return False
    with _lock:
        owners = _live_owners(svc)
        if owner not in owners:
            return False
        owners.discard(owner)
        if owner == EXPLICIT:
            svc._hold_explicit = False
        if not owners:
            svc._hold_mode = False
            svc._hold_explicit = False
        return True


def owners(svc: Any) -> FrozenSet[str]:
    if svc is None:
        return frozenset()
    with _lock:
        return frozenset(_live_owners(svc))


def holder(svc: Any) -> Optional[str]:
    """Who holds the body, or None. "hold" means held by a path that named no owner."""
    if svc is None:
        return None
    with _lock:
        if not getattr(svc, "_hold_mode", False):
            return None
        live = _live_owners(svc)
        return next((o for o in _PRIORITY if o in live), "hold")
