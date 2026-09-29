"""The AnimationService body-ownership contract, for test doubles."""

import threading


class BodyOwnership:
    _tracking_flag: bool = False
    _body_owners: int = 0
    _body_owner_lock = threading.Lock()

    @property
    def _tracking_active(self) -> bool:
        """True while anything owns the body — a flag holder or a live writer."""
        return self._tracking_flag or self._body_owners > 0

    @_tracking_active.setter
    def _tracking_active(self, value: bool) -> None:
        # Sets the flag only, like the real service; it cannot release a running writer.
        self._tracking_flag = bool(value)

    def acquire_body(self) -> None:
        with self._body_owner_lock:
            self._body_owners += 1

    def release_body(self) -> None:
        with self._body_owner_lock:
            self._body_owners = max(0, self._body_owners - 1)
