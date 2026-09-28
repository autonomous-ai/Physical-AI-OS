"""SER-DEBUG — TEMPORARY DIAGNOSTIC TRACER — REMOVE BEFORE DEPLOY.

Throwaway diagnostic aid for tuning speech emotion recognition, modelled on
the SPEAKER-DEBUG block in ``speaker_recognizer.py`` (and on the facial-emotion
debug logs). It traces every SER utterance to disk — the submitted audio, the
prefilter decision and every metric behind it, the HTTP request/response, the
label + confidence, the per-stage latency/CPU/memory, and the flush/emit
decision that finally reaches the OS server.

TO REMOVE FOR PRODUCTION: delete this file and every line tagged ``SER-DEBUG``
in ``service.py`` / ``emotion2vec.py`` (``grep -rn "SER-DEBUG"`` in this
package). Nothing else imports it. It is OFF by default (production-safe); set
``HAL_SER_DEBUG=true`` to enable it during development.

Env knobs (all optional):
    HAL_SER_DEBUG              "true" to enable (OFF by default)
    HAL_SER_DEBUG_DIR          output root (default: ./speech_emotion_logs beside this file)
    HAL_SER_DEBUG_MAX_ENTRIES  per-kind dir cap, oldest pruned (default 1000; 0 = unbounded)

Layout — one dir per traced event, named ``<timestamp>_<class-id>_<confidence>``
exactly like the speaker/face logs, with ``<timestamp>_FAIL-<reason>`` when the
event never produced a class at all:

    <root>/recognize/<ts>_<label>_<conf>/     one utterance: submit -> HTTP -> buffer
    <root>/recognize/<ts>_FAIL-<reason>/      dropped before any label existed
    <root>/emit/<ts>_<label>_<conf>/          one flush decision for one user
    <root>/emit/<ts>_FAIL-<reason>/           flush produced nothing to send

Each dir holds:
    input.wav        the WAV as submitted (what the mic session produced)
    prefiltered.wav  the trimmed WAV actually uploaded (absent when the
                     prefilter dropped the sample before re-encoding)
    result.json      everything about the decision: context, prefilter metrics,
                     HTTP exchange, label/confidence/threshold, and the final
                     verdict (``accepted`` / ``dropped`` + ``drop_reason``)
    profile.json     per-stage wall-clock / CPU / RSS for the call, kept in its
                     own file so neither it nor result.json buries the other

NOTE: ``speech_emotion_logs/`` lands inside the source tree — don't commit it
(it is git-ignored, and this whole block is meant to be removed before deploy).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Optional

import numpy as np

from hal.drivers.voice.speech_emotion.utils import wav_to_pcm16

logger = logging.getLogger("hal.voice.speech_emotion.debug")


def audio_stats(wav_bytes: Optional[bytes]) -> dict[str, Any]:
    """Best-effort (duration/rms/peak/sample_rate) for a WAV blob. Never raises."""
    if not wav_bytes:
        return {"bytes": 0}
    out: dict[str, Any] = {"bytes": len(wav_bytes)}
    try:
        samples, sample_rate = wav_to_pcm16(wav_bytes)
        out["sample_rate"] = int(sample_rate)
        if sample_rate > 0:
            out["duration_s"] = round(float(samples.size) / sample_rate, 3)
        if samples.size:
            f = samples.astype(np.float64)
            # Normalized to [0, 1] so the numbers read the same as the speaker
            # tracer's, which works on float32 audio.
            out["rms"] = round(float(np.sqrt(np.mean(f ** 2))) / 32768.0, 6)
            out["peak"] = round(float(np.max(np.abs(f))) / 32768.0, 6)
    except Exception as e:  # a debug helper must never break the service
        out["decode_error"] = str(e)
    return out


# ---------------------------------------------------------------------------
# SER-DEBUG: per-stage latency / CPU / memory profiler.
#
# Same idea as the speaker tracer's profiler, kept independent so neither block
# depends on the other being present. Stages form a TREE — a stage opened
# inside another becomes its child — so ``prefilter.silero_vad`` and
# ``api_call.request`` are attributed separately instead of collapsing into one
# opaque total:
#
#   recognize                 the engine call as a whole
#     +- prefilter            RMS trim + Silero gate
#     |    +- decode_wav / rms_trim / silero_vad / encode_wav
#     +- encode_b64           WAV -> base64 (+ encryption wrap)
#     +- api_call
#          +- request       << the HTTP round-trip itself
#          +- decode         response parse (+ decrypt)
#   persist_wav               buffered-sample WAV write
#
# RSS is SAMPLED on a background thread (~20 ms) and each stage reports the
# PEAK inside its own window: endpoint-only sampling reports 0.0 for a stage
# that allocates and frees within its window, and negative for one that runs
# while an earlier allocation is released. ``rss_peak_delta_mb`` is the memory
# number to read; ``rss_end_delta_mb`` is what the stage KEPT and is
# legitimately negative when the allocator hands pages back. RSS and cpu_ms are
# PROCESS-wide, so a concurrent HAL thread lands in these numbers — read one
# stage as an upper bound and prefer the shape across several calls.
_rss_mode: Optional[str] = None
_rss_proc: Any = None


def _rss_bytes() -> Optional[int]:
    """SER-DEBUG: process RSS in bytes; None if unmeasurable.

    ``psutil``/``statm`` report CURRENT RSS (deltas may be negative);
    ``rusage`` is the macOS-without-psutil fallback and is a HIGH-WATER mark,
    so its deltas are growth-only.
    """
    global _rss_mode, _rss_proc
    if _rss_mode is None:
        try:
            import psutil  # optional; present on-device via the HAL deps

            _rss_proc = psutil.Process()
            _rss_mode = "psutil"
        except Exception:
            _rss_mode = "statm" if os.path.exists("/proc/self/statm") else "rusage"
    try:
        if _rss_mode == "psutil":
            return int(_rss_proc.memory_info().rss)
        if _rss_mode == "statm":
            with open("/proc/self/statm", "r") as fh:  # field 1 = resident pages
                return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        if _rss_mode == "rusage":
            import resource
            import sys

            maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # ru_maxrss is bytes on Darwin/BSD, kilobytes on Linux.
            return int(maxrss) if sys.platform == "darwin" else int(maxrss) * 1024
    except Exception:
        return None
    return None


def _mb(n: Optional[float]) -> Optional[float]:
    """SER-DEBUG: bytes -> MB, rounded. Passes None through."""
    return None if n is None else round(float(n) / (1024.0 * 1024.0), 2)


class _StageNode:
    """SER-DEBUG: one node in the stage tree. Children nest under parents."""

    __slots__ = (
        "name", "calls", "ms", "ms_max", "cpu_ms", "thread_cpu_ms",
        "rss_peak_b", "rss_peak_delta_b", "rss_end_delta_b", "rss_after_b",
        "children", "_order",
    )

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0
        self.ms = 0.0
        self.ms_max = 0.0
        self.cpu_ms = 0.0
        self.thread_cpu_ms = 0.0
        self.rss_peak_b: Optional[int] = None
        self.rss_peak_delta_b: Optional[int] = None
        self.rss_end_delta_b: Optional[int] = None
        self.rss_after_b: Optional[int] = None
        self.children: dict[str, "_StageNode"] = {}
        self._order: list[str] = []

    def child(self, name: str) -> "_StageNode":
        """Get-or-create a child, preserving first-seen (pipeline) order."""
        node = self.children.get(name)
        if node is None:
            node = _StageNode(name)
            self.children[name] = node
            self._order.append(name)
        return node

    def kids(self) -> list["_StageNode"]:
        return [self.children[n] for n in self._order]

    def accumulate(
        self,
        ms: float,
        cpu_ms: float,
        thread_cpu_ms: float,
        rss_before: Optional[int],
        rss_after: Optional[int],
        rss_peak: Optional[int],
    ) -> None:
        self.calls += 1
        self.ms += ms
        self.ms_max = max(self.ms_max, ms)
        self.cpu_ms += cpu_ms
        self.thread_cpu_ms += thread_cpu_ms
        if rss_after is not None:
            self.rss_after_b = rss_after
            if rss_before is not None:
                self.rss_end_delta_b = (
                    (self.rss_end_delta_b or 0) + (rss_after - rss_before)
                )
        if rss_peak is not None:
            self.rss_peak_b = (
                rss_peak if self.rss_peak_b is None else max(self.rss_peak_b, rss_peak)
            )
            if rss_before is not None:
                # MAX, not sum: for a repeated stage the useful number is the
                # worst single occurrence, not a total that grows with count.
                growth = max(0, rss_peak - rss_before)
                self.rss_peak_delta_b = (
                    growth if self.rss_peak_delta_b is None
                    else max(self.rss_peak_delta_b, growth)
                )

    def to_dict(self) -> dict[str, Any]:
        kids = self.kids()
        d: dict[str, Any] = {
            "stage": self.name,
            "ms": round(self.ms, 2),
            # This node's OWN time — the parent's glue, not its children's work.
            "self_ms": round(max(0.0, self.ms - sum(k.ms for k in kids)), 2),
            "cpu_ms": round(self.cpu_ms, 2),
            # >100% = used more than one core; ~0% = blocked, not working.
            "cpu_pct": (round(self.cpu_ms / self.ms * 100.0, 1) if self.ms > 0 else None),
            "thread_cpu_ms": round(self.thread_cpu_ms, 2),
            # THE memory number: peak inside this stage minus RSS at entry.
            "rss_peak_delta_mb": _mb(self.rss_peak_delta_b),
            "rss_peak_mb": _mb(self.rss_peak_b),
            # What it KEPT — legitimately negative when pages go back to the OS.
            "rss_end_delta_mb": _mb(self.rss_end_delta_b),
            "rss_after_mb": _mb(self.rss_after_b),
        }
        if self.calls != 1:
            d["calls"] = self.calls
            d["ms_max"] = round(self.ms_max, 2)
        if kids:
            d["children"] = [k.to_dict() for k in kids]
        return d


class _StageProfiler:
    """SER-DEBUG: nested per-stage latency / CPU / memory. Never raises."""

    _SAMPLE_INTERVAL_S = 0.02
    _MAX_SAMPLES = 20000        # ~400 s of sampling; backstop, not a real limit
    _MAX_LIFETIME_S = 300.0     # sampler self-terminates if a call never ends

    def __init__(self, label: str) -> None:
        self.label = label
        self._t0 = time.perf_counter()
        self._cpu0 = time.process_time()
        self._rss0 = _rss_bytes()
        self._root = _StageNode(label)
        # Open-stage stack: a stage entered while another is open becomes its
        # child. This is what makes the output a tree instead of a flat list.
        self._stack: list[_StageNode] = [self._root]
        self._samples: list[tuple[float, int]] = []
        self._stop = threading.Event()
        if self._rss0 is not None:
            self._samples.append((self._t0, self._rss0))
            # Daemon: must never hold up interpreter shutdown. Self-terminates
            # on _MAX_LIFETIME_S so a call that dies before to_dict() can't
            # leak a sampler for the life of the process.
            threading.Thread(
                target=self._sample_loop, name="ser-debug-rss", daemon=True,
            ).start()

    def _sample_loop(self) -> None:
        deadline = self._t0 + self._MAX_LIFETIME_S
        try:
            while not self._stop.wait(self._SAMPLE_INTERVAL_S):
                if (
                    len(self._samples) >= self._MAX_SAMPLES
                    or time.perf_counter() > deadline
                ):
                    return
                rss = _rss_bytes()
                if rss is not None:
                    self._samples.append((time.perf_counter(), rss))
        except Exception:
            pass

    def close(self) -> None:
        """Stop the sampler. Idempotent."""
        self._stop.set()

    def _peak_between(
        self, t0: float, t1: float, *points: Optional[int]
    ) -> Optional[int]:
        """Highest RSS observed in [t0, t1], including the endpoint reads."""
        best: Optional[int] = None
        for p in points:
            if p is not None and (best is None or p > best):
                best = p
        for t, rss in list(self._samples):  # snapshot; sampler only appends
            if t0 <= t <= t1 and (best is None or rss > best):
                best = rss
        return best

    @contextmanager
    def stage(self, name: str):
        """Time + measure one stage. Records even when the body raises."""
        node = self._stack[-1].child(name)
        self._stack.append(node)
        t0 = time.perf_counter()
        cpu0 = time.process_time()
        thread0 = time.thread_time()
        rss_before = _rss_bytes()
        try:
            yield
        finally:
            # `finally`, so a stage that REJECTS (prefilter drop, HTTP error)
            # still reports its cost — a reject pays for the same work as a
            # pass, and is exactly what we tune.
            t1 = time.perf_counter()
            cpu_ms = (time.process_time() - cpu0) * 1000.0
            thread_ms = (time.thread_time() - thread0) * 1000.0
            rss_after = _rss_bytes()
            try:
                if self._stack and self._stack[-1] is node:
                    self._stack.pop()
                node.accumulate(
                    (t1 - t0) * 1000.0, cpu_ms, thread_ms,
                    rss_before, rss_after,
                    self._peak_between(t0, t1, rss_before, rss_after),
                )
            except Exception:  # a profiler must never break the service
                pass

    def to_dict(self) -> Optional[dict[str, Any]]:
        """JSON-safe profile for the trace's profile.json."""
        try:
            self.close()
            t1 = time.perf_counter()
            rss_end = _rss_bytes()
            peak = self._peak_between(self._t0, t1, self._rss0, rss_end)
            total_ms = (t1 - self._t0) * 1000.0
            cpu_ms = (time.process_time() - self._cpu0) * 1000.0
            return {
                "total_ms": round(total_ms, 2),
                "cpu_ms": round(cpu_ms, 2),
                "cpu_pct": (round(cpu_ms / total_ms * 100.0, 1) if total_ms > 0 else None),
                "cpu_count": os.cpu_count(),
                "rss_source": _rss_mode,
                "rss_sample_interval_ms": round(self._SAMPLE_INTERVAL_S * 1000.0, 1),
                "rss_samples": len(self._samples),
                "rss_start_mb": _mb(self._rss0),
                "rss_end_mb": _mb(rss_end),
                "rss_peak_mb": _mb(peak),
                "rss_peak_delta_mb": _mb(
                    None if (peak is None or self._rss0 is None) else peak - self._rss0
                ),
                "rss_end_delta_mb": _mb(
                    None if (rss_end is None or self._rss0 is None)
                    else rss_end - self._rss0
                ),
                "stages": [k.to_dict() for k in self._root.kids()],
            }
        except Exception as e:
            logger.debug("SER-DEBUG profile serialize failed: %s", e)
            return None

    def summary_line(self) -> str:
        """One-line `path=<ms>/<cpu%>/<+peak MB>` summary for the log."""
        try:
            parts = [f"total={(time.perf_counter() - self._t0) * 1000.0:.1f}ms"]

            def walk(node: _StageNode, prefix: str) -> None:
                for k in node.kids():
                    path = f"{prefix}{k.name}"
                    seg = f"{path}{'' if k.calls == 1 else f'x{k.calls}'}={k.ms:.1f}ms"
                    if k.ms > 0:
                        seg += f"/{k.cpu_ms / k.ms * 100.0:.0f}%cpu"
                    peak = _mb(k.rss_peak_delta_b)
                    if peak is not None:
                        seg += f"/+{peak:.1f}MB"
                    parts.append(seg)
                    walk(k, path + ".")

            walk(self._root, "")
            return " ".join(parts)
        except Exception:
            return "<unavailable>"


