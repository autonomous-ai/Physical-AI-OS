"""Backstop for a tracking lock left held with no owner."""

import time
from typing import Callable, Optional

WEDGE_GRACE_S: float = 30.0


class TrackingWedgeWatchdog:
    """Decides when a held-but-unowned tracking flag has to be cleared.

    Stateful across calls (it times the condition) but otherwise pure: it never touches
    the animation service, so the caller keeps the decision to act and the log line that
    goes with it.
    """

    def __init__(
        self,
        grace_s: float = WEDGE_GRACE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._grace_s = grace_s
        self._clock = clock
        self._idle_since: Optional[float] = None

    def check(self, flag: bool, owners: int) -> Optional[float]:
        """Return how long the wedge has been held, or None if there is none."""
        if not flag or owners > 0:
            self._idle_since = None
            return None

        now = self._clock()
        if self._idle_since is None:
            self._idle_since = now
            return None

        held = now - self._idle_since
        if held < self._grace_s:
            return None

        self._idle_since = None
        return held
