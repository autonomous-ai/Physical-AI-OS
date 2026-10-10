"""Shared ownership of a motion service's temporary camera freeze."""

from contextlib import contextmanager
import threading
import weakref


class _FreezeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.owners = 0


_registry_lock = threading.Lock()
_states = weakref.WeakKeyDictionary()


@contextmanager
def freeze_lease(service):
    """Keep motion frozen until the last overlapping camera consumer exits.

    Only the ownership transitions hold a lock; capture, settle and inference
    run concurrently. Weak keys retain no service after its normal lifetime.
    Every production freeze caller must use this helper so releases cannot
    clear another caller's hold. Driver freeze/unfreeze APIs stay unchanged.
    """
    if service is None:
        yield
        return
    with _registry_lock:
        state = _states.get(service)
        if state is None:
            state = _FreezeState()
            _states[service] = state
    with state.lock:
        if state.owners == 0:
            # A failed freeze never acquires ownership or schedules unfreeze.
            service.freeze()
        state.owners += 1
    try:
        yield
    finally:
        with state.lock:
            state.owners -= 1
            if state.owners == 0:
                service.unfreeze()