class _Call:
    """SER-DEBUG: one in-flight traced event, owned by the calling thread.

    The service opens it, the engine adds stages / metrics / attachments as the
    utterance moves through the pipeline, and the service closes it — so one
    utterance produces exactly one trace dir even though the code that knows
    the audio, the HTTP exchange and the buffering verdict lives in three
    different places.
    """

    __slots__ = ("kind", "stamp", "t0", "result", "wavs", "reason", "prof")

    def __init__(self, kind: str, stamp: str, label: str) -> None:
        self.kind = kind
        self.stamp = stamp
        self.t0 = time.time()
        self.result: dict[str, Any] = {}
        self.wavs: dict[str, bytes] = {}
        self.reason: Optional[str] = None
        self.prof = _StageProfiler(label)


class SerDebugTracer:
    """SER-DEBUG: writes per-event trace dirs. Self-contained; never raises."""

    def __init__(self) -> None:
        # OFF by default (production-safe). Set HAL_SER_DEBUG=true to enable
        # during development — any env source works (shell `export`, systemd
        # `Environment=`, docker `-e`). Read once at construction, so restart
        # HAL after changing it.
        self.enabled = os.environ.get("HAL_SER_DEBUG", "false").lower() == "true"
        # Default: a `speech_emotion_logs/` dir right next to this file, so
        # traces are trivial to inspect. Override with HAL_SER_DEBUG_DIR.
        _default_dir = Path(__file__).resolve().parent / "speech_emotion_logs"
        self._base = Path(os.environ.get("HAL_SER_DEBUG_DIR", str(_default_dir)))
        try:
            self._max = int(os.environ.get("HAL_SER_DEBUG_MAX_ENTRIES", "1000"))
        except ValueError:
            self._max = 1000
        self._local = threading.local()
        if self.enabled:
            # Prefer the source-tree dir; if it's read-only (device deploy),
            # fall back to a writable temp dir instead of silently disabling.
            if not self._try_mkdir(self._base):
                import tempfile

                fallback = Path(tempfile.gettempdir()) / "hal-ser-debug"
                if self._try_mkdir(fallback):
                    logger.warning(
                        "SER-DEBUG: %s not writable — using %s", self._base, fallback,
                    )
                    self._base = fallback
                else:
                    logger.warning("SER-DEBUG disabled (no writable dir)")
                    self.enabled = False
            if self.enabled:
                logger.info("SER-DEBUG tracing ON -> %s", self._base)

    # --- helpers ----------------------------------------------------------

    @staticmethod
    def _try_mkdir(p: Path) -> bool:
        try:
            p.mkdir(parents=True, exist_ok=True)
            return True
        except OSError:
            return False

    @staticmethod
    def _stamp() -> str:
        now = time.time()
        return (
            time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
            + f"-{int((now % 1.0) * 1e6):06d}"
        )

    @staticmethod
    def _san(value: Any) -> str:
        s = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_")
        return s[:48] or "na"

    # --- per-call API (service opens, engine fills in, service closes) ----

    def begin(self, kind: str, **context: Any) -> None:
        """Open a traced event on THIS thread. No-op when tracing is off.

        Any previously-open call on the same thread is dropped (its profiler
        sampler stopped) rather than written — an unfinished call means the
        pipeline raised past its ``finish()``, and half a trace is worse than
        none.
        """
        if not self.enabled:
            return
        prev = getattr(self._local, "call", None)
        if prev is not None:
            try:
                prev.prof.close()
            except Exception:
                pass
        call = _Call(kind, self._stamp(), kind)
        call.result.update(context)
        self._local.call = call

    def note(self, **fields: Any) -> None:
        """Merge flat fields into this call's result.json. No-op when off."""
        call = getattr(self._local, "call", None)
        if call is not None:
            call.result.update(fields)

    def note_section(self, name: str, data: dict[str, Any]) -> None:
        """Merge a nested section (``prefilter``, ``http``, …) into result.json."""
        call = getattr(self._local, "call", None)
        if call is None:
            return
        section = call.result.get(name)
        if isinstance(section, dict):
            section.update(data)
        else:
            call.result[name] = dict(data)

    def attach(self, filename: str, wav_bytes: Optional[bytes]) -> None:
        """Attach a WAV to this call's dir (``input.wav``, ``prefiltered.wav``)."""
        call = getattr(self._local, "call", None)
        if call is not None and wav_bytes:
            call.wavs[filename] = wav_bytes

    def fail(self, reason: str, **fields: Any) -> None:
        """Mark WHY this call produced no label. First reason wins.

        First wins because the earliest gate is the one that actually decided:
        a prefilter drop must not be relabelled by whatever the caller reports
        afterwards.
        """
        call = getattr(self._local, "call", None)
        if call is None:
            return
        if call.reason is None:
            call.reason = reason
        call.result.update(fields)

    def stage(self, name: str) -> Any:
        """Context manager timing one stage; a ``nullcontext`` when off.

        Used at every call site so the production path costs one attribute
        lookup and a ``nullcontext``.
        """
        call = getattr(self._local, "call", None)
        return call.prof.stage(name) if call is not None else nullcontext()

    def finish(
        self,
        *,
        cls: Any = None,
        confidence: Optional[float] = None,
        **fields: Any,
    ) -> None:
        """Close the open call and write its dir. Safe to call unconditionally.

        ``cls``/``confidence`` name the dir (``<ts>_<label>_<conf>``); when
        neither survives, the dir is ``<ts>_FAIL-<reason>`` instead.
        """
        call = getattr(self._local, "call", None)
        if call is None:
            return
        self._local.call = None
        try:
            call.result.update(fields)
            call.result["elapsed_s"] = round(time.time() - call.t0, 3)
            profile = call.prof.to_dict()
            if profile is not None:
                logger.info(
                    "SER-DEBUG profile [%s]: %s",
                    call.kind, call.prof.summary_line(),
                )
            self._write(
                call.kind, stamp=call.stamp, cls=cls, confidence=confidence,
                reason=None if cls else call.reason,
                result=call.result, wavs=call.wavs, profile=profile,
            )
        except Exception as e:  # a debug tracer must never break the service
            logger.debug("SER-DEBUG finish failed: %s", e)

    # --- standalone API (no open call — submit-time drops, flush decisions) --

    def record(
        self,
        kind: str,
        *,
        cls: Any = None,
        confidence: Optional[float] = None,
        reason: Optional[str] = None,
        result: Optional[dict[str, Any]] = None,
        wavs: Optional[dict[str, bytes]] = None,
    ) -> None:
        """Write a one-shot trace dir. Used where there is no staged call."""
        if not self.enabled:
            return
        self._write(
            kind, stamp=self._stamp(), cls=cls, confidence=confidence,
            reason=reason, result=result or {}, wavs=wavs or {}, profile=None,
        )

    # --- writer -----------------------------------------------------------

    def _write(
        self,
        kind: str,
        *,
        stamp: str,
        cls: Any,
        confidence: Optional[float],
        reason: Optional[str],
        result: dict[str, Any],
        wavs: dict[str, bytes],
        profile: Optional[dict[str, Any]],
    ) -> None:
        if not self.enabled:
            return
        try:
            if cls:
                conf = f"{float(confidence):.2f}" if confidence is not None else "na"
                dname = f"{stamp}_{self._san(cls)}_{conf}"
            else:
                dname = f"{stamp}_FAIL-{self._san(reason or 'unknown')}"
            out = self._base / kind / dname
            out.mkdir(parents=True, exist_ok=True)

            payload: dict[str, Any] = {
                "timestamp": stamp,
                "ts": time.time(),
                "service": f"speech_emotion.{kind}",
                "status": "failure" if not cls else "prediction",
            }
            if reason:
                payload["reason"] = reason
            payload.update(result)
            (out / "result.json").write_text(json.dumps(payload, indent=2, default=str))

            # Latency/memory goes in its OWN file — result.json is already dense
            # with the decision, and mixing timings in makes both harder to read.
            if profile:
                (out / "profile.json").write_text(
                    json.dumps(profile, indent=2, default=str)
                )

            for fn, wb in wavs.items():
                if wb:
                    try:
                        (out / fn).write_bytes(wb)
                    except OSError:
                        pass
            self._prune(kind)
        except Exception as e:  # a debug tracer must never break the service
            logger.debug("SER-DEBUG trace failed: %s", e)

    def _prune(self, kind: str) -> None:
        if self._max <= 0:
            return
        try:
            kd = self._base / kind
            dirs = sorted((p for p in kd.iterdir() if p.is_dir()), key=lambda p: p.name)
            for old in dirs[: max(0, len(dirs) - self._max)]:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass


# Module-level singleton: the service and the engine trace into the SAME call,
# which is what lets one utterance produce one dir spanning both files.
tracer = SerDebugTracer()
