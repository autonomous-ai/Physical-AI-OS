import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Optional
from enum import IntEnum
import logging


class Priority(IntEnum):
    LOW = 3
    NORMAL = 2
    HIGH = 1
    CRITICAL = 0


class ServiceEvent:
    def __init__(self, event_type: str, payload: Any, priority: Priority = Priority.NORMAL):
        self.event_type = event_type
        self.payload = payload
        self.priority = priority

    def __lt__(self, other):
        return self.priority < other.priority


class ServiceBase(ABC):
    def __init__(self, name: str):
        self.name = name
        self._current_event: Optional[ServiceEvent] = None
        self._event_lock = threading.Lock()
        self._event_available = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._stop_event = threading.Event()
        self.logger = logging.getLogger(f"service.{name}")

    def dispatch(self, event_type: str, payload: Any, priority: Priority = Priority.NORMAL):
        if not self._running.is_set():
            self.logger.warning(f"Service {self.name} is not running, ignoring event {event_type}")
            return

        event = ServiceEvent(event_type, payload, priority)

        with self._event_lock:
            if self._current_event is None or event.priority <= self._current_event.priority:
                self._current_event = event
                self._event_available.set()

        self.logger.debug(f"Dispatched event {event_type} with priority {priority.name}")

    def start(self):
        if self._running.is_set():
            self.logger.warning(f"Service {self.name} is already running")
            return

        self._running.set()
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._worker_thread.start()
        self.logger.info(f"Service {self.name} started")

    def stop(self, timeout: float = 5.0):
        if not self._running.is_set():
            self.logger.warning(f"Service {self.name} is not running")
            return

        self.logger.info(f"Stopping service {self.name}")
        self._stop_event.set()
        self._running.clear()

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
            if self._worker_thread.is_alive():
                self.logger.warning(f"Service {self.name} did not stop within timeout")
            else:
                self.logger.info(f"Service {self.name} stopped")

    def _event_loop(self):
        while self._running.is_set():
            if self._event_available.wait(timeout=0.1):
                with self._event_lock:
                    if self._current_event:
                        event = self._current_event
                    else:
                        continue

                try:
                    self.handle_event(event.event_type, event.payload)
                except Exception as e:
                    self.logger.error(f"Error handling event {event.event_type}: {e}")
                finally:
                    with self._event_lock:
                        if self._current_event is event:
                            self._current_event = None
                            self._event_available.clear()

            if self._stop_event.is_set():
                break

    @abstractmethod
    def handle_event(self, event_type: str, payload: Any):
        pass

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    @property
    def has_pending_event(self) -> bool:
        with self._event_lock:
            return self._current_event is not None

    def wait_until_idle(self, timeout: Optional[float] = None) -> bool:
        """Wait until no pending events. Returns True if idle, False if timeout."""
        start_time = None
        if timeout is not None:
            start_time = time.time()

        while self.has_pending_event:
            if timeout is not None and time.time() - start_time > timeout:
                return False
            time.sleep(0.01)

        return True
