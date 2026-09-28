"""Harness update queue: what one spoken snapshot holds (drivers/harness/update_queue.py)."""

import pytest

from hal import config
from hal.drivers.harness.update_queue import HarnessUpdate, HarnessUpdateQueue


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(config, "HARNESS_PROGRESS_SPEAK_P", 0.15)
    monkeypatch.setattr(config, "HARNESS_PROGRESS_MIN_GAP_S", 60.0)
    monkeypatch.setattr(config, "HARNESS_PROGRESS_QUIET_START_S", 15.0)
    monkeypatch.setattr(config, "HARNESS_PROGRESS_MAX_AGE_S", 30.0)
    monkeypatch.setattr(config, "HARNESS_UPDATE_MAX_AGE_S", 600.0)


def make_queue(roll: float = 0.0, now: float = 1000.0):
    clock = Clock(now)
    rolls = [roll]
    queue = HarnessUpdateQueue(rng=lambda: rolls[0], clock=clock, wall_clock=lambda: 0.0)
    return queue, clock, rolls


def update(queue, clock, kind, text="text", run="run-a", outcome=""):
    queue.put(HarnessUpdate(kind=kind, text=text, run_id=run, outcome=outcome, received_at=clock.now))


def test_result_supersedes_all_progress_and_questions_come_first():
    queue, clock, _ = make_queue()
    update(queue, clock, "progress", run="a")
    update(queue, clock, "result", "done", run="a")
    update(queue, clock, "progress", run="b")
    clock.now += 1
    update(queue, clock, "question", "which?", run="b")
    snapshot = queue.take_snapshot(progress_renderable=True)
    assert [item.kind for item in snapshot.items] == ["question", "result"]
    assert not snapshot.progress_only
    assert not queue.pending()


def test_empty_or_unknown_updates_are_ignored():
    queue, clock, _ = make_queue()
    update(queue, clock, "result", "   ")
    update(queue, clock, "chatter", "x")
    assert queue.take_snapshot(progress_renderable=True) is None


def test_progress_is_spoken_only_when_the_roll_wins():
    queue, clock, rolls = make_queue(roll=0.9)
    clock.now += 20  # past the quiet start of the run first seen at 1000
    update(queue, clock, "progress", run="a")
    queue._first_seen["a"] = 1000.0
    assert queue.take_snapshot(progress_renderable=True) is None
    assert not queue.pending()  # a lost roll drops the batch
    rolls[0] = 0.1
    update(queue, clock, "progress", "first", run="a")
    update(queue, clock, "progress", "newest", run="a")
    snapshot = queue.take_snapshot(progress_renderable=True)
    assert snapshot.progress_only and [item.text for item in snapshot.items] == ["newest"]


def test_progress_respects_quiet_start_and_per_run_gap():
    queue, clock, _ = make_queue(roll=0.0)
    update(queue, clock, "progress", run="a")
    assert queue.take_snapshot(progress_renderable=True) is None  # 0 s after the request
    clock.now += 20
    update(queue, clock, "progress", run="a")
    assert queue.take_snapshot(progress_renderable=True) is not None
    clock.now += 30
    update(queue, clock, "progress", run="a")
    assert queue.take_snapshot(progress_renderable=True) is None  # within the 60 s gap
    clock.now += 31
    update(queue, clock, "progress", run="a")
    assert queue.take_snapshot(progress_renderable=True) is not None


def test_quiet_start_reads_the_run_id_creation_stamp():
    clock = Clock()
    queue = HarnessUpdateQueue(rng=lambda: 0.0, clock=clock, wall_clock=lambda: 1_788_422_100.0)
    run = "device-chat-7-1788422075499"  # created ~24.5 s before wall_clock
    queue.put(HarnessUpdate(kind="progress", text="working", run_id=run, received_at=clock.now))
    assert queue.take_snapshot(progress_renderable=True) is not None


def test_unrenderable_progress_is_dropped_without_spending_the_run_budget():
    queue, clock, _ = make_queue(roll=0.0)
    clock.now += 20
    queue._first_seen["a"] = 1000.0
    update(queue, clock, "progress", run="a")
    assert queue.take_snapshot(progress_renderable=False) is None
    update(queue, clock, "progress", run="a")
    assert queue.take_snapshot(progress_renderable=True) is not None


def test_stale_updates_expire():
    queue, clock, _ = make_queue(roll=0.0)
    update(queue, clock, "result", "old")
    queue._first_seen["a"] = 0.0
    update(queue, clock, "progress", "old progress")
    clock.now += 601
    assert queue.take_snapshot(progress_renderable=True) is None


def test_snapshot_owner_is_the_newest_update():
    queue, clock, _ = make_queue()
    update(queue, clock, "result", run="a")
    clock.now += 1
    update(queue, clock, "result", run="b")
    assert queue.take_snapshot(progress_renderable=True).owner == "b"


def test_requeue_returns_results_ahead_of_newer_updates_but_not_progress():
    queue, clock, _ = make_queue()
    update(queue, clock, "result", "first", run="a")
    snapshot = queue.take_snapshot(progress_renderable=True)
    update(queue, clock, "result", "second", run="b")
    queue.requeue(snapshot)
    again = queue.take_snapshot(progress_renderable=True)
    assert [item.text for item in again.items] == ["first", "second"]
    queue.requeue(type(snapshot)(items=(HarnessUpdate(kind="progress", text="p", received_at=clock.now),)))
    assert not queue.pending()
