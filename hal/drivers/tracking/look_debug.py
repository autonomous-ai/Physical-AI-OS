"""LOOK-DEBUG: per-look trace dirs for the aim -> capture -> answer flow."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_current: Optional[Dict[str, Any]] = None
_enabled: Optional[bool] = None
_base: Optional[Path] = None


def _init() -> bool:
    """Resolve config once. Returns whether tracing is on."""
    global _enabled, _base
    if _enabled is not None:
        return _enabled
    _enabled = os.environ.get("HAL_LOOK_DEBUG", "false").lower() in ("1", "true", "yes")
    if not _enabled:
        return False
    default_dir = Path(__file__).resolve().parent / "look_logs"
    base = Path(os.environ.get("HAL_LOOK_DEBUG_DIR", str(default_dir)))
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Source tree read-only on a device deploy — fall back rather than
        # silently disabling, which would look like the feature was never on.
        import tempfile

        base = Path(tempfile.gettempdir()) / "hal-look-debug"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("LOOK-DEBUG disabled (no writable dir)")
            _enabled = False
            return False
        logger.warning("LOOK-DEBUG: falling back to %s", base)
    _base = base
    logger.info("LOOK-DEBUG tracing ON -> %s", base)
    return True


def _max_entries() -> int:
    try:
        return int(os.environ.get("HAL_LOOK_DEBUG_MAX_ENTRIES", "200"))
    except ValueError:
        return 200


def _prune() -> None:
    cap = _max_entries()
    if cap <= 0 or _base is None:
        return
    try:
        dirs = sorted((d for d in _base.iterdir() if d.is_dir()), reverse=True)
        for stale in dirs[cap:]:
            shutil.rmtree(stale, ignore_errors=True)
    except Exception:
        pass


# One visual question calls `look` TWICE by design: the first captures and replays the
# turn, the replayed turn re-triggers it and lands on the reuse path.
STALE_TRACE_S: float = 120.0


def start(trigger: str = "look tool") -> None:
    """Open a trace at the moment the `look` tool fires.

    Re-entrant: the turn replay calls this again for the SAME look, and that must extend
    the open trace rather than replace it.
    """
    global _current
    if not _init():
        return
    with _lock:
        if _current is not None:
            if (time.monotonic() - _current["_t0"]) < STALE_TRACE_S:
                _current["look_calls"] = _current.get("look_calls", 1) + 1
                _current["events"].append({
                    "t_ms": round((time.monotonic() - _current["_t0"]) * 1000),
                    "msg": "look re-entered (turn replay)",
                })
                return
            # Orphaned by a turn that never closed — safe to start over.
            _current["events"].append({"t_ms": 0, "msg": "abandoned: superseded"})
        _current = {
            "trigger": trigger,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "_t0": time.monotonic(),
            "look_calls": 1,
            "events": [],
        }


def note(key: str, value: Any) -> None:
    """Record one field on the in-flight trace."""
    if not _init():
        return
    with _lock:
        if _current is not None:
            _current[key] = value


def note_event(msg: str) -> None:
    """Append a timeline entry — cheap breadcrumbs for ordering questions."""
    if not _init():
        return
    with _lock:
        if _current is not None:
            _current["events"].append(
                {"t_ms": round((time.monotonic() - _current["_t0"]) * 1000), "msg": msg}
            )


def encode_annotated(frame: Any, box: Any = None, label: str = "",
                     both_axes: bool = False,
                     centre_lines: bool = True) -> Optional[bytes]:
    """JPEG of `frame` with the detection drawn on."""
    if frame is None:
        return None
    try:
        import cv2

        img = frame.copy()
        fh, fw = img.shape[0], img.shape[1]
        if box is not None:
            x, y, w, h = (int(v) for v in box)
            cv2.rectangle(img, (x, y), (x + w, y + h), (0, 255, 0), 2)
            cx, cy = x + w // 2, y + h // 2
            if centre_lines:
                cv2.line(img, (cx, 0), (cx, fh), (0, 255, 0), 1)
                if both_axes:
                    cv2.line(img, (0, cy), (fw, cy), (0, 255, 0), 1)
        if centre_lines:
            cv2.line(img, (fw // 2, 0), (fw // 2, fh), (0, 0, 255), 1)
            if both_axes:
                cv2.line(img, (0, fh // 2), (fw, fh // 2), (0, 0, 255), 1)
        if label:
            cv2.putText(img, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (0, 255, 255), 2, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
        return buf.tobytes() if ok else None
    except Exception as e:
        logger.debug("LOOK-DEBUG annotate failed: %s", e)
        return None


def _frames_enabled() -> bool:
    return os.environ.get("HAL_LOOK_DEBUG_FRAMES", "true").lower() in ("1", "true", "yes")


def note_step_frame(n: int, frame: Any, box: Any = None, label: str = "") -> None:
    """Buffer a JPEG of what the detector actually saw on this step."""
    if not _init() or not _frames_enabled() or frame is None:
        return
    try:
        jpg = encode_annotated(frame, box, label)
        if jpg is None:
            return
        with _lock:
            if _current is not None:
                _current.setdefault("_step_frames", []).append((n, label, jpg))
    except Exception as e:  # a debug aid must never break the look
        logger.debug("LOOK-DEBUG step frame failed: %s", e)


def stage_ms(name: str, ms: float) -> None:
    """Accumulate elapsed time under a named stage."""
    if not _init():
        return
    with _lock:
        if _current is None:
            return
        stages = _current.setdefault("stages", {})
        entry = stages.setdefault(name, {"ms": 0.0, "n": 0})
        entry["ms"] = round(entry["ms"] + ms, 1)
        entry["n"] += 1


@contextlib.contextmanager
def stage(name: str):
    """Time one stage of the look. Never swallows the exception, and still records the time
    when the stage raises — a stage that failed slowly is exactly the one worth seeing.
    """
    t0 = time.monotonic()
    try:
        yield
    finally:
        stage_ms(name, (time.monotonic() - t0) * 1000.0)


def note_aim(result: Any) -> None:
    """Record the aim outcome — the usual culprit when a frame looks wrong."""
    if not _init():
        return
    try:
        start = getattr(result, "start_yaw", None)
        end = getattr(result, "end_yaw", None)
        note("aim", {
            "aimed": getattr(result, "aimed", None),
            "reason": getattr(result, "reason", None),
            "iterations": getattr(result, "iterations", None),
            # Did the head actually MOVE? start/end are absolute servo yaw, so
            # this distinguishes "decided to move" from "moved".
            "start_yaw": start,
            "end_yaw": end,
            "actually_moved_deg": (None if start is None or end is None
                                   else round(end - start, 2)),
            "yaw_commanded_deg": getattr(result, "yaw_moved_deg", None),
            "final_dx_frac": getattr(result, "final_dx_frac", None),
            "bearing_steps": getattr(result, "bearing_steps", None),
            "bearing_consulted": getattr(result, "bearing_consulted", None),
            "steps": getattr(result, "steps", None),
        })
        note_event(f"aim: {getattr(result, 'reason', '?')}")
    except Exception:
        pass


def note_capture(frame_path: Optional[str]) -> None:
    """Record the exact frame handed to the model."""
    if not _init():
        return
    note("capture_path", frame_path)
    note_event("captured" if frame_path else "capture FAILED")


def _take_profile(trace: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build the timing breakdown and REMOVE the raw stages from the trace."""
    stages: Dict[str, Any] = trace.pop("stages", None) or {}
    total: float = float(trace.get("total_ms") or 0)
    if not stages:
        return None

    def _is_child(name: str) -> bool:
        prefix, _, leaf = name.rpartition(".")
        return bool(prefix) and leaf != "total" and f"{prefix}.total" in stages

    def _pct(ms: float) -> Optional[float]:
        return round(ms / total * 100.0, 1) if total > 0 else None

    device_ms = sum(
        float(v.get("ms") or 0) for k, v in stages.items() if not _is_child(k)
    )
    out: Dict[str, Any] = {}
    for name, v in sorted(stages.items(), key=lambda kv: -float(kv[1].get("ms") or 0)):
        ms = float(v.get("ms") or 0)
        n = int(v.get("n") or 1)
        entry: Dict[str, Any] = {"ms": round(ms, 1), "n": n, "pct": _pct(ms)}
        if n > 1:
            entry["avg_ms"] = round(ms / n, 1)
        if _is_child(name):
            entry["nested_in"] = f"{name.rpartition('.')[0]}.total"
        out[name] = entry

    return {
        "total_ms": round(total, 1),
        "device_ms": round(device_ms, 1),
        "device_pct": _pct(device_ms),
        "waiting_on_model_ms": round(total - device_ms, 1),
        "stages": out,
    }


