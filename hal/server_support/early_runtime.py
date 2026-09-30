"""Serve the bootstrap API while the complete HAL starts in one worker."""

import asyncio
import logging
import os
import threading

logger = logging.getLogger("hal.server")


def _fatal_startup():
    # Nonzero exit: incomplete startup or teardown must not look healthy to systemd.
    os._exit(1)


class EarlyRuntime:
    """ASGI dispatcher with one shared hardware owner and bounded shutdown.

    cleanup_early owns teardown until the full lifespan enters; then the full lifespan owns it.
    """

    def __init__(self, bootstrap, load_runtime, cleanup_early, *,
                 fatal=_fatal_startup, shutdown_timeout=20.0):
        self.bootstrap = bootstrap
        self.load_runtime = load_runtime
        self.cleanup_early = cleanup_early
        self.fatal = fatal
        self.shutdown_timeout = shutdown_timeout
        self._runtime = None
        self._stop = threading.Event()
        self._worker = None
        self._worker_failed = threading.Event()
        self._fatal_lock = threading.Lock()
        self._fatal_called = False

    def _fail_process(self):
        # The worker and the ASGI owner can observe the same failure concurrently.
        with self._fatal_lock:
            if self._fatal_called:
                return
            self._fatal_called = True
        self.fatal()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
        else:
            app = self._runtime or self.bootstrap
            await app(scope, receive, send)

    def _run_runtime(self):
        entered = False

        async def run():
            nonlocal entered
            runtime = self.load_runtime()
            if self._stop.is_set():
                return
            async with runtime.router.lifespan_context(runtime):
                entered = True
                if not self._stop.is_set():
                    self._runtime = runtime
                    logger.info("[startup] full HTTP API ready")
                await asyncio.to_thread(self._stop.wait)
                self._runtime = None

        failed = False
        try:
            asyncio.run(run())
        except BaseException:
            failed = True
            self._runtime = None
            logger.exception("[startup] full HAL startup or shutdown failed")
        finally:
            if not entered:
                try:
                    self.cleanup_early()
                except Exception:
                    failed = True
                    logger.exception("[startup] early hardware cleanup failed")
        if failed:
            self._worker_failed.set()
            # During shutdown the ASGI owner reports failure before terminating.
            if not self._stop.is_set():
                self._fail_process()

    async def _lifespan(self, receive, send):
        message = await receive()
        if message["type"] != "lifespan.startup":
            raise RuntimeError("Expected lifespan.startup")
        started = False
        try:
            async with self.bootstrap.router.lifespan_context(self.bootstrap):
                self._worker = threading.Thread(
                    target=self._run_runtime, name="hal-runtime-startup", daemon=True,
                )
                self._worker.start()
                started = True
                await send({"type": "lifespan.startup.complete"})
                try:
                    await receive()
                finally:
                    self._stop.set()
                    # Unit timeout is 30s; reserve 5s for HTTP drain and 5s overhead.
                    await asyncio.to_thread(self._worker.join, self.shutdown_timeout)
                    if self._worker.is_alive():
                        raise TimeoutError(
                            "HAL hardware cleanup did not finish within "
                            f"{self.shutdown_timeout:.1f}s; worker still running"
                        )
                    if self._worker_failed.is_set():
                        raise RuntimeError("HAL hardware worker failed during startup or shutdown")
            await send({"type": "lifespan.shutdown.complete"})
        except BaseException as exc:
            self._stop.set()
            if self._worker is None:
                self.cleanup_early()
            try:
                await send({
                    "type": "lifespan.shutdown.failed" if started else "lifespan.startup.failed",
                    "message": str(exc),
                })
            finally:
                # Never race the hardware owner with a second cleanup attempt.
                # Uvicorn's failure event alone does not ensure a nonzero exit.
                self._fail_process()
