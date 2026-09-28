"""SpeechEmotionService — public orchestrator.

Receives `(user, wav_bytes, duration_s)` per utterance from voice_service,
buffers recognition results per user, periodically flushes with polarity-
bucket dedup, and POSTs `speech_emotion.detected` sensing events to the OS server.

Architecture mirrors `EmotionPerception` in the face sensing pipeline:

    submit()                            # non-blocking
        │  (queue.put_nowait)
        ▼
    worker thread  ── HTTP recognize ──▶ perception-service /api/dl/ser/recognize
        │
        ▼
    per-user buffer[user] = [Inference, …]
        ▲
        │  (flush thread wakes every FLUSH_S)
        ▼
    flush:
        - drop neutral / empty user
        - mode label per user
        - bucket = polarity(mode)
        - TTL dedup keyed on (user, bucket) over DEDUP_WINDOW_S
        - hedged message → POST OS server sensing event

Anti-spam guards (matched to face emotion):

    1. submit() drops audio shorter than MIN_AUDIO_S
    2. submit() drops empty user
    3. worker drops results below the per-label threshold from
       constants.CONFIDENCE_THRESHOLD_BY_LABEL (DEFAULT_CONFIDENCE_THRESHOLD
       for unlisted labels)
    4. flush drops neutral/<unk>/other labels
    5. flush dedups by (user, bucket) over DEDUP_WINDOW_S
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from collections import Counter
from copy import copy
from dataclasses import dataclass
from typing import Optional

import requests

from hal import config
from hal.dedup_sidecar import DedupStateSidecar
from hal.drivers.voice.speech_emotion.base import (
    BaseSpeechEmotionRecognizer,
)
from hal.drivers.voice.speech_emotion.constants import (
    CONFIDENCE_THRESHOLD_BY_LABEL,
    DEFAULT_API_TIMEOUT_S,
    DEFAULT_AUDIO_MAX_FILES,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_DEDUP_WINDOW_S,
    DEFAULT_DL_SER_ENDPOINT,
    DEFAULT_FLUSH_S,
    DEFAULT_JOB_MAX_AGE_S,
    DEFAULT_MIN_AUDIO_S,
    DEFAULT_QUEUE_MAXSIZE,
    SENSING_EVENT_TYPE,
    SpeechEmotionLabel
)
from hal.drivers.voice.speech_emotion.debug_tracer import (  # SER-DEBUG
    audio_stats,
    tracer,
)
from hal.drivers.voice.speech_emotion.emotion2vec import Emotion2VecRecognizer
from hal.drivers.voice.speech_emotion.utils import (
    bucket_for,
    format_message,
    is_neutral,
    normalize_label,
    threshold_for,
)

logger = logging.getLogger("hal.voice.speech_emotion")

# Resolve runtime knobs from hal.config with sensible fallbacks so the
# module imports cleanly even if the config hasn't been bumped yet.
_FLUSH_S: float = float(getattr(config, "SPEECH_EMOTION_FLUSH_S", DEFAULT_FLUSH_S))
_DEDUP_WINDOW_S: float = float(
    getattr(config, "SPEECH_EMOTION_DEDUP_WINDOW_S", DEFAULT_DEDUP_WINDOW_S)
)
_MIN_AUDIO_S: float = float(
    getattr(config, "SPEECH_EMOTION_MIN_AUDIO_S", DEFAULT_MIN_AUDIO_S)
)
_API_URL: str = getattr(config, "SPEECH_EMOTION_API_URL", "") or ""
_API_KEY: str = getattr(config, "SPEECH_EMOTION_API_KEY", "") or ""
_API_TIMEOUT_S: float = float(
    getattr(config, "SPEECH_EMOTION_API_TIMEOUT_S", DEFAULT_API_TIMEOUT_S)
)
_SENSING_URL: str = config.OS_SENSING_URL

# Boot-scoped dedup sidecar — survives HAL service restarts, cleared on a
# full device reboot (tmpfs + boot_id).
_SER_STATE_PATH = "/tmp/hal-ser-state.json"
_AUDIO_DIR: str = getattr(config, "SPEECH_EMOTION_AUDIO_DIR", "") or ""
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")

# The buckets an event can actually be emitted under. Derived from the same two
# predicates the flush path uses rather than from LABEL_BUCKETS.values(), so a
# label added later without a bucket entry (which buckets to "other") is still
# counted as reachable instead of being silently gated away.
# Today this is {"positive", "negative"} — every neutral/other/<unk> label is
# dropped in _process_job before it can reach a bucket.
_REACHABLE_BUCKETS: frozenset = frozenset(
    bucket_for(label) for label in SpeechEmotionLabel if not is_neutral(label)
)


@dataclass(slots=True)
class _Job:
    user: str
    wav_bytes: bytes
    duration_s: float
    # Set at submit(); the worker uses it to discard audio that waited so long
    # in the queue that it no longer describes the user's current mood.
    ts: float


@dataclass(slots=True)
class _Inference:
    user: str
    label: SpeechEmotionLabel
    confidence: float
    duration_s: float
    ts: float
    audio_path: str = ""


def _build_default_recognizer() -> BaseSpeechEmotionRecognizer:
    """Compose URL from DL_BACKEND_URL + DL_SER_ENDPOINT if not preset."""
    url = _API_URL
    if not url and config.DL_BACKEND_URL:
        endpoint = getattr(config, "DL_SER_ENDPOINT", DEFAULT_DL_SER_ENDPOINT)
        url = (
            config.DL_BACKEND_URL.rstrip("/")
            + "/"
            + endpoint.strip("/")
        )
    return Emotion2VecRecognizer(
        url=url,
        api_key=_API_KEY or config.DL_API_KEY,
        timeout_s=_API_TIMEOUT_S,
    )


class SpeechEmotionService:
    """Init once per process; call submit() per utterance.

    Spawns two daemon threads when the recognizer is available — worker
    (drains the submission queue, runs HTTP recognize) and flush (drains
    the per-user buffer every FLUSH_S, dedups, sends to the OS server). Both shut
    down when stop() is called.
    """

    def __init__(
        self,
        recognizer: Optional[BaseSpeechEmotionRecognizer] = None,
        *,
        flush_s: float = _FLUSH_S,
        dedup_window_s: float = _DEDUP_WINDOW_S,
        min_audio_s: float = _MIN_AUDIO_S,
        sensing_url: str = _SENSING_URL,
        audio_dir: str = _AUDIO_DIR,
        audio_max_files: int = DEFAULT_AUDIO_MAX_FILES,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        job_max_age_s: float = DEFAULT_JOB_MAX_AGE_S,
    ):
        self._recognizer: BaseSpeechEmotionRecognizer = (
            recognizer if recognizer is not None else _build_default_recognizer()
        )
        self._flush_s: float = flush_s
        self._dedup_window_s: float = dedup_window_s
        self._min_audio_s: float = min_audio_s
        self._sensing_url: str = sensing_url
        self._audio_dir: str = audio_dir
        self._audio_max_files: int = audio_max_files
        self._job_max_age_s: float = job_max_age_s

        # mutable state — guarded by _lock
        self._lock: threading.RLock = threading.RLock()
        self._buffer: dict[str, list[_Inference]] = {}
        # TTL dedup map restored from the boot-scoped sidecar so a HAL
        # service restart doesn't re-fire the last-known speech emotion on
        # the first flush (same pattern as the motion sidecar).
        self._sidecar: DedupStateSidecar = DedupStateSidecar(
            _SER_STATE_PATH, "speech_emotion"
        )
        self._last_sent_by_key: dict[tuple[str, str], float] = self._sidecar.load()
        self._last_flush_ts: float = 0.0

        self._stop_event: threading.Event = threading.Event()
        self._jobs: queue.Queue[Optional[_Job]] = queue.Queue(maxsize=queue_maxsize)
        self._worker_thread: Optional[threading.Thread] = None
        self._flush_thread: Optional[threading.Thread] = None

        if self.available:
            if self._audio_dir:
                try:
                    os.makedirs(self._audio_dir, exist_ok=True)
                except OSError as e:
                    logger.warning(
                        "[speech_emotion] audio_dir mkdir failed (%s): %s — "
                        "POST will carry empty audio field",
                        self._audio_dir, e,
                    )
                    self._audio_dir = ""
            self._start_workers()
            logger.info(
                "[speech_emotion] SERVICE STARTED — flush=%.1fs dedup=%.1fs "
                "min_audio=%.1fs per-label thresholds=%s default=%.2f "
                "sensing_url=%s audio_dir=%s recognizer=%s",
                flush_s, dedup_window_s, min_audio_s,
                CONFIDENCE_THRESHOLD_BY_LABEL, DEFAULT_CONFIDENCE_THRESHOLD,
                self._sensing_url, self._audio_dir or "<disabled>",
                type(self._recognizer).__name__,
            )
        else:
            logger.warning(
                "[speech_emotion] SERVICE IDLE — recognizer unavailable "
                "(missing DL_BACKEND_URL or endpoint config). submit() will "
                "be a no-op until restart."
            )

    # --- public API -------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._recognizer is not None and self._recognizer.available

    def submit(self, user: str, wav_bytes: bytes, duration_s: float) -> None:
        """Non-blocking. Drops the sample (and logs) when:
          - service is disabled / recognizer unavailable
          - user is empty (no subject to attribute emotion to)
          - audio is empty or shorter than MIN_AUDIO_S
          - worker queue is full (back-pressure — caller should not retry)

        The caller passes the SAME wav_bytes used for speaker recognition;
        no defensive copy is needed because bytes are immutable in Python.
        """
        logger.debug(
            "[speech_emotion] submit() called: user=%r duration=%.2fs wav=%d bytes",
            user, duration_s, len(wav_bytes) if wav_bytes else 0,
        )
        if not self.available:
            logger.info("[speech_emotion] DROP submit — service unavailable")
            self._debug_submit_drop("service-unavailable", user, wav_bytes, duration_s)
            return
        norm_user = normalize_label(user)
        if not norm_user:
            logger.info("[speech_emotion] DROP submit — user normalized to empty")
            self._debug_submit_drop("empty-user", user, wav_bytes, duration_s)
            return
        if not wav_bytes:
            logger.info("[speech_emotion] DROP submit — wav_bytes empty")
            self._debug_submit_drop("empty-wav", norm_user, wav_bytes, duration_s)
            return
        if duration_s < self._min_audio_s:
            logger.info(
                "[speech_emotion] DROP submit — duration=%.2fs < min=%.2fs",
                duration_s, self._min_audio_s,
            )
            self._debug_submit_drop("too-short", norm_user, wav_bytes, duration_s)
            return
        if self._buckets_saturated(norm_user, time.time()):
            logger.info(
                "[speech_emotion] DROP submit — every bucket for %r is still "
                "inside the dedup window; no label could emit",
                norm_user,
            )
            self._debug_submit_drop(
                "buckets-saturated", norm_user, wav_bytes, duration_s,
            )
            return

        job = _Job(
            user=norm_user, wav_bytes=wav_bytes,
            duration_s=duration_s, ts=time.time(),
        )
        try:
            self._jobs.put_nowait(job)
            logger.info(
                "[speech_emotion] ENQUEUED — user=%r queue_size=%d",
                norm_user, self._jobs.qsize(),
            )
        except queue.Full:
            # Evict the OLDEST, keep the newest. For a real-time affect signal
            # the utterance that just happened is the valuable one; a backlog
            # only describes a mood the user has already moved on from.
            try:
                evicted = self._jobs.get_nowait()
            except queue.Empty:
                evicted = None
            if evicted is not None:
                logger.warning(
                    "[speech_emotion] EVICT — queue full, dropped oldest job "
                    "(user=%r age=%.1fs) to make room",
                    evicted.user, time.time() - evicted.ts,
                )
                self._debug_submit_drop(
                    "queue-evicted-oldest", evicted.user,
                    evicted.wav_bytes, evicted.duration_s,
                )
            try:
                self._jobs.put_nowait(job)
                logger.info(
                    "[speech_emotion] ENQUEUED — user=%r queue_size=%d",
                    norm_user, self._jobs.qsize(),
                )
            except queue.Full:
                # Another producer refilled the slot between the get and the
                # put. Rare, and not worth a retry loop — drop this one.
                logger.warning(
                    "[speech_emotion] DROP submit — worker queue full (size=%d)",
                    self._jobs.qsize(),
                )
                self._debug_submit_drop(
                    "queue-full", norm_user, wav_bytes, duration_s,
                )

    def _buckets_saturated(self, user: str, cur_ts: float) -> bool:
        """True when no label this sample could produce is able to emit.

        Output is capped at one event per (user, bucket) per DEDUP_WINDOW_S, and
        only `_REACHABLE_BUCKETS` are emittable. When every one of them is still
        inside its window, `_flush_user` will suppress whatever comes back — so
        the Silero pass, the base64 expansion, the cloud round trip and the disk
        write are all spent on a result that cannot become an event.

        Deliberately conservative: a bucket counts as closed only if it will
        STILL be closed by the time this sample could reach a flush — queue wait
        + the cloud call + one flush tick. Without that margin a window expiring
        mid-flight would make this gate drop a sample that would have emitted,
        which would turn a pure optimisation into a behaviour change.
        """
        if not _REACHABLE_BUCKETS:
            return False
        # Worst-case submit → flush latency. job_max_age_s caps the queue wait;
        # when it is disabled (0) fall back to the full queue draining at one
        # timeout per job.
        queue_wait = (
            self._job_max_age_s if self._job_max_age_s > 0
            else self._jobs.maxsize * _API_TIMEOUT_S
        )
        horizon = queue_wait + _API_TIMEOUT_S + self._flush_s
        with self._lock:
            for bucket in _REACHABLE_BUCKETS:
                last_ts = self._last_sent_by_key.get((user, bucket))
                if last_ts is None:
                    return False
                if (cur_ts - last_ts) + horizon >= self._dedup_window_s:
                    # Window will have expired before this sample could flush.
                    return False
        return True

    def _debug_submit_drop(  # SER-DEBUG (remove before deploy)
        self, reason: str, user: str, wav_bytes: bytes, duration_s: float,
    ) -> None:
        """SER-DEBUG: trace an utterance rejected before it ever reached the
        worker. Written one-shot (no stage profile) because nothing was run.

        These drops happen on the CALLER's thread, before any trace call is
        open, so they cannot ride the thread-local call the worker path uses.
        """
        if not tracer.enabled:
            return
        tracer.record(
            "recognize",
            reason=reason,
            result={
                "stage": "submit",
                "user": user,
                "submitted_duration_s": round(duration_s, 3),
                "min_audio_s": self._min_audio_s,
                "queue_size": self._jobs.qsize(),
                "available": self.available,
                "input_audio": audio_stats(wav_bytes),
            },
            wavs={"input.wav": wav_bytes} if wav_bytes else None,
        )

    def stop(self) -> None:
        """Signal worker + flush threads to exit. Idempotent."""
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        try:
            self._jobs.put_nowait(None)
        except queue.Full:
            pass

    def to_dict(self) -> dict:
        """Diagnostic snapshot — mirrors EmotionPerception.to_dict shape."""
        with self._lock:
            return {
                "type": "speech_emotion",
                "available": self.available,
                "buffered_users": len(self._buffer),
                "dedup_keys": len(self._last_sent_by_key),
                "queue_size": self._jobs.qsize(),
                "last_flush_ts": self._last_flush_ts,
            }

    # --- worker thread ----------------------------------------------------
    def _start_workers(self) -> None:
        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="speech-emotion-worker", daemon=True,
        )
        self._flush_thread = threading.Thread(
            target=self._flush_loop, name="speech-emotion-flush", daemon=True,
        )
        self._worker_thread.start()
        self._flush_thread.start()

    def _worker_loop(self) -> None:
        logger.info("[speech_emotion] worker thread READY")
        while not self._stop_event.is_set():
            try:
                job = self._jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            if job is None:
                logger.info("[speech_emotion] worker thread received stop sentinel")
                break
            # Audio that sat in the queue this long no longer describes how the
            # user feels now. Recognizing it would emit a stale mood and then
            # let the (user, bucket) dedup suppress the genuinely current one.
            age_s = time.time() - job.ts
            if self._job_max_age_s > 0 and age_s > self._job_max_age_s:
                logger.info(
                    "[speech_emotion] DROP — stale job: user=%r waited %.1fs > %.1fs",
                    job.user, age_s, self._job_max_age_s,
                )
                if tracer.enabled:  # SER-DEBUG
                    # One-shot like _debug_submit_drop: nothing ran, so there is
                    # no open thread-local call to finish — but this drop happens
                    # on the WORKER thread, hence stage="worker".
                    tracer.record(
                        "recognize",
                        reason="stale-job",
                        result={
                            "stage": "worker",
                            "user": job.user,
                            "queued_age_s": round(age_s, 3),
                            "job_max_age_s": self._job_max_age_s,
                            "queue_size": self._jobs.qsize(),
                            "input_audio": audio_stats(job.wav_bytes),
                        },
                        wavs={"input.wav": job.wav_bytes} if job.wav_bytes else None,
                    )
                continue
            try:
                self._process_job(job)
            except Exception as e:
                logger.exception("[speech_emotion] worker loop error")
                # SER-DEBUG: close the open trace so a crash mid-pipeline is
                # written out instead of being dropped by the next begin().
                tracer.fail("worker-exception", exception=repr(e))
                tracer.finish()
        logger.info("[speech_emotion] worker thread EXIT")

    def _process_job(self, job: _Job) -> None:
        t0 = time.time()
        logger.debug(
            "[speech_emotion] worker -> recognize: user=%r duration=%.2fs",
            job.user, job.duration_s,
        )
        # SER-DEBUG: open ONE trace for this utterance. The engine adds the
        # prefilter metrics, the HTTP exchange and the stage timings into the
        # same call, so a single dir holds the whole path from mic to verdict.
        if tracer.enabled:
            tracer.begin(
                "recognize",
                stage="worker",
                user=job.user,
                submitted_duration_s=round(job.duration_s, 3),
                input_audio=audio_stats(job.wav_bytes),
                confidence_thresholds={
                    "by_label": dict(CONFIDENCE_THRESHOLD_BY_LABEL),
                    "default": DEFAULT_CONFIDENCE_THRESHOLD,
                },
            )
            tracer.attach("input.wav", job.wav_bytes)
        with tracer.stage("recognize"):  # SER-DEBUG
            result = self._recognizer.recognize(job.wav_bytes)
        elapsed = time.time() - t0
        if result is None:
            logger.warning(
                "[speech_emotion] DROP — recognizer returned None for user=%r "
                "(took %.2fs; check DL backend reachability / response shape)",
                job.user, elapsed,
            )
            # SER-DEBUG: fallback reason only — the engine's own fail() call is
            # more precise and wins (first reason set is kept).
            tracer.fail("recognizer-returned-none")
            tracer.finish(verdict="dropped", drop_reason="recognizer-returned-none")
            return
        logger.info(
            "[speech_emotion] recognize OK: user=%r label=%s confidence=%.3f (took %.2fs)",
            job.user, result.label, result.confidence, elapsed,
        )
        label = SpeechEmotionLabel(normalize_label(result.label))
        label_threshold = threshold_for(label)
        tracer.note(  # SER-DEBUG
            label_raw=result.label,
            label=label.value,
            confidence=round(result.confidence, 4),
            label_threshold=label_threshold,
            bucket=bucket_for(label),
            is_neutral=is_neutral(label),
        )
        # Neutral can never become an event: _flush_user drops every sample in
        # NEUTRAL_LABELS before the modal vote. Deciding that here, rather than
        # nine stages later, saves the WAV write, the buffer append and the lock
        # for the most common label emotion2vec returns on ordinary speech.
        # Checked before the confidence gate on purpose — the structural reason
        # outranks the numeric one, so the trace names the gate that really decided.
        if is_neutral(label):
            logger.info("[speech_emotion] DROP — neutral label: %s", label)
            tracer.finish(  # SER-DEBUG
                cls=label.value, confidence=result.confidence,
                verdict="dropped", drop_reason="neutral",
            )
            return
        if result.confidence < label_threshold:
            logger.info(
                "[speech_emotion] DROP — low confidence: %s %.3f < %.2f",
                label, result.confidence, label_threshold,
            )
            # Dir stays <ts>_<label>_<conf>: a low-confidence drop HAS a class,
            # and the label is exactly what makes the trace worth reading.
            tracer.finish(  # SER-DEBUG
                cls=label.value, confidence=result.confidence,
                verdict="dropped", drop_reason="low-confidence",
            )
            return

        inf_ts = time.time()
        with tracer.stage("persist_wav"):  # SER-DEBUG
            audio_path = self._persist_wav(job.wav_bytes, job.user, label, inf_ts)
        inf = _Inference(
            user=job.user,
            label=label,
            confidence=result.confidence,
            duration_s=job.duration_s,
            ts=inf_ts,
            audio_path=audio_path,
        )
        with self._lock:
            self._buffer.setdefault(job.user, []).append(inf)
            buf_len = len(self._buffer[job.user])
        logger.info(
            "[speech_emotion] BUFFERED — user=%r label=%s conf=%.3f buf_len=%d audio=%s",
            job.user, inf.label, inf.confidence, buf_len, audio_path or "<none>",
        )
        tracer.finish(  # SER-DEBUG
            cls=label.value, confidence=result.confidence,
            verdict="buffered", buffer_len=buf_len,
            persisted_audio_path=audio_path or None,
        )

    def _persist_wav(
        self,
        wav_bytes: bytes,
        user: str,
        label: SpeechEmotionLabel,
        ts: float,
    ) -> str:
        """Write the WAV buffer to disk and return the path. Empty string on
        skip/failure (audio_dir disabled or I/O error) — caller must tolerate.
        """
        if not self._audio_dir:
            return ""
        safe_user = _SAFE_NAME_RE.sub("_", user) or "unknown"
        safe_label = _SAFE_NAME_RE.sub("_", label.value) or "unknown"
        filename = f"{int(ts * 1000)}_{safe_user}_{safe_label}.wav"
        path = os.path.join(self._audio_dir, filename)
        try:
            with open(path, "wb") as f:
                f.write(wav_bytes)
        except OSError as e:
            logger.warning(
                "[speech_emotion] persist wav failed (%s): %s", path, e,
            )
            tracer.note(persist_error=f"{path}: {e}")  # SER-DEBUG
            return ""
        self._prune_audio_dir()
        return path

    def _prune_audio_dir(self) -> None:
        """Keep only the newest `_audio_max_files` clips in the audio dir.

        Best-effort by design: a prune failure must never fail the write that
        already succeeded, so every OSError is swallowed. Same shape as
        `debug_tracer.SerDebugTracer._prune`.
        """
        if self._audio_max_files <= 0:
            return
        try:
            # Filenames start with `int(ts * 1000)`, a fixed-width value for
            # any date this code will see, so lexicographic order is
            # chronological order and no stat() per file is needed.
            names = sorted(
                n for n in os.listdir(self._audio_dir) if n.endswith(".wav")
            )
            for old in names[: max(0, len(names) - self._audio_max_files)]:
                os.remove(os.path.join(self._audio_dir, old))
        except OSError:
            pass

    # --- flush thread -----------------------------------------------------

    def _flush_loop(self) -> None:
        logger.info(
            "[speech_emotion] flush thread READY (interval=%.1fs)", self._flush_s,
        )
        while not self._stop_event.is_set():
            # wait() returns True if the stop event fires during the wait —
            # use that as the exit signal to avoid one extra flush at shutdown.
            if self._stop_event.wait(self._flush_s):
                logger.info("[speech_emotion] flush thread EXIT")
                return
            try:
                self._flush_once()
            except Exception:
                logger.exception("[speech_emotion] flush failed")

    def _flush_once(self) -> None:
        cur_ts = time.time()
        with self._lock:
            # Prune expired dedup entries (oldest TTL window) BEFORE the
            # empty-buffer return. Expiry is driven by the clock, not by
            # traffic, so pruning only on ticks that happen to have work left
            # `to_dict()["dedup_keys"]` over-reporting for as long as the room
            # stayed quiet — and quiet is exactly when it was read.
            cutoff = cur_ts - self._dedup_window_s
            before = len(self._last_sent_by_key)
            self._last_sent_by_key = {
                k: ts for k, ts in self._last_sent_by_key.items() if ts >= cutoff
            }
            pruned = before - len(self._last_sent_by_key)
            if not self._buffer:
                logger.debug(
                    "[speech_emotion] flush tick: buffer empty (pruned=%d)", pruned,
                )
                return
            buf = copy(self._buffer)
            self._buffer.clear()
            self._last_flush_ts = cur_ts

        logger.info(
            "[speech_emotion] flush tick: users=%d dedup_keys=%d (pruned=%d)",
            len(buf), len(self._last_sent_by_key), pruned,
        )
        for user, inferences in buf.items():
            if not user or not inferences:
                continue
            self._flush_user(user, inferences, cur_ts)

    def _flush_user(
        self, user: str, inferences: list[_Inference], cur_ts: float,
    ) -> None:
        logger.debug(
            "[speech_emotion] flushing user=%r samples=%d labels=[%s]",
            user, len(inferences),
            ", ".join(inf.label for inf in inferences),
        )
        # SER-DEBUG: one trace per (user, flush) decision — the recognize dirs
        # say what each utterance scored, this says what the buffer as a whole
        # turned into and whether the OS server ever heard about it.
        if tracer.enabled:
            tracer.begin(
                "emit",
                user=user,
                flush_ts=cur_ts,
                dedup_window_s=self._dedup_window_s,
                flush_s=self._flush_s,
                samples=[
                    {
                        "label": inf.label.value if hasattr(inf.label, "value")
                        else inf.label,
                        "confidence": round(inf.confidence, 4),
                        "duration_s": round(inf.duration_s, 3),
                        "ts": inf.ts,
                        "age_s": round(cur_ts - inf.ts, 3),
                        "audio_path": inf.audio_path or None,
                    }
                    for inf in inferences
                ],
            )
        non_neutral = [inf for inf in inferences if not is_neutral(inf.label)]
        if not non_neutral:
            logger.info(
                "[speech_emotion] DROP — %s: all %d samples are neutral/<unk>/other",
                user, len(inferences),
            )
            tracer.fail("all-neutral")  # SER-DEBUG
            tracer.finish(verdict="dropped", drop_reason="all-neutral")
            return

        counts = Counter(inf.label for inf in non_neutral)
        dominant_label, _ = counts.most_common(1)[0]
        dom_inferences = [inf for inf in non_neutral if inf.label == dominant_label]
        avg_confidence = sum(inf.confidence for inf in dom_inferences) / len(
            dom_inferences
        )
        bucket = bucket_for(dominant_label)
        latest_audio_path = max(dom_inferences, key=lambda i: i.ts).audio_path
        logger.debug(
            "[speech_emotion] mode for user=%r: label=%s avg_conf=%.3f bucket=%s audio=%s",
            user, dominant_label, avg_confidence, bucket,
            latest_audio_path or "<none>",
        )
        tracer.note(  # SER-DEBUG
            label=dominant_label.value,
            avg_confidence=round(avg_confidence, 4),
            bucket=bucket,
            sample_count=len(inferences),
            non_neutral_count=len(non_neutral),
            dominant_count=len(dom_inferences),
            label_counts={
                (lbl.value if hasattr(lbl, "value") else lbl): n
                for lbl, n in counts.items()
            },
            latest_audio_path=latest_audio_path or None,
        )

        key = (user, bucket)
        with self._lock:
            last_ts = self._last_sent_by_key.get(key)
            if last_ts is not None and (cur_ts - last_ts) < self._dedup_window_s:
                logger.info(
                    "[speech_emotion] DROP — dedup: user=%r bucket=%s "
                    "(last sent %.1fs ago, window=%.1fs)",
                    user, bucket, cur_ts - last_ts, self._dedup_window_s,
                )
                # Still named by the label it WOULD have emitted — a dedup drop
                # is a decision about a real classification, not a failure.
                tracer.finish(  # SER-DEBUG
                    cls=dominant_label.value, confidence=avg_confidence,
                    verdict="dropped", drop_reason="dedup",
                    dedup_age_s=round(cur_ts - last_ts, 3),
                )
                return
            self._last_sent_by_key[key] = cur_ts
            # Snapshot under the lock, write outside it. save() is a filesystem
            # write; holding _lock across it blocked submit(), to_dict() and the
            # worker's buffer append for the duration of a disk I/O on tmpfs.
            dedup_snapshot = dict(self._last_sent_by_key)
        self._sidecar.save(dedup_snapshot)

        message = format_message(dominant_label, avg_confidence, bucket)
        logger.info(
            "[speech_emotion] EMIT — user=%r message=%r audio=%s",
            user, message, latest_audio_path or "<none>",
        )
        with tracer.stage("send_to_sensing"):  # SER-DEBUG
            self._send_to_sensing(
                message=message, user=user, audio_path=latest_audio_path,
            )
        tracer.finish(  # SER-DEBUG — _send_to_sensing noted the POST outcome
            cls=dominant_label.value, confidence=avg_confidence,
            verdict="emitted", message=message,
        )

    # --- transport --------------------------------------------------------

    def _send_to_sensing(
        self, *, message: str, user: str, audio_path: str = "",
    ) -> None:
        """POST sensing event to the OS server with 3x retry on connection error / 503.

        Same shape as SensingSender.send but carries `current_user`
        explicitly so the OS server sensing handler doesn't have to look it up.
        ``audio_path`` is the on-disk path of the latest WAV that produced
        the dominant label this flush; empty when persistence is disabled
        or the write failed.
        """
        if not self._sensing_url:
            logger.warning(
                "[speech_emotion] send_to_sensing skipped — empty sensing_url"
            )
            tracer.note_section("sensing", {"sent": False, "skipped": "no-url"})  # SER-DEBUG
            return
        payload = {
            "type": SENSING_EVENT_TYPE,
            "message": message,
            "current_user": user,
            "audio": audio_path,
        }
        logger.info(
            "[speech_emotion] POST -> %s payload.user=%r payload.type=%s audio=%s",
            self._sensing_url, user, SENSING_EVENT_TYPE, audio_path or "<none>",
        )
        tracer.note_section("sensing", {  # SER-DEBUG
            "url": self._sensing_url,
            "payload": payload,
        })
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(self._sensing_url, json=payload, timeout=5)
            except requests.ConnectionError as e:
                if attempt < max_retries:
                    logger.warning(
                        "[speech_emotion] OS server unreachable (attempt %d/%d), "
                        "retry in 2s",
                        attempt, max_retries,
                    )
                    time.sleep(2)
                    continue
                logger.warning(
                    "[speech_emotion] OS server unreachable after %d attempts: %s",
                    max_retries, e,
                )
                tracer.note_section("sensing", {  # SER-DEBUG
                    "sent": False, "attempts": attempt,
                    "error": f"unreachable: {e}",
                })
                return
            except requests.RequestException as e:
                logger.warning("[speech_emotion] OS server POST failed: %s", e)
                tracer.note_section("sensing", {  # SER-DEBUG
                    "sent": False, "attempts": attempt,
                    "error": f"{type(e).__name__}: {e}",
                })
                return

            if resp.status_code == 503 and attempt < max_retries:
                logger.warning(
                    "[speech_emotion] OS server 503, retry %d/%d in 2s",
                    attempt, max_retries,
                )
                time.sleep(2)
                continue
            if resp.status_code != 200:
                logger.warning(
                    "[speech_emotion] OS server returned %d: %s",
                    resp.status_code, resp.text[:200],
                )
                tracer.note_section("sensing", {  # SER-DEBUG
                    "sent": False, "attempts": attempt,
                    "status_code": resp.status_code, "body": resp.text[:500],
                })
                return
            logger.info(
                "[speech_emotion] SENT -> OS server 200 OK (attempt=%d): %s",
                attempt, message,
            )
            tracer.note_section("sensing", {  # SER-DEBUG
                "sent": True, "attempts": attempt, "status_code": 200,
            })
            return
