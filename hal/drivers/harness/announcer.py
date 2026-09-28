"""Speak queued Harness updates in snapshots when the device is free.

Raw Harness output is written for a screen (markdown, file lists, paths), so it
is never read out as-is. One worker thread waits until nobody is talking or
listening, takes a snapshot of the queue (see update_queue.py) and renders it:

1. Realtime model, when the provider supports announcements (Gemini text-capable
   models and pipecat_v1, turn-based mode): the update is sent as one
   device-initiated text turn and the model says it in its own voice. A parked
   session is resumed for a result or question, never for progress.
2. Otherwise, or when the model says nothing: the text summarizer rewrites it
   for speech and TTS speaks it with the Harness result chime. Past its timeout
   a sanitized opening of the text is spoken instead. Progress never takes this
   path.

A user capture always wins: prepare_turn() stops a running announcement.
"""

import logging
import re
import threading
import time
from typing import Any, Callable

from hal import config as hal_config
from hal.drivers.harness.update_queue import HarnessUpdate, HarnessUpdateQueue, Snapshot

logger = logging.getLogger("hal.harness.announce")

GATE_POLL_S = 0.25
# How long the fallback waits for a speaker another voice took in the meantime.
BUSY_SPEAKER_WAIT_S = 10.0

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL_RE = re.compile(r"https?://\S+")
_MARKUP_RE = re.compile(r"[*_`#>|]+")
_BULLET_RE = re.compile(r"^\s*(?:[-•]|\d+\.)\s+", re.MULTILINE)
_SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+")


