"""Gemini teardown must deliver task outcomes before closing its IO loop."""

import asyncio
import gc
from concurrent.futures import CancelledError

import pytest
from google.genai.errors import APIError

from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


@pytest.mark.parametrize("code", [1000, 1011])
def test_completed_receive_error_is_delivered_when_loop_stops(code):
    loop = asyncio.new_event_loop()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    failure = APIError(code, {})

    async def receive():
        # Close and receive failure can become ready in the same loop iteration.
        loop.stop()
        raise failure

    future = asyncio.run_coroutine_threadsafe(receive(), loop)
    GeminiLiveAgent._run_io_loop(loop)
    gc.collect()
    assert future.done(), "receive outcome was stranded during teardown"
    with pytest.raises(APIError) as caught:
        future.result()
    assert caught.value is failure
    assert not errors
    assert loop.is_closed()


def test_pending_receive_is_cancelled_and_finalized_before_loop_closes():
    loop = asyncio.new_event_loop()
    finalized = []

    async def receive():
        try:
            loop.call_soon(loop.stop)
            await asyncio.Event().wait()
        finally:
            finalized.append(True)

    future = asyncio.run_coroutine_threadsafe(receive(), loop)
    GeminiLiveAgent._run_io_loop(loop)
    assert finalized == [True]
    with pytest.raises(CancelledError):
        future.result(timeout=0)
    assert loop.is_closed()
