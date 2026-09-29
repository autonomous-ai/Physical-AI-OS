"""SpeechEmotionService — public orchestrator."""

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
from hal.drivers.voice.speech_emotion.debug_tracer import (
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

_SER_STATE_PATH = "/tmp/hal-ser-state.json"
_AUDIO_DIR: str = getattr(config, "SPEECH_EMOTION_AUDIO_DIR", "") or ""
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")

_REACHABLE_BUCKETS: frozenset = frozenset(
    bucket_for(label) for label in SpeechEmotionLabel if not is_neutral(label)
)


@dataclass(slots=True)
class _Job:
    user: str
    wav_bytes: bytes
    duration_s: float
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
    """Init once per process; call submit() per utterance."""

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

        self._lock: threading.RLock = threading.RLock()
        self._buffer: dict[str, list[_Inference]] = {}
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

    @property
    def available(self) -> bool:
        return self._recognizer is not None and self._recognizer.available

    def submit(self, user: str, wav_bytes: bytes, duration_s: float) -> None:
        """Non-blocking."""
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
                logger.warning(
                    "[speech_emotion] DROP submit — worker queue full (size=%d)",
                    self._jobs.qsize(),
                )
                self._debug_submit_drop(
                    "queue-full", norm_user, wav_bytes, duration_s,
                )

    def _buckets_saturated(self, user: str, cur_ts: float) -> bool:
        """True when no label this sample could produce is able to emit."""
        if not _REACHABLE_BUCKETS:
            return False
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
                    return False
        return True

    def _debug_submit_drop(
        self, reason: str, user: str, wav_bytes: bytes, duration_s: float,
    ) -> None:
        """SER-DEBUG: trace an utterance rejected before it ever reached the worker."""
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
            age_s = time.time() - job.ts
            if self._job_max_age_s > 0 and age_s > self._job_max_age_s:
                logger.info(
                    "[speech_emotion] DROP — stale job: user=%r waited %.1fs > %.1fs",
                    job.user, age_s, self._job_max_age_s,
                )
                if tracer.enabled:
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
                tracer.fail("worker-exception", exception=repr(e))
                tracer.finish()
        logger.info("[speech_emotion] worker thread EXIT")

    def _process_job(self, job: _Job) -> None:
        t0 = time.time()
        logger.debug(
            "[speech_emotion] worker -> recognize: user=%r duration=%.2fs",
            job.user, job.duration_s,
        )
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
        with tracer.stage("recognize"):
            result = self._recognizer.recognize(job.wav_bytes)
        elapsed = time.time() - t0
        if result is None:
            logger.warning(
                "[speech_emotion] DROP — recognizer returned None for user=%r "
                "(took %.2fs; check DL backend reachability / response shape)",
                job.user, elapsed,
            )
            tracer.fail("recognizer-returned-none")
            tracer.finish(verdict="dropped", drop_reason="recognizer-returned-none")
            return
        logger.info(
            "[speech_emotion] recognize OK: user=%r label=%s confidence=%.3f (took %.2fs)",
            job.user, result.label, result.confidence, elapsed,
        )
        label = SpeechEmotionLabel(normalize_label(result.label))
        label_threshold = threshold_for(label)
        tracer.note(
            label_raw=result.label,
            label=label.value,
            confidence=round(result.confidence, 4),
            label_threshold=label_threshold,
            bucket=bucket_for(label),
            is_neutral=is_neutral(label),
        )
        # Neutral can never become an event: _flush_user drops every sample in
        # NEUTRAL_LABELS before the modal vote.
        if is_neutral(label):
            logger.info("[speech_emotion] DROP — neutral label: %s", label)
            tracer.finish(
                cls=label.value, confidence=result.confidence,
                verdict="dropped", drop_reason="neutral",
            )
            return
        if result.confidence < label_threshold:
            logger.info(
                "[speech_emotion] DROP — low confidence: %s %.3f < %.2f",
                label, result.confidence, label_threshold,
            )
            tracer.finish(
                cls=label.value, confidence=result.confidence,
                verdict="dropped", drop_reason="low-confidence",
            )
            return

        inf_ts = time.time()
        with tracer.stage("persist_wav"):
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
        tracer.finish(
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
        """Write the WAV buffer to disk and return the path. Empty string on skip/failure
        (audio_dir disabled or I/O error) — caller must tolerate.
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
            tracer.note(persist_error=f"{path}: {e}")
            return ""
        self._prune_audio_dir()
        return path

    def _prune_audio_dir(self) -> None:
        """Keep only the newest `_audio_max_files` clips in the audio dir.

        Best-effort by design: a prune failure must never fail the write that already
        succeeded, so every OSError is swallowed.
        """
        if self._audio_max_files <= 0:
            return
        try:
            names = sorted(
                n for n in os.listdir(self._audio_dir) if n.endswith(".wav")
            )
            for old in names[: max(0, len(names) - self._audio_max_files)]:
                os.remove(os.path.join(self._audio_dir, old))
        except OSError:
            pass

    def _flush_loop(self) -> None:
        logger.info(
            "[speech_emotion] flush thread READY (interval=%.1fs)", self._flush_s,
        )
        while not self._stop_event.is_set():
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
            tracer.fail("all-neutral")
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
        tracer.note(
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
                tracer.finish(
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
        with tracer.stage("send_to_sensing"):
            self._send_to_sensing(
                message=message, user=user, audio_path=latest_audio_path,
            )
        tracer.finish(
            cls=dominant_label.value, confidence=avg_confidence,
            verdict="emitted", message=message,
        )

    def _send_to_sensing(
        self, *, message: str, user: str, audio_path: str = "",
    ) -> None:
        """POST sensing event to the OS server with 3x retry on connection error / 503."""
        if not self._sensing_url:
            logger.warning(
                "[speech_emotion] send_to_sensing skipped — empty sensing_url"
            )
            tracer.note_section("sensing", {"sent": False, "skipped": "no-url"})
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
        tracer.note_section("sensing", {
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
                tracer.note_section("sensing", {
                    "sent": False, "attempts": attempt,
                    "error": f"unreachable: {e}",
                })
                return
            except requests.RequestException as e:
                logger.warning("[speech_emotion] OS server POST failed: %s", e)
                tracer.note_section("sensing", {
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
                tracer.note_section("sensing", {
                    "sent": False, "attempts": attempt,
                    "status_code": resp.status_code, "body": resp.text[:500],
                })
                return
            logger.info(
                "[speech_emotion] SENT -> OS server 200 OK (attempt=%d): %s",
                attempt, message,
            )
            tracer.note_section("sensing", {
                "sent": True, "attempts": attempt, "status_code": 200,
            })
            return
