"""Regression tests for retrying a music stream that dies right after starting."""

import io
import time

from hal.drivers.voice import music_service
from hal.drivers.voice.music_service import MusicService


class _FakeProc:
    """Stand-in for a Popen that exits with `rc` after `alive_s` seconds."""

    def __init__(self, rc: int = 0, alive_s: float = 0.0, stderr: bytes = b""):
        self._rc = rc
        self._die_at = time.time() + alive_s
        self.stderr = io.BytesIO(stderr)
        self.terminated = False

    def poll(self):
        return self._rc if time.time() >= self._die_at else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def wait(self, timeout=None):
        return self._rc


def _service(monkeypatch, attempts):
    """MusicService whose stream attempts are driven by `attempts`."""
    monkeypatch.setattr(music_service, "MUSIC_RETRY_BACKOFF_S", 0.01)
    logged = []
    monkeypatch.setattr(
        music_service, "_log_play_event",
        lambda query, title, started_at, ended_at, stopped_by, person: logged.append(stopped_by),
    )

    svc = MusicService()
    svc._resolve_audio_url = lambda query: ("https://youtu.be/x", "Some Song")
    starts = []

    def _fake_start(audio_url):
        rc, alive_s = attempts[len(starts)]
        starts.append(audio_url)
        svc._ffmpeg_proc = _FakeProc(rc=rc, alive_s=alive_s, stderr=b"Invalid data")
        svc._ytdlp_proc = _FakeProc(rc=1)
        svc._aplay_proc = _FakeProc(rc=0)
        return True

    svc._start_stream = _fake_start
    return svc, starts, logged


def _run(svc):
    svc._lock.acquire()
    svc._play_sync("some song")


def test_early_death_retries_and_second_attempt_plays(monkeypatch):
    """First attempt dies 0.2s in (the 403 case) -> retry -> clean playback."""
    svc, starts, logged = _service(monkeypatch, attempts=[(1, 0.2), (0, 0.3)])

    _run(svc)

    assert len(starts) == 2, "a stream that died 0.2s in must be retried"
    assert logged == ["end"]


def test_all_attempts_dying_early_reports_error(monkeypatch):
    """Every attempt dies early -> give up and report an error (apology TTS)."""
    svc, starts, logged = _service(monkeypatch, attempts=[(1, 0.2), (1, 0.2)])

    _run(svc)

    assert len(starts) == music_service.MUSIC_STREAM_TRIES
    assert logged == ["error"]


def test_death_after_real_playback_is_not_retried(monkeypatch):
    """A stream that played past the failure window is not restarted."""
    svc, starts, logged = _service(monkeypatch, attempts=[(1, 0.2), (0, 0.3)])
    monkeypatch.setattr(music_service, "MUSIC_STREAM_FAIL_S", 0.05)

    _run(svc)

    assert len(starts) == 1, "a mid-song failure must not restart the track"
    assert logged == ["error"]


def test_clean_end_does_not_retry(monkeypatch):
    """ffmpeg exiting 0 is a finished track regardless of how short it was."""
    svc, starts, logged = _service(monkeypatch, attempts=[(0, 0.1), (0, 0.1)])

    _run(svc)

    assert len(starts) == 1
    assert logged == ["end"]
