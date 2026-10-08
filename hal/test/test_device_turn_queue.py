"""Capacity and ownership contracts for overlapping device capture/finalization."""

import threading

from hal.drivers.voice._internal.device_turn_queue import DeviceTurnQueue


def test_delayed_finalizer_allows_next_capture_but_reserves_its_capacity():
    queue = DeviceTurnQueue()
    entered = threading.Event()
    finish = threading.Event()
    cleaned = []
    first = queue.reserve({"generation": 1})

    def finalize(cancelled):
        entered.set()
        assert finish.wait(2)

    try:
        assert queue.submit(first, finalize, lambda: cleaned.append("first"))
        assert entered.wait(1)
        second = queue.reserve({"generation": 1})
        assert second is not None
        assert queue.reserve({"generation": 1}) is None
        # Queue-full never evicts an already accepted recording.
        assert not first.cancelled.is_set()
        assert not second.cancelled.is_set()
        finish.set()
        assert first.done.wait(1)
        assert cleaned == ["first"]
        assert queue.pending == 1
        queue.release(second)
        assert second.done.is_set()
        assert queue.pending == 0
    finally:
        finish.set()
        assert queue.shutdown(2)


def test_fifo_is_reservation_order_even_when_second_stt_connects_first():
    queue = DeviceTurnQueue()
    order = []
    first = queue.reserve({})
    second = queue.reserve({})
    try:
        assert queue.submit(second, lambda _: order.append(2), lambda: order.append("c2"))
        assert queue.submit(first, lambda _: order.append(1), lambda: order.append("c1"))
        assert second.done.wait(1)
        assert order == [1, "c1", 2, "c2"]
    finally:
        assert queue.shutdown(2)


def test_privacy_cancel_cancels_running_and_queued_and_cleans_both():
    queue = DeviceTurnQueue()
    entered = threading.Event()
    finish = threading.Event()
    dispatched = []
    cleaned = []
    first = queue.reserve({})
    second = queue.reserve({})

    def finalize(cancelled):
        entered.set()
        assert finish.wait(2)
        if not cancelled.is_set():
            dispatched.append(1)

    try:
        assert queue.submit(first, finalize, lambda: cleaned.append(1))
        assert entered.wait(1)
        assert queue.submit(second, lambda _: dispatched.append(2), lambda: cleaned.append(2))
        queue.cancel_all()
        assert first.cancelled.is_set() and second.cancelled.is_set()
        # A still-running finalizer retains capacity until its resources close.
        assert queue.reserve({}) is None
        finish.set()
        assert second.done.wait(1)
        assert dispatched == []
        assert cleaned == [1, 2]
    finally:
        finish.set()
        assert queue.shutdown(2)


def test_route_change_cancels_recording_but_preserves_matching_generation():
    queue = DeviceTurnQueue()
    old_cancelled = threading.Event()
    old = queue.reserve({"generation": 1}, cancelled=old_cancelled)
    current = queue.reserve({"generation": 2})
    queue.cancel_invalid(lambda snapshot: snapshot["generation"] == 2)
    assert old_cancelled.is_set() and old.done.is_set()
    assert not current.cancelled.is_set()
    assert queue.pending == 1
    # No ownership transfer: caller must close its own cancelled recording.
    assert not queue.submit(old, lambda _: None, lambda: None)
    assert queue.shutdown(1)
    assert current.cancelled.is_set() and current.done.is_set()


def test_failed_finalizer_cleanup_and_next_turn_use_same_bounded_worker():
    queue = DeviceTurnQueue()
    workers = []
    cleaned = []

    def fail(cancelled):
        workers.append(threading.current_thread())
        raise ValueError("test finalizer failure")

    try:
        for index in range(5):
            ticket = queue.reserve({})
            assert queue.submit(ticket, fail, lambda: cleaned.append(True))
            assert ticket.done.wait(1)
        assert len(set(workers)) == 1
        assert len(cleaned) == 5
        assert queue.pending == 0
    finally:
        assert queue.shutdown(2)
    assert not workers[0].is_alive()
    assert queue.reserve({}) is None


def test_shutdown_reports_running_work_without_creating_replacement_worker():
    queue = DeviceTurnQueue()
    entered = threading.Event()
    finish = threading.Event()
    ticket = queue.reserve({})

    def finalize(cancelled):
        entered.set()
        assert finish.wait(2)

    try:
        assert queue.submit(ticket, finalize, lambda: None)
        assert entered.wait(1)
        assert not queue.shutdown(0)
        assert ticket.cancelled.is_set()
        assert queue.reserve({}) is None
        finish.set()
        assert queue.shutdown(1)
        assert ticket.done.is_set()
    finally:
        finish.set()
        assert queue.shutdown(2)


def test_abandoned_first_reservation_unblocks_second_without_duplicate_cleanup():
    queue = DeviceTurnQueue()
    first = queue.reserve({})
    second = queue.reserve({})
    cleaned = []
    try:
        assert queue.submit(second, lambda _: None, lambda: cleaned.append(2))
        assert not queue.submit(second, lambda _: None, lambda: cleaned.append("duplicate"))
        queue.release(first)
        assert second.done.wait(1)
        queue.release(second)
        assert cleaned == [2]
    finally:
        assert queue.shutdown(1)


def test_cleanup_failure_still_releases_slot_and_rejected_submit_retains_ownership():
    queue = DeviceTurnQueue(capacity=1)
    calls = []

    def cleanup():
        calls.append("cleanup")
        raise RuntimeError("test cleanup failure")

    try:
        ticket = queue.reserve({})
        assert queue.submit(ticket, lambda _: None, cleanup)
        assert ticket.done.wait(1)
        next_ticket = queue.reserve({})
        assert next_ticket is not None
        queue.release(next_ticket)
        assert not queue.submit(next_ticket, lambda _: None, cleanup)
        assert calls == ["cleanup"]
        assert queue.pending == 0
    finally:
        assert queue.shutdown(1)
