"""Optional Smart Turn inference on bounded PCM snapshots, outside capture.

Pipecat and its bundled model are loaded on the worker's first request. The
capture loop only submits snapshots and polls results; it owns turn timing and
must change the request token whenever speech resumes or a new session starts.
"""

import asyncio
import logging
import threading

logger = logging.getLogger("hal.voice")
_MAX_PCM_BYTES = 8 * 16000 * 2


def _create_analyzer():
    from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

    analyzer = LocalSmartTurnAnalyzerV3(sample_rate=16000, cpu_count=1)
    # Pipecat normally applies this on its pipeline StartFrame; constructing
    # the analyzer alone leaves the effective sample rate at zero.
    analyzer.set_sample_rate(16000)
    return analyzer, EndOfTurnState.COMPLETE


class SmartTurnDetector:
    """Single-flight detector with at most one request and one result.

    ``available`` becomes true after model initialization; ``failed`` becomes
    true on any import, loading or inference error. Both are false before the
    first request. Failures disable this instance so callers can use their
    bounded silence fallback without repeated imports or error logs.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False
        self._busy = False
        self._available = False
        self._failed = False
        self._request = None
        self._result = None
        self._thread = threading.Thread(
            target=self._run, name="voice-smart-turn", daemon=True,
        )
        self._thread.start()

    @property
    def available(self) -> bool:
        with self._lock:
            return self._available and not self._closed

    @property
    def failed(self) -> bool:
        with self._lock:
            return self._failed

    def submit(self, token: object, pcm: bytes) -> bool:
        """Accept immutable mono 16 kHz PCM16; never queue behind inference."""
        if not pcm or len(pcm) % 2:
            return False
        with self._lock:
            if self._closed or self._failed or self._busy:
                return False
            self._busy = True
            self._result = None
            self._request = (token, bytes(pcm[-_MAX_PCM_BYTES:]))
            self._wake.set()
            return True

    def poll(self, token: object) -> bool | None:
        """Consume the result only if it belongs to the current speech token."""
        with self._lock:
            result, self._result = self._result, None
            if result is None or result[0] != token or self._closed:
                return None
            return result[1]

    def close(self) -> None:
        """Stop accepting work and discard results, without waiting on ONNX."""
        with self._lock:
            self._closed = True
            self._request = None
            self._result = None
            self._wake.set()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        analyzer = None
        try:
            while True:
                self._wake.wait()
                with self._lock:
                    self._wake.clear()
                    if self._closed:
                        return
                    request, self._request = self._request, None
                if request is None:
                    continue
                token, pcm = request
                try:
                    if analyzer is None:
                        analyzer, complete = _create_analyzer()
                        with self._lock:
                            self._available = True
                        logger.info("Smart Turn ready (local bundled model, CPU threads=1)")
                    analyzer.clear()
                    analyzer.append_audio(pcm, is_speech=True)
                    state, _metrics = loop.run_until_complete(analyzer.analyze_end_of_turn())
                    with self._lock:
                        if not self._closed:
                            self._result = (token, state == complete)
                        self._busy = False
                except Exception as exc:
                    with self._lock:
                        self._failed = True
                        self._available = False
                        self._busy = False
                    logger.warning(
                        "Smart Turn unavailable; using silence fallback. "
                        "Optional HAL pipecat extra and bundled model required: %s", exc,
                    )
                    return
        finally:
            if analyzer is not None:
                try:
                    loop.run_until_complete(analyzer.cleanup())
                except Exception:
                    logger.debug("Smart Turn cleanup failed", exc_info=True)
            loop.close()
