"""Typed boundary for learned motion policies (no LeRobot or motor-driver imports)."""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class PolicyRun:
    """A requested learned-policy run, not an instruction to move hardware."""

    id: str
    policy: str
    task: str
    state: str
    dry_run: bool


class PolicyService(Protocol):
    """Boundary future local and remote policy executors must satisfy."""

    def run(self, policy: str, task: str) -> PolicyRun:
        """Accept a policy request and return its run record."""

    def stop(self) -> Optional[PolicyRun]:
        """Stop the active run, if any, without issuing a new target."""

    def active_run(self) -> Optional[PolicyRun]:
        """Return the active run, if any."""


class LoggingPolicyService:
    """Dry-run policy service that records intent and never actuates."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._lock = threading.RLock()
        self._active: Optional[PolicyRun] = None

    def run(self, policy: str, task: str) -> PolicyRun:
        with self._lock:
            if self._active is not None:
                raise RuntimeError("a policy run is already active")
            run = PolicyRun(
                id=str(uuid.uuid4()),
                policy=policy,
                task=task,
                state="dry_run",
                dry_run=True,
            )
            self._active = run
        self._logger.info(
            "[policy] dry-run accepted id=%s policy=%s task=%r; no inference or motor command issued",
            run.id,
            run.policy,
            run.task,
        )
        return run

    def stop(self) -> Optional[PolicyRun]:
        with self._lock:
            run = self._active
            self._active = None
        if run is not None:
            self._logger.info("[policy] dry-run stopped id=%s; no motor command issued", run.id)
        return run

    def active_run(self) -> Optional[PolicyRun]:
        with self._lock:
            return self._active
