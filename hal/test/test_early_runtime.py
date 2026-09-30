"""Early LED remains usable while full HAL imports and initializes."""

import asyncio
from contextlib import asynccontextmanager
import threading
import unittest

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx

from hal.server_support.early_runtime import EarlyRuntime


class EarlyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.release = threading.Event()
        self.loading = threading.Event()
        self.ready = threading.Event()
        self.failed = threading.Event()
        self.cleaned = threading.Event()
        self.full_stopped = threading.Event()
        self.cleanup_started = threading.Event()
        self.fatal_calls = 0
        self.early_cleanup_threads = []
        self.shared = {"color": "black"}
        self.threads = []
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.lifecycle = None

    async def asyncTearDown(self):
        self.release.set()
        if self.lifecycle is not None and not self.lifecycle.done():
            await self.incoming.put({"type": "lifespan.shutdown"})
            await asyncio.wait_for(self.lifecycle, 3)
        if hasattr(self, "dispatcher") and self.dispatcher._worker is not None:
            await asyncio.to_thread(self.dispatcher._worker.join, 3)
            self.assertFalse(self.dispatcher._worker.is_alive())

    def bootstrap(self):
        app = FastAPI()

        @app.post("/led/status")
        def led():
            self.shared["color"] = "white"
            return {"status": "ok"}

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        def pending(path: str):
            return JSONResponse({"detail": "HAL is starting"}, status_code=503)

        return app

    def full(self, *, slow=False, fail=False, slow_shutdown=False, fail_shutdown=False):
        @asynccontextmanager
        async def lifespan(app):
            self.threads.append(threading.get_ident())
            self.loading.set()
            if slow:
                self.release.wait(3)
            if fail:
                raise RuntimeError("required hardware failed")
            self.ready.set()
            try:
                yield
            finally:
                self.cleanup_started.set()
                if slow_shutdown:
                    self.release.wait(10)
                if fail_shutdown:
                    raise RuntimeError("hardware cleanup failed")
                self.threads.append(threading.get_ident())
                self.full_stopped.set()

        app = FastAPI(lifespan=lifespan)

        @app.get("/servo")
        def servo():
            return dict(self.shared)

        return app

    async def start(self, loader, **kwargs):
        def fatal():
            self.fatal_calls += 1
            self.failed.set()

        def cleanup():
            self.early_cleanup_threads.append(threading.get_ident())
            self.cleaned.set()

        cleanup = kwargs.pop("cleanup_early", cleanup)
        self.dispatcher = EarlyRuntime(
            self.bootstrap(), loader, cleanup, fatal=fatal, **kwargs,
        )
        self.lifecycle = asyncio.create_task(self.dispatcher(
            {"type": "lifespan"}, self.incoming.get, self.outgoing.put,
        ))
        await self.incoming.put({"type": "lifespan.startup"})
        result = await asyncio.wait_for(self.outgoing.get(), 2)
        self.assertEqual(result["type"], "lifespan.startup.complete")

    async def event(self, event):
        self.assertTrue(await asyncio.to_thread(event.wait, 2))

    async def request(self, path, method="GET"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.dispatcher), base_url="http://127.0.0.1",
        ) as client:
            return await asyncio.wait_for(client.request(method, path), 1)

    async def stop(self, *, failed=False):
        await self.incoming.put({"type": "lifespan.shutdown"})
        result = await asyncio.wait_for(self.outgoing.get(), 2)
        self.assertEqual(result["type"],
                         "lifespan.shutdown.failed" if failed else "lifespan.shutdown.complete")
        await self.lifecycle
        if failed:
            self.assertTrue(self.failed.is_set())
            self.assertEqual(self.fatal_calls, 1)
        self.assertTrue(self.outgoing.empty())

    async def test_slow_import_serves_led_then_preserves_state_on_handoff(self):
        runtime = self.full()

        def load():
            self.loading.set()
            self.release.wait(3)
            return runtime

        await self.start(load)
        await self.event(self.loading)
        self.assertEqual((await self.request("/led/status", "POST")).status_code, 200)
        self.assertEqual((await self.request("/servo")).status_code, 503)
        self.assertEqual((await self.request("/health")).status_code, 503)
        self.release.set()
        await self.event(self.ready)
        response = await self.request("/servo")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"color": "white"})
        await self.stop()
        self.assertTrue(self.full_stopped.is_set())
        self.assertFalse(self.cleaned.is_set())
        self.assertEqual(self.threads[0], self.threads[1])
        self.assertNotEqual(self.threads[0], threading.get_ident())

    async def test_blocking_full_lifespan_does_not_block_led(self):
        await self.start(lambda: self.full(slow=True))
        await self.event(self.loading)
        self.assertEqual((await self.request("/led/status", "POST")).status_code, 200)
        self.assertEqual((await self.request("/servo")).status_code, 503)
        self.release.set()
        await self.event(self.ready)
        await self.stop()
        self.assertFalse(self.failed.is_set())

    async def test_import_failure_is_fatal_and_cleans_early_owner(self):
        def load():
            raise ImportError("required driver missing")

        await self.start(load)
        await self.event(self.failed)
        self.assertTrue(self.cleaned.is_set())
        self.assertFalse(self.full_stopped.is_set())
        await self.stop(failed=True)

    async def test_lifespan_failure_is_fatal(self):
        await self.start(lambda: self.full(fail=True))
        await self.event(self.failed)
        self.assertTrue(self.cleaned.is_set())
        await self.stop(failed=True)

    async def test_shutdown_during_full_initialization_tears_down_on_worker(self):
        await self.start(lambda: self.full(slow=True), shutdown_timeout=0.01)
        await self.event(self.loading)
        await self.stop(failed=True)
        self.assertFalse(self.cleaned.is_set())
        self.assertFalse(self.full_stopped.is_set())
        self.release.set()
        await self.event(self.full_stopped)
        await asyncio.to_thread(self.dispatcher._worker.join, 1)
        self.assertIsNone(self.dispatcher._runtime)
        self.assertFalse(self.cleaned.is_set())
        self.assertTrue(self.failed.is_set())
        self.assertEqual(self.threads[0], self.threads[1])

    async def test_shutdown_during_import_is_bounded_and_never_starts_full_hardware(self):
        runtime = self.full()

        def load():
            self.loading.set()
            self.release.wait(3)
            return runtime

        await self.start(load, shutdown_timeout=0.01)
        await self.event(self.loading)
        await self.stop(failed=True)
        self.assertTrue(self.dispatcher._worker.is_alive())
        self.assertFalse(self.cleaned.is_set())
        self.release.set()
        await self.event(self.cleaned)
        await asyncio.to_thread(self.dispatcher._worker.join, 1)
        self.assertFalse(self.dispatcher._worker.is_alive())
        self.assertFalse(self.ready.is_set())
        self.assertTrue(self.failed.is_set())
        self.assertEqual(self.early_cleanup_threads, [self.dispatcher._worker.ident])

    async def test_shutdown_waits_for_full_owner_before_reporting_complete(self):
        await self.start(lambda: self.full(slow_shutdown=True))
        self.assertEqual(self.dispatcher.shutdown_timeout, 20.0)
        await self.event(self.ready)
        await self.incoming.put({"type": "lifespan.shutdown"})
        await self.event(self.cleanup_started)
        # Reproduce cleanup outliving the old five-second completion deadline.
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.outgoing.get(), 5.1)
        self.assertFalse(self.cleaned.is_set())
        self.release.set()
        result = await asyncio.wait_for(self.outgoing.get(), 2)
        self.assertEqual(result["type"], "lifespan.shutdown.complete")
        await self.lifecycle
        self.assertTrue(self.full_stopped.is_set())
        self.assertFalse(self.dispatcher._worker.is_alive())
        self.assertFalse(self.failed.is_set())

    async def test_full_cleanup_timeout_fails_without_concurrent_early_cleanup(self):
        await self.start(lambda: self.full(slow_shutdown=True), shutdown_timeout=0.01)
        await self.event(self.ready)
        await self.stop(failed=True)
        self.assertTrue(self.cleanup_started.is_set())
        self.assertFalse(self.full_stopped.is_set())
        self.assertFalse(self.cleaned.is_set())
        self.assertTrue(self.dispatcher._worker.is_alive())
        self.release.set()
        await self.event(self.full_stopped)

    async def test_full_cleanup_exception_reports_failure_without_double_cleanup(self):
        await self.start(lambda: self.full(fail_shutdown=True))
        await self.event(self.ready)
        await self.stop(failed=True)
        self.assertFalse(self.cleaned.is_set())
        self.assertFalse(self.full_stopped.is_set())
        self.assertFalse(self.dispatcher._worker.is_alive())

    async def test_early_cleanup_exception_reports_failure(self):
        runtime = self.full()

        def load():
            self.loading.set()
            self.release.wait(3)
            return runtime

        def cleanup():
            self.early_cleanup_threads.append(threading.get_ident())
            raise RuntimeError("early LED cleanup failed")

        await self.start(load, cleanup_early=cleanup)
        await self.event(self.loading)
        await self.incoming.put({"type": "lifespan.shutdown"})
        await self.event(self.dispatcher._stop)
        self.release.set()
        result = await asyncio.wait_for(self.outgoing.get(), 2)
        self.assertEqual(result["type"], "lifespan.shutdown.failed")
        await self.lifecycle
        self.assertEqual(self.fatal_calls, 1)
        self.assertEqual(self.early_cleanup_threads, [self.dispatcher._worker.ident])
        self.assertFalse(self.ready.is_set())


if __name__ == "__main__":
    unittest.main()
