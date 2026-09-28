"""Pending Harness updates and the policy for what one spoken snapshot holds.

os-server posts every Harness result, question and progress line here instead
of speaking it. The announcer takes a snapshot whenever the device is free:

- A result or question is always spoken. It supersedes queued progress of the
  same run, and progress of other runs is not worth speaking next to it.
- A snapshot of only progress is spoken with probability HARNESS_PROGRESS_SPEAK_P,
  at most once per run per HARNESS_PROGRESS_MIN_GAP_S, never within
  HARNESS_PROGRESS_QUIET_START_S of the request, and only its newest line.
- Anything left unspoken past its max age is dropped; Web Chat and the Harness
  app keep the full text.

Pure state + policy (injectable clock and random source) so tests need no audio.
"""

import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from hal import config as hal_config

KINDS = ("result", "question", "progress")
# Trailing unix-ms creation stamp of a device run id ("device-chat-7-1788422075499").
_RUN_ID_STAMP = re.compile(r"-(\d{13})$")


@dataclass(frozen=True)
class HarnessUpdate:
    kind: str
    text: str
    run_id: str = ""
    outcome: str = ""
    received_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class Snapshot:
    items: tuple[HarnessUpdate, ...]

    @property
    def progress_only(self) -> bool:
        return all(item.kind == "progress" for item in self.items)

    @property
    def owner(self) -> str:
        """Run that owns the speech: the newest update's run."""
        return self.items[-1].run_id if self.items else ""


class HarnessUpdateQueue:
    def __init__(
        self,
        *,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._rng = rng
        self._clock = clock
        self._wall_clock = wall_clock
        self._items: list[HarnessUpdate] = []
        self._cond = threading.Condition()
        # run_id -> clock() when that run was first seen (runs without a stamp).
        self._first_seen: dict[str, float] = {}
        # run_id -> clock() of its last spoken progress line.
        self._last_progress: dict[str, float] = {}

    def put(self, update: HarnessUpdate) -> None:
        if update.kind not in KINDS or not update.text.strip():
            return
        with self._cond:
            self._first_seen.setdefault(update.run_id, update.received_at)
            self._items.append(update)
            self._cond.notify_all()

    def requeue(self, snapshot: Snapshot) -> None:
        """Return an unspoken snapshot's results/questions for the next one.

        Progress is not returned: by the time the user has finished talking it
        is stale, and it already spent its per-run budget.
        """
        with self._cond:
            keep = [item for item in snapshot.items if item.kind != "progress"]
            self._items[:0] = keep
            if keep:
                self._cond.notify_all()

    def pending(self) -> bool:
        with self._cond:
            return bool(self._items)

    def wait(self, timeout: float) -> bool:
        """Block until an update is queued (or timeout); True when one is."""
        with self._cond:
            if not self._items:
                self._cond.wait(timeout=timeout)
            return bool(self._items)

    def run_age_s(self, run_id: str) -> float:
        """Seconds since the request behind a run, from its id stamp when it has one."""
        match = _RUN_ID_STAMP.search(run_id or "")
        if match:
            return max(0.0, self._wall_clock() - int(match.group(1)) / 1000.0)
        first = self._first_seen.get(run_id)
        return self._clock() - first if first is not None else 0.0

    def _prune_locked(self, now: float) -> None:
        keep: list[HarnessUpdate] = []
        for item in self._items:
            limit = (
                hal_config.HARNESS_PROGRESS_MAX_AGE_S
                if item.kind == "progress"
                else hal_config.HARNESS_UPDATE_MAX_AGE_S
            )
            if now - item.received_at <= limit:
                keep.append(item)
        self._items = keep
        horizon = max(hal_config.HARNESS_UPDATE_MAX_AGE_S, hal_config.HARNESS_PROGRESS_MIN_GAP_S)
        live_runs = {item.run_id for item in keep}
        for table in (self._first_seen, self._last_progress):
            for run_id, seen in list(table.items()):
                if run_id not in live_runs and now - seen > horizon:
                    del table[run_id]

    def take_snapshot(self, *, progress_renderable: bool) -> Snapshot | None:
        """Drain the queue into one snapshot to speak, or None if nothing qualifies.

        progress_renderable says whether a progress line could be spoken right
        now without reconnecting anything; when False queued progress is
        dropped without consuming its per-run budget.
        """
        with self._cond:
            now = self._clock()
            self._prune_locked(now)
            items, self._items = self._items, []
            if not items:
                return None
            important = [item for item in items if item.kind != "progress"]
            if important:
                # Questions first: they need an answer, results only a hearing.
                important.sort(key=lambda item: (item.kind != "question", item.received_at))
                return Snapshot(items=tuple(important))
            if not progress_renderable:
                return None
            newest: dict[str, HarnessUpdate] = {}
            for item in items:
                newest[item.run_id] = item
            eligible = [
                item for item in newest.values()
                if self.run_age_s(item.run_id) >= hal_config.HARNESS_PROGRESS_QUIET_START_S
                and now - self._last_progress.get(item.run_id, float("-inf"))
                >= hal_config.HARNESS_PROGRESS_MIN_GAP_S
            ]
            if not eligible:
                return None
            if self._rng() >= hal_config.HARNESS_PROGRESS_SPEAK_P:
                return None
            chosen = max(eligible, key=lambda item: item.received_at)
            self._last_progress[chosen.run_id] = now
            return Snapshot(items=(chosen,))
