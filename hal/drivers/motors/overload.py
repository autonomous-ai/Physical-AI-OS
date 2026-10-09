"""Servo overload cut-off: when a stalled joint must lose torque, and when to try again."""

import time
from typing import Callable, Dict, Optional, Tuple, Union

# STS3215 Present_Load (reg 60): drive duty in 0.1 % units, bit 10 is the direction.
_LOAD_MAGNITUDE_MASK = 0x3FF


def load_magnitude(raw: int) -> int:
    """Present_Load register value -> 0..1000 (0.1 % of full drive), direction dropped."""
    return int(raw) & _LOAD_MAGNITUDE_MASK


class OverloadGuard:
    """Times sustained high load per joint, and the lockout that follows a cut-off.

    Stateful across calls but otherwise pure, like TrackingWedgeWatchdog: it never
    touches the bus, so the caller keeps the torque writes and the log lines.
    """

    def __init__(
        self,
        threshold: Union[int, Dict[str, int]],
        hold_s: float,
        retry_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # One floor for every joint, or a per-joint map; a joint missing from the map
        # is not watched.
        self.threshold = threshold
        self.hold_s = hold_s
        self.retry_s = retry_s
        self._clock = clock
        self._over_since: Dict[str, float] = {}
        self._locked_until: Optional[float] = None
        self.trips = 0
        self.last_trip: Optional[Tuple[str, int]] = None
        # The floor the last trip crossed (a per-sample floor when one was passed).
        self.last_floor: Optional[int] = None
        self.load: Dict[str, int] = {}
        self.peak: Dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        if isinstance(self.threshold, dict):
            return any(value > 0 for value in self.threshold.values())
        return self.threshold > 0

    def threshold_for(self, joint: str) -> Optional[int]:
        """This joint's load floor, or None when it is not watched."""
        if isinstance(self.threshold, dict):
            return self.threshold.get(joint)
        return self.threshold

    @property
    def locked(self) -> bool:
        """True from a cut-off until retry_due() reports the lockout over."""
        return self._locked_until is not None

    def retry_in_s(self) -> float:
        if self._locked_until is None:
            return 0.0
        return max(0.0, self._locked_until - self._clock())

    def observe(
        self,
        loads: Optional[Dict[str, int]],
        floors: Optional[Dict[str, int]] = None,
    ) -> Optional[Tuple[str, int]]:
        """Feed one per-joint load sample. Returns (joint, load) when the cut-off fires.

        None or empty means the read failed: the timing restarts, so two high samples
        either side of a gap never count as one sustained overload. `floors` overrides
        the configured floor for the joints it names, for this sample only (the contact
        stop's learned envelope).
        """
        if not self.enabled or self.locked:
            return None
        if not loads:
            self._over_since.clear()
            return None
        now = self._clock()
        # Rebound, never mutated: /health copies these from another thread.
        self.load = dict(loads)
        self.peak = {joint: max(load, self.peak.get(joint, 0)) for joint, load in loads.items()}
        worst: Optional[Tuple[str, int]] = None
        worst_floor: Optional[int] = None
        for joint, load in loads.items():
            floor = floors[joint] if floors and joint in floors else self.threshold_for(joint)
            if floor is None or load < floor:
                self._over_since.pop(joint, None)
                continue
            since = self._over_since.setdefault(joint, now)
            if now - since >= self.hold_s and (worst is None or load > worst[1]):
                worst = (joint, load)
                worst_floor = floor
        if worst is None:
            return None
        self._over_since.clear()
        self._locked_until = now + self.retry_s
        self.trips += 1
        self.last_trip = worst
        self.last_floor = worst_floor
        return worst

    def retry_due(self) -> bool:
        """True exactly once, when the lockout has run out; the guard is armed again."""
        if self._locked_until is None or self._clock() < self._locked_until:
            return False
        self._locked_until = None
        return True