def sanitize_for_speech(kind: str, text: str) -> str:
    """Last-resort spoken form: markup stripped, cut to its opening sentences."""
    text = _CODE_BLOCK_RE.sub(" ", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub(" ", text)
    text = _BULLET_RE.sub("", text)
    text = _MARKUP_RE.sub("", text)
    text = " ".join(text.split())
    limit, sentences = (500, 6) if kind == "question" else (280, 2)
    spoken = " ".join(_SENTENCE_RE.split(text)[:sentences])
    if len(spoken) > limit:
        spoken = spoken[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return spoken


class HarnessAnnouncer:
    def __init__(
        self,
        *,
        queue: HarnessUpdateQueue | None = None,
        get_tts: Callable[[], Any] | None = None,
        get_voice_service: Callable[[], Any] | None = None,
        get_music: Callable[[], Any] | None = None,
        read_voice_mode: Callable[[], dict] | None = None,
        summarize: Callable[[str, str], str] | None = None,
    ) -> None:
        self._queue = queue or HarnessUpdateQueue()
        self._get_tts = get_tts or _app_state_getter("tts_service")
        self._get_voice_service = get_voice_service or _app_state_getter("voice_service")
        self._get_music = get_music or _app_state_getter("music_service")
        self._read_voice_mode = read_voice_mode or _read_voice_mode
        self._summarize = summarize or _summarize_for_speech
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def queue(self) -> HarnessUpdateQueue:
        return self._queue

    def submit(self, update: HarnessUpdate) -> None:
        logger.info(
            "[announce] queued %s update (run=%s, %d chars)",
            update.kind, update.run_id or "-", len(update.text),
        )
        self._queue.put(update)
        self._ensure_worker()

    def stop(self) -> None:
        self._stop.set()

    def _ensure_worker(self) -> None:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="harness-announce")
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._queue.wait(timeout=1.0):
                continue
            if not self.gate_open():
                self._stop.wait(GATE_POLL_S)
                continue
            try:
                self.speak_next()
            except Exception:
                logger.exception("[announce] snapshot failed")

    # --- gate ------------------------------------------------------------------

    def gate_open(self) -> bool:
        """Nobody is speaking, listening or mid-turn, and the grace time has passed."""
        tts = self._get_tts()
        if tts is None or not tts.available or tts.speaking:
            return False
        now = time.time()
        grace = hal_config.HARNESS_ANNOUNCE_GRACE_S
        if now - (tts.last_spoken_time or 0.0) < grace:
            return False
        music = self._get_music()
        if music is not None and getattr(music, "streaming", False):
            return False
        voice = self._get_voice_service()
        if voice is None:
            return True
        if voice.listening or voice.harness_capture_active or voice.live_speaker_busy:
            return False
        if now - (voice.last_transcript_ts or 0.0) < grace:
            return False
        realtime = voice.realtime
        return not (realtime is not None and realtime.turn_in_flight)

    # --- rendering -------------------------------------------------------------

    def _announcing_realtime(self, voice) -> Any:
        """The orchestrator when it may render this snapshot, else None."""
        if voice is None or not hal_config.REALTIME_ENABLED:
            return None
        realtime = voice.realtime
        if realtime is None or voice.live_active or not realtime.supports_announce:
            return None
        from hal.drivers.voice._internal.harness_voice import bypass_realtime

        if bypass_realtime(self._read_voice_mode()):
            # Harness-only voice mode: the realtime agent is out of the loop.
            return None
        return realtime

    def speak_next(self) -> bool:
        """Take one snapshot and speak it; True when something was spoken."""
        from hal.drivers.voice._internal.realtime_announce import (
            AnnouncementItem,
            build_announcement,
            play_realtime_announcement,
        )

        tts = self._get_tts()
        voice = self._get_voice_service()
        realtime = self._announcing_realtime(voice)
        progress_renderable = bool(realtime is not None and not realtime.parked and realtime.available)
        snapshot = self._queue.take_snapshot(progress_renderable=progress_renderable)
        if snapshot is None or tts is None:
            return False
        now = time.monotonic()
        items = [
            AnnouncementItem(kind=u.kind, text=u.text, outcome=u.outcome, age_s=now - u.received_at)
            for u in snapshot.items
        ]
        logger.info(
            "[announce] snapshot: %s (owner=%s, realtime=%s)",
            ", ".join(item.kind for item in items), snapshot.owner or "-", realtime is not None,
        )
        if realtime is not None and realtime.prepare_announcement(allow_resume=not snapshot.progress_only):
            stop = threading.Event()
            result = play_realtime_announcement(
                realtime, tts, voice.strip_rt_markers, build_announcement(items),
                owner=snapshot.owner, stop_event=stop,
            )
            if result.spoken:
                realtime.save_turn(user_text="[Harness update]", agent_text=result.transcript or "(audio only)")
                logger.info("[announce] spoken by realtime: %r", result.transcript[:200])
                return True
            if result.preempted:
                # Nothing was heard yet: keep results and questions for after
                # the user's turn. Once speech started, the snapshot counts as
                # delivered — replaying it would repeat what they heard.
                self._queue.requeue(snapshot)
                logger.info("[announce] preempted by the user before speaking; requeued")
                return False
            logger.info("[announce] realtime produced no speech — falling back")
        if snapshot.progress_only:
            return False
        return self._speak_fallback(tts, snapshot, items)

    def _speak_fallback(self, tts, snapshot: Snapshot, items) -> bool:
        from hal.drivers.voice._internal.realtime_announce import (
            announcement_content,
            speech_instructions,
        )
        from hal.drivers.voice._internal.realtime_turn import _reply_language_name

        instructions = speech_instructions(items, _reply_language_name())
        content = announcement_content(items, hal_config.HARNESS_ANNOUNCE_CONTENT_MAX_CHARS)
        text = _call_with_timeout(
            lambda: self._summarize(instructions, content),
            hal_config.HARNESS_ANNOUNCE_SUMMARIZER_TIMEOUT_S,
        )
        if not text:
            text = " ".join(filter(None, (sanitize_for_speech(i.kind, i.text) for i in items)))
            logger.info("[announce] summarizer unavailable — speaking sanitized text")
        if not text:
            return False
        deadline = time.monotonic() + BUSY_SPEAKER_WAIT_S
        while True:
            # realtime_feedback: the realtime session learns what was said
            # ([TTS HISTORY]), exactly as it did for a raw Harness reply.
            if tts.speak(text, realtime_feedback=True, turn_id=snapshot.owner, harness_result=True):
                logger.info("[announce] spoken by fallback: %r", text[:200])
                return True
            if not tts.speaking or time.monotonic() >= deadline:
                logger.info("[announce] fallback speech refused (muted, cancelled or busy)")
                return False
            time.sleep(GATE_POLL_S)


def _app_state_getter(name: str) -> Callable[[], Any]:
    def get() -> Any:
        from hal import app_state

        return getattr(app_state, name, None)

    return get


def _read_voice_mode() -> dict:
    from hal.drivers.voice._internal.harness_voice import read_voice_mode

    return read_voice_mode()


def _summarize_for_speech(instructions: str, content: str) -> str:
    if not hal_config.REALTIME_SUMMARIZER_API_KEY:
        return ""
    from hal.realtime.summarizer import RealtimeSummarizer

    return RealtimeSummarizer(system_prompt=instructions, max_tokens=400).summarize([content])


def _call_with_timeout(fn: Callable[[], str], timeout_s: float) -> str:
    """Run fn on a daemon thread; "" when it fails or outlives timeout_s."""
    result: list[str] = []

    def run() -> None:
        try:
            result.append(fn() or "")
        except Exception:
            logger.exception("[announce] summarizer failed")

    worker = threading.Thread(target=run, daemon=True, name="harness-announce-summarize")
    worker.start()
    worker.join(timeout=max(0.0, timeout_s))
    return result[0].strip() if result else ""


_default: HarnessAnnouncer | None = None
_default_lock = threading.Lock()


def default_announcer() -> HarnessAnnouncer:
    global _default
    with _default_lock:
        if _default is None:
            _default = HarnessAnnouncer()
        return _default
