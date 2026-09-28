"""Serve the bootstrap API while the complete HAL starts in one worker."""

import asyncio
import logging
import os
import threading

logger = logging.getLogger("hal.server")


def _fatal_startup():
    # Uvicorn has already acknowledged startup. A nonzero process exit is needed
    # so systemd cannot mistake a permanently incomplete HAL for a healthy one.
    os._exit(1)


class EarlyRuntime:
    """ASGI dispatcher with one shared hardware owner and bounded shutdown.

    The bootstrap lifespan initializes early hardware but must not tear it down.
    Before the full lifespan enters, cleanup_early owns teardown; afterwards the
    full lifespan owns it. The loader and full lifespan run on the same worker,
    keeping blocking imports and hardware initialization off the HTTP loop.
    """

    def __init__(self, bootstrap, load_runtime, cleanup_early, *,
                 fatal=_fatal_startup, shutdown_timeout=5.0):
        self.bootstrap = bootstrap
        self.load_runtime = load_runtime
        self.cleanup_early = cleanup_early
        self.fatal = fatal
        self.shutdown_timeout = shutdown_timeout
        self._runtime = None
        self._stop = threading.Event()
        self._worker = None

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
            self.fatal()

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
                    await asyncio.to_thread(self._worker.join, self.shutdown_timeout)
                    if self._worker.is_alive():
                        logger.warning("[shutdown] HAL startup worker did not stop within %.1fs",
                                       self.shutdown_timeout)
            await send({"type": "lifespan.shutdown.complete"})
        except BaseException as exc:
            self._stop.set()
            if self._worker is None:
                self.cleanup_early()
            await send({
                "type": "lifespan.shutdown.failed" if started else "lifespan.startup.failed",
                "message": str(exc),
            })
