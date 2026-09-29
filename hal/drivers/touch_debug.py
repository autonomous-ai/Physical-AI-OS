"""
TOUCH-DEBUG: per-gesture trace files for the TTP223 edge -> session -> gesture -> action flow.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_current: Optional[Dict[str, Any]] = None
_enabled: Optional[bool] = None
_base: Optional[Path] = None
_pad_labels: Dict[int, str] = {}

# A cycle that has gone this long without resolving is flushed by the idle timer.
# Covers the startup-settle burst, which returns from `_on_edge` before any
# session timer is armed and so would otherwise never close.
FLUSH_IDLE_S: float = 2.0

# Hard cap on edges held per cycle. Continuous petting inside the pet cooldown
# can produce edges indefinitely; a debug aid must not grow without bound.
MAX_EDGES: int = 500

_flush_timer: Optional[threading.Timer] = None


def _init() -> bool:
    """Resolve config once. Returns whether tracing is on."""
    global _enabled, _base, _pad_labels
    if _enabled is not None:
        return _enabled
    _enabled = os.environ.get("HAL_TOUCH_DEBUG", "false").lower() in ("1", "true", "yes")
    if not _enabled:
        return False
    default_dir = Path(__file__).resolve().parent / "touch_logs"
    base = Path(os.environ.get("HAL_TOUCH_DEBUG_DIR", str(default_dir)))
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Source tree read-only on a device deploy — fall back rather than
        # silently disabling, which would look like the feature was never on.
        import tempfile

        base = Path(tempfile.gettempdir()) / "hal-touch-debug"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("TOUCH-DEBUG disabled (no writable dir)")
            _enabled = False
            return False
        logger.warning("TOUCH-DEBUG: falling back to %s", base)
    _base = base
    _pad_labels = _parse_pad_labels(os.environ.get("HAL_TOUCH_DEBUG_PADS", ""))
    logger.info("TOUCH-DEBUG tracing ON -> %s", base)
    return True


def _parse_pad_labels(raw: str) -> Dict[int, str]:
    """Parse "96=S1,100=S4" into {96: "S1", 100: "S4"}. Malformed entries are skipped
    rather than raising — a typo in an env var must not kill touch.
    """
    out: Dict[int, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        line_s, _, label = part.partition("=")
        try:
            out[int(line_s.strip())] = label.strip()[:16]
        except ValueError:
            continue
    return out


def _pad(line: int) -> str:
    """Label for a line. Defaults to the line number: the board's historical S-names
    (S1/S2/S4) do not follow line order after two relocations, so inventing them here
    would assert something false.
    """
    return _pad_labels.get(line, f"L{line}")


def _max_entries() -> int:
    try:
        return int(os.environ.get("HAL_TOUCH_DEBUG_MAX_ENTRIES", "200"))
    except ValueError:
        return 200


def _prune() -> None:
    cap = _max_entries()
    if cap <= 0 or _base is None:
        return
    try:
        files = sorted(
            (f for f in _base.iterdir() if f.is_file() and f.suffix == ".json"),
            reverse=True,
        )
        for stale in files[cap:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except Exception:
        pass


def _arm_idle_flush() -> None:
    """(Re)arm the safety flush. Called with _lock held."""
    global _flush_timer
    if _flush_timer is not None:
        _flush_timer.cancel()
    _flush_timer = threading.Timer(FLUSH_IDLE_S, _on_idle_flush)
    _flush_timer.daemon = True
    _flush_timer.start()


def _on_idle_flush() -> None:
    """Close a cycle that went quiet without resolving to a gesture."""
    with _lock:
        trace = _current
        if trace is None:
            return
        edges = trace.get("edges") or []
        all_suppressed = bool(edges) and all(e.get("suppressed") for e in edges)
    finish("IGNORED-settle" if all_suppressed else "IGNORED-unresolved")


def start_cycle(chip: int, lines: List[int], axis: Optional[List[int]] = None) -> None:
    """Open a trace for one gesture cycle."""
    if not _init():
        return
    try:
        with _lock:
            if _current is not None:
                _arm_idle_flush()
                return
            _new_trace_locked(chip, lines, axis)
            _arm_idle_flush()
    except Exception as e:
        logger.debug("TOUCH-DEBUG start_cycle failed: %s", e)


def _new_trace_locked(chip: int, lines: List[int], axis: Optional[List[int]]) -> None:
    global _current
    _current = {
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "_t0": time.monotonic(),
        "chip": chip,
        "lines": list(lines),
        "pads": {str(l): _pad(l) for l in lines},
        "axis": list(axis) if axis else None,
        "edges": [],
        "sessions": [],
        "_pending": [],
        "_dropped_edges": 0,
    }


def note_edge(line: int, level: int, suppressed: bool = False) -> None:
    """Record one GPIO edge. Called from the lgpio callback — append only."""
    if not _init():
        return
    try:
        with _lock:
            if _current is None:
                return
            if len(_current["edges"]) >= MAX_EDGES:
                _current["_dropped_edges"] += 1
                return
            rec = {
                "t_ms": round((time.monotonic() - _current["_t0"]) * 1000, 1),
                "line": line,
                "pad": _pad(line),
                "level": level,
                "suppressed": suppressed,
            }
            _current["edges"].append(rec)
            if not suppressed:
                _current["_pending"].append(rec)
            _arm_idle_flush()
    except Exception as e:
        logger.debug("TOUCH-DEBUG note_edge failed: %s", e)


def note_session_end(count: int) -> None:
    """Close off the edges seen since the last boundary into one session."""
    if not _init():
        return
    try:
        with _lock:
            if _current is None:
                return
            pending = _current["_pending"]
            _current["_pending"] = []
            _current["sessions"].append(_summarise_session(pending, count, _current))
            _arm_idle_flush()
    except Exception as e:
        logger.debug("TOUCH-DEBUG note_session_end failed: %s", e)


def _summarise_session(edges: List[Dict[str, Any]], count: int,
                       trace: Dict[str, Any]) -> Dict[str, Any]:
    """Per-contact arithmetic — the measurement the classifier is tuned from."""
    seq: List[List[Any]] = []
    for e in edges:
        if e["level"] != 0:
            continue
        if seq and seq[-1][0] == e["pad"]:
            continue
        seq.append([e["pad"], e["t_ms"]])

    times = [t for _, t in seq]
    deltas = [round(b - a, 1) for a, b in zip(times, times[1:])]
    distinct = list(dict.fromkeys(p for p, _ in seq))
    return {
        "n": count,
        "t_ms": edges[0]["t_ms"] if edges else None,
        "ended_t_ms": round((time.monotonic() - trace["_t0"]) * 1000, 1),
        "edge_count": len(edges),
        "steps": len(seq),
        "distinct_pads": distinct,
        "touch_order": seq,
        "adjacent_deltas_ms": deltas,
        "span_ms": round(times[-1] - times[0], 1) if len(times) > 1 else 0.0,
        "primary_pad": seq[0][0] if seq else None,
    }


def note_classifier(**fields: Any) -> None:
    """Record the DRIVER's own view of the cycle."""
    if not _init():
        return
    try:
        with _lock:
            if _current is not None:
                _current["classifier"] = dict(fields)
    except Exception as e:
        logger.debug("TOUCH-DEBUG note_classifier failed: %s", e)