def _log_profile(profile: Dict[str, Any]) -> None:
    """One line accounting for the whole look, biggest stage first."""
    parts = " ".join(
        f"{k}={v['ms']:.0f}ms" + (f"(x{v['n']})" if v.get("n", 1) > 1 else "")
        for k, v in profile["stages"].items()
    )
    logger.info(
        "LOOK-PROFILE total=%.0fms device=%.0fms waiting_on_model=%.0fms | %s",
        profile["total_ms"], profile["device_ms"],
        profile["waiting_on_model_ms"], parts,
    )


def finish(status: str, question: str = "", answer: str = "", error: str = "") -> None:
    """Close the trace once the turn has an answer, and write it to disk."""
    global _current
    if not _init():
        return
    with _lock:
        trace = _current
        _current = None
    if trace is None or _base is None:
        return
    try:
        trace["question"] = question
        trace["answer"] = answer
        if error:
            trace["error"] = error
        trace["total_ms"] = round((time.monotonic() - trace.pop("_t0")) * 1000)
        profile = _take_profile(trace)
        if profile:
            _log_profile(profile)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in status)[:40]
        outdir = _base / f"{stamp}_{safe}"
        outdir.mkdir(parents=True, exist_ok=True)
        for idx, (n, label, jpg) in enumerate(trace.pop("_step_frames", []) or [], 1):
            safe_label = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in label
            )[:32]
            name = f"step_{n:02d}_{safe_label}.jpg" if safe_label else f"step_{n:02d}.jpg"
            try:
                (outdir / name).write_bytes(jpg)
            except OSError:
                pass
        src = trace.get("capture_path")
        if src and os.path.exists(src):
            try:
                shutil.copyfile(src, outdir / "capture.jpg")
            except OSError:
                pass
        with open(outdir / "result.json", "w", encoding="utf-8") as f:
            json.dump(trace, f, indent=2, ensure_ascii=False)
        if profile:
            with open(outdir / "profile.json", "w", encoding="utf-8") as f:
                json.dump(profile, f, indent=2, ensure_ascii=False)
        _prune()
        logger.info("LOOK-DEBUG wrote %s", outdir)
    except Exception as e:
        logger.debug("LOOK-DEBUG write failed: %s", e)


def abandon(reason: str) -> None:
    """Close a trace that never reached a turn (reused frame, no camera)."""
    finish(f"FAIL-{reason}", error=reason)
