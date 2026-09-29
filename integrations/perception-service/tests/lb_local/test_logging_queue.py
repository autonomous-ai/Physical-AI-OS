"""Queued logging: a stuck log writer must never block the caller (#530)."""

import logging
import threading
import time

import pytest

from core.logging_ext import NonBlockingQueueHandler, queued, stop_queued_logging
from core.request_context import install_request_id_logging


class _GatedHandler(logging.Handler):
    """Collects formatted records; blocks every write until `gate` is set."""

    def __init__(self, fmt: str = "%(message)s") -> None:
        super().__init__()
        self.setFormatter(logging.Formatter(fmt))
        self.gate = threading.Event()
        self.gate.set()
        self.started = threading.Event()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.started.set()
        self.gate.wait()
        self.lines.append(self.format(record))


def _logger(name: str, handler: logging.Handler) -> logging.Logger:
    log = logging.getLogger(name)
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False
    return log


@pytest.fixture(autouse=True)
def _stop_writers():
    yield
    stop_queued_logging(timeout=0.5)


def test_emit_returns_while_writer_is_stuck():
    target = _GatedHandler()
    target.gate.clear()
    log = _logger("t.stuck", queued(target))
    log.info("first")
    assert target.started.wait(2), "writer thread never picked up the record"

    t0 = time.perf_counter()
    for i in range(100):
        log.info("record %d", i)
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.1, f"100 log calls took {elapsed:.3f}s with the writer stuck"
    target.gate.set()


def test_full_queue_drops_and_reports():
    install_request_id_logging()
    target = _GatedHandler("[%(request_id)s] %(levelname)s %(message)s")
    target.gate.clear()
    qh = queued(target, maxsize=3)
    log = _logger("t.full", qh)

    log.info("r0")
    assert target.started.wait(2)       # r0 is off the queue, writer blocked on it
    for i in range(1, 4):
        log.info("r%d", i)              # fills the queue (3)
    for i in range(4, 9):
        log.info("r%d", i)              # 5 dropped, must not block

    target.gate.set()
    stop_queued_logging(timeout=2)

    assert target.lines[0] == "[-] INFO r0"
    assert "WARNING [logging] queue full: dropped 5 record(s)" in target.lines[1]
    assert [line.split()[-1] for line in target.lines[2:]] == ["r1", "r2", "r3"]
    assert qh.take_dropped() == 0


def test_stop_flushes_queued_records():
    target = _GatedHandler()
    log = _logger("t.flush", queued(target))
    for i in range(50):
        log.info("line %d", i)
    stop_queued_logging(timeout=2)
    assert target.lines == [f"line {i}" for i in range(50)]


def test_stop_does_not_hang_when_writer_is_stuck():
    target = _GatedHandler()
    target.gate.clear()
    log = _logger("t.hang", queued(target))
    log.info("stuck")
    assert target.started.wait(2)

    t0 = time.perf_counter()
    stop_queued_logging(timeout=0.2)
    assert time.perf_counter() - t0 < 1.0
    target.gate.set()


def test_exception_text_survives_the_queue():
    target = _GatedHandler()
    log = _logger("t.exc", queued(target))
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("failed")
    stop_queued_logging(timeout=2)
    assert "failed" in target.lines[0]
    assert "ValueError: boom" in target.lines[0]


def test_queue_handler_type():
    assert isinstance(queued(_GatedHandler()), NonBlockingQueueHandler)
