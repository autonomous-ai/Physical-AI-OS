"""Bounded, generation-owned cloud startup independent of local capture readiness."""

import logging
import threading
import time

from hal import config

logger = logging.getLogger("hal.realtime.orchestrator")


def start(self) -> None:
    """Schedule the latest connection without blocking local input readiness."""
    provider = config.REALTIME_PROVIDER.strip().lower()
    if provider in ("none", "off", "disabled", ""):
        logger.info("Realtime orchestrator disabled (provider=%s)", provider)
        return
    with self._lifecycle_lock:
        self._startup_generation += 1
        self._startup_pending = (self._startup_generation, provider)
        if self._startup_worker is None:
            self._startup_worker = threading.Thread(
                target=_startup_loop, args=(self,), daemon=True, name="rt-startup",
            )
            self._startup_worker.start()


def _startup_loop(self) -> None:
    # One worker plus one replaceable request bounds rapid mode toggles even
    # when a provider connect cannot be interrupted. Retired connects never
    # become available and are closed before attempting the newest request.
    while True:
        with self._lifecycle_lock:
            pending = self._startup_pending
            self._startup_pending = None
            if pending is None:
                self._startup_worker = None
                return
        generation, provider = pending
        agent = None
        error = None
        try:
            instructions = self._context.build_instructions()
            agent = self._make_agent(provider, instructions)
            if agent is not None:
                agent.connect()
        except Exception as exc:
            error = exc
        with self._lifecycle_lock:
            current = generation == self._startup_generation
            if current and agent is not None:
                self._agent = agent
                self._initial_connect_exc = error
                self._connect_retry_stop = threading.Event()
                self._connect_retry_thread = None
                self._started.set()
                self._last_activity_monotonic = time.monotonic()
                if error is None:
                    self._session_connected_monotonic = self._last_activity_monotonic
                    logger.info("[realtime] Realtime orchestrator started (provider=%s)", provider)
                else:
                    logger.warning("[realtime] Failed to connect realtime agent (%s: %s) — retrying in background",
                                   type(error).__name__, error)
                if not self.available:
                    self._start_connect_retry_loop()
                self._start_idle_park_loop()
                threading.Thread(target=self._catch_up_memory_summaries, daemon=True,
                                 name="realtime-catchup-summarize").start()
        if not current and agent is not None:
            try:
                agent.disconnect()
            except Exception:
                logger.exception("[realtime] Failed to close retired startup session")
        elif agent is None:
            logger.warning(
                "[realtime] Startup did not create an agent (provider=%s)", provider,
                exc_info=(type(error), error, error.__traceback__) if error is not None else None,
            )

