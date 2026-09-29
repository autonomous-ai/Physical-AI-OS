"""Motion service contract — the interface every motion driver must satisfy."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set, runtime_checkable

from typing_extensions import Protocol


@runtime_checkable
class MotionService(Protocol):
    """The contract that routes/servo.py and the rest of HAL talk to."""

    def start(self, skip_wake: bool = False) -> None: ...
    def stop(self, timeout: float = 5.0) -> None: ...

    @property
    def is_connected(self) -> bool: ...

    def dispatch(self, event_type: str, payload: Any) -> None: ...
    def get_available_recordings(self) -> List[str]: ...
    def add_recording(self, name: str, actions: List[Dict[str, float]]) -> None: ...
    def ensure_running(self) -> None:
        """Restart the event loop if it stopped (e.g. after zero/hold)."""
        ...

    @property
    def is_suppressed(self) -> bool:
        """True when zero_pose or hold is active — idle/ambient animations suppressed."""
        ...

    @property
    def motion_mode(self) -> Optional[str]:
        """The mode holding the body ("zero"/"hold"/"released"), or None."""
        ...

    def freeze(self) -> None: ...
    def unfreeze(self) -> None: ...

    @property
    def is_frozen(self) -> bool: ...

    # The lock that says "somebody else is driving the joints, keep off": the vision
    # tracker, a look aim + capture, a search sweep.

    def acquire_body(self) -> None:
        """Claim the body. Re-entrant by count; pair with release_body()."""
        ...

    def release_body(self) -> None:
        """Give up one claim. Never drops below zero."""
        ...

    def move_to(self, target_positions: Dict[str, float], duration: float = 2.0) -> None: ...
    def move_and_hold(self, target_positions: Dict[str, float], duration: float = 2.0) -> None: ...

    def get_joint_names(self) -> Set[str]:
        """Return the set of valid joint keys, e.g. {"base_yaw.pos", "base_pitch.pos", ...}."""
        ...

    def get_positions(self) -> Dict[str, float]:
        """Read current joint positions (hardware). Keys match get_joint_names()."""
        ...

    def send_positions(self, positions: Dict[str, float]) -> None:
        """Write joint positions directly (one-shot, no interpolation)."""
        ...

    def zero_pose(self) -> None:
        """Move to the device's zero/park pose and hold (torque stays ON)."""
        ...

    def release(self) -> Dict[str, str]:
        """Move to gravity-rest, then disable torque. Returns per-motor error dict (empty=ok)."""
        ...

    def halt(self) -> None:
        """Abort any move, recording or tracking in flight and HOLD where the body is.

        Must be safe to call at any time, including when nothing is moving, and must
        never be gated by a safety bound (`motion.stop_always`).
        """
        ...

    def resume(self) -> None:
        """Exit zero/hold, re-enable torque, restart idle animation."""
        ...

    def hold(self, explicit: bool = False) -> None:
        """Suppress idle animations but keep torque ON."""
        ...

    def joint_status(self) -> Dict[str, dict]:
        """Per-joint online/offline status with angle and servo ID."""
        ...

    def aim(self, direction: str, duration: float, current_positions: Dict[str, float],
            safety_policy: Any) -> Dict[str, float]:
        """Aim to a named direction. Returns the final joint positions dict."""
        ...

    def nudge(self, yaw: float, pitch: float, duration: float,
              current_positions: Dict[str, float],
              safety_policy: Any) -> Dict[str, float]:
        """Relative nudge from current position. Returns the final joint positions dict."""
        ...
