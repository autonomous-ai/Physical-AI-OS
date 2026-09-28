"""Pitch-preserving tempo adjustment of streaming mono s16le PCM."""

import subprocess
import select
import threading
from collections.abc import Callable, Iterable, Iterator


def change_tempo(chunks: Iterable[bytes], speed: float, sample_rate: int,
                 cancelled: Callable[[], bool] = lambda: False) -> Iterator[bytes]:
    """Keep bounded pipe buffers; closing the iterator terminates the filter."""
    if speed == 1.0:
        yield from chunks
        return
    if not 0.25 <= speed <= 4.0:
        raise ValueError("TTS tempo must be between 0.25 and 4.0")
    # Each atempo stage stays in its high-quality 0.5..2 range.
    factors = []
    remaining = speed
    while remaining > 2:
        factors.append(2.0)
        remaining /= 2
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    process = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-filter_threads", "1",
         "-probesize", "32", "-analyzeduration", "0",
         "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
         "-af", ",".join(f"atempo={factor}" for factor in factors),
         "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
         "-flush_packets", "1", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    stopped = threading.Event()
    errors = []

    def feed():
        try:
            for chunk in chunks:
                if stopped.is_set() or cancelled():
                    break
                data = memoryview(chunk)
                while data and not stopped.is_set():
                    written = process.stdin.write(data)
                    data = data[written:]
        except Exception as exc:
            if not stopped.is_set():
                errors.append(exc)
        finally:
            try:
                close = getattr(chunks, "close", None)
                if close is not None:
                    close()
            finally:
                process.stdin.close()

    worker = threading.Thread(target=feed, name="tts-tempo-feed", daemon=True)
    worker.start()
    try:
        while True:
            if cancelled():
                return
            ready, _, _ = select.select([process.stdout], [], [], 0.05)
            if not ready:
                continue
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            yield chunk
        worker.join()
        if cancelled():
            return
        if errors:
            raise errors[0]
        if process.wait() != 0:
            raise RuntimeError("TTS tempo filter failed")
    finally:
        stopped.set()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()
        worker.join(timeout=0.1)