def note_decision(gesture: str, reason: str, session_count: int) -> None:
    """Record which gesture the driver resolved to, and why."""
    if not _init():
        return
    try:
        with _lock:
            if _current is not None:
                _current["decision"] = {
                    "session_count": session_count,
                    "gesture": gesture,
                    "reason": reason,
                }
    except Exception as e:
        logger.debug("TOUCH-DEBUG note_decision failed: %s", e)


def note_action(fn: str, source: str, **fields: Any) -> None:
    """Record the action dispatched and the device state it ran against."""
    if not _init():
        return
    try:
        state_snapshot = _read_state()
        with _lock:
            if _current is not None:
                _current["action"] = {
                    "fn": fn,
                    "source": source,
                    "device_state_at_dispatch": state_snapshot,
                    **fields,
                }
    except Exception as e:
        logger.debug("TOUCH-DEBUG note_action failed: %s", e)


def _read_state() -> Dict[str, Any]:
    """Snapshot the flags the touch actions branch on. Imported lazily and defensively —
    app_state pulls in most of HAL, and a debug aid must not be the reason a driver
    fails to import.
    """
    try:
        import hal.app_state as state

        return {
            "sleeping": getattr(state, "_sleeping", None),
            "mic_muted": getattr(state, "_mic_muted", None),
            "speaker_muted": getattr(state, "_speaker_muted", None),
            "hw_mic_switch": getattr(state, "_hw_mic_switch_muted", None),
            "enrolling": getattr(state, "_enrolling", None),
        }
    except Exception:
        return {}


def finish(status: str) -> None:
    """Close the cycle and write it out."""
    global _current, _flush_timer
    if not _init():
        return
    try:
        with _lock:
            trace = _current
            _current = None
            if _flush_timer is not None:
                _flush_timer.cancel()
                _flush_timer = None
        if trace is None or _base is None:
            return
        if trace["_pending"]:
            trace["sessions"].append(
                _summarise_session(trace["_pending"], -1, trace)
            )
        trace["_pending"] = []
        trace["total_ms"] = round((time.monotonic() - trace.pop("_t0")) * 1000, 1)
        dropped = trace.pop("_dropped_edges", 0)
        if dropped:
            trace["edges_dropped"] = dropped
        trace.pop("_pending", None)
        _log_summary(status, trace)
        # Off-thread: json.dump + fsync must never sit on a Timer thread that
        # the driver still needs, and must never reach the lgpio callback path.
        threading.Thread(
            target=_write, args=(status, trace), daemon=True, name="touch-debug-write"
        ).start()
    except Exception as e:
        logger.debug("TOUCH-DEBUG finish failed: %s", e)


def _write(status: str, trace: Dict[str, Any]) -> None:
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in status)[:40]
        path = _base / f"{stamp}_{safe}.json"
        if path.exists():
            n = 2
            while (_base / f"{stamp}_{safe}-{n}.json").exists():
                n += 1
            path = _base / f"{stamp}_{safe}-{n}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(trace, f, indent=2, ensure_ascii=False)
        _prune()
    except Exception as e:
        logger.debug("TOUCH-DEBUG write failed: %s", e)


def _log_summary(status: str, trace: Dict[str, Any]) -> None:
    """One INFO line accounting for the whole gesture.

    INFO deliberately: the driver's own decision lines are logger.debug and never appear
    at the shipped HAL_LOG_LEVEL, which is the gap this closes.
    """
    try:
        cl = trace.get("classifier") or {}
        pads = sorted({e["pad"] for e in trace["edges"] if not e["suppressed"]})
        spans = [s["span_ms"] for s in trace["sessions"] if s.get("span_ms")]
        steps = sum(s.get("steps", 0) for s in trace["sessions"])
        action = (trace.get("action") or {}).get("fn", "-")
        logger.info(
            "TOUCH-TRACE %s pads=%s edges=%d steps=%d contacts=%d span=%sms "
            "moved=%s swipe=%s -> %s (resolved +%.0fms)",
            status, pads, len(trace["edges"]), steps, len(trace["sessions"]),
            max(spans) if spans else 0, cl.get("moved"), cl.get("is_swipe"),
            action, trace["total_ms"],
        )
    except Exception:
        pass
