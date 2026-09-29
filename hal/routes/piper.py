"""Piper install + voice download from the admin UI.

Runs `piper_download.py` as a transient systemd unit (survives HAL restart); state via a job file.
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from hal.drivers.voice.tts.piper_catalog import (
    BINARY_URL, CATALOG, DEFAULT_VOICE, voice_urls,
)

router = APIRouter()
logger = logging.getLogger("hal.voice.piper")

PIPER_DIR = os.environ.get("HAL_PIPER_DIR", "/opt/piper")
VOICES_DIR = os.environ.get("HAL_PIPER_VOICES", os.path.join(PIPER_DIR, "voices"))
BIN_PATH = os.path.join(PIPER_DIR, "piper")

# On the SD card: /tmp and /var/log are RAM on this board.
JOB_FILE = os.environ.get("HAL_PIPER_JOB", "/var/lib/autonomous/piper-job.json")
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "piper_download.py")
# Prefix: each run gets a unique unit name (systemd-run refuses a name still reaping).
SYSTEMD_UNIT_PREFIX = "autonomous-piper-download"

# Max time a claim may sit without a worker write before it counts as a failed start.
CLAIM_GRACE_S = 45.0

_IDLE = {
    "active": False, "kind": "", "target": "", "percent": 0,
    "bytes_done": 0, "bytes_total": 0, "error": "", "done": False,
}

# Guards the read-then-write in _claim, so two requests arriving together start
# one download rather than two. There is only ever one HAL, so cross-process
# contention is not a case to handle.
_claim_lock = threading.Lock()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _read_job() -> dict:
    """The job as the worker last left it; an "active" record with a dead pid is reported failed."""
    try:
        with open(JOB_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return dict(_IDLE)
    job = {k: raw.get(k, v) for k, v in _IDLE.items()}
    if not job["active"]:
        return job
    pid = int(raw.get("pid") or 0)
    if pid:
        if _alive(pid):
            return job
        reason = "download stopped unexpectedly"
    else:
        if time.time() - float(raw.get("claimed_at") or 0) < CLAIM_GRACE_S:
            return job
        reason = "download failed to start"
    job.update(active=False, done=True, error=job["error"] or reason)
    return job


def _write_job(rec: dict) -> None:
    try:
        os.makedirs(os.path.dirname(JOB_FILE), exist_ok=True)
        tmp = JOB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        os.replace(tmp, JOB_FILE)
    except OSError as e:
        logger.warning("Piper: cannot write job file %s: %s", JOB_FILE, e)


def _claim(kind: str, target: str) -> bool:
    """Record a job as running before its worker exists; False if one already is."""
    with _claim_lock:
        if _read_job()["active"]:
            return False
        _write_job({
            **_IDLE, "active": True, "kind": kind, "target": target,
            "pid": 0, "claimed_at": time.time(),
        })
        return True


def _spawn(spec: dict) -> None:
    """Start the worker outside hal.service's cgroup via systemd-run (plain child as dev fallback)."""
    argv = [sys.executable, WORKER, JOB_FILE, json.dumps(spec)]
    unit = f"{SYSTEMD_UNIT_PREFIX}-{time.time_ns()}"
    try:
        subprocess.run(
            ["systemd-run", "--collect", "--quiet", f"--unit={unit}",
             "--property=Description=Piper download"] + argv,
            check=True, capture_output=True, timeout=20,
        )
        logger.info("Piper: %s %s started as %s", spec["kind"], spec["target"], unit)
        return
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode("utf-8", "replace").strip() or e
        logger.warning("Piper: systemd-run failed (%s), running in-process "
                       "— this download will not survive a HAL restart", detail)
    except Exception as e:
        logger.warning("Piper: systemd-run unavailable (%s), running in-process "
                       "— this download will not survive a HAL restart", e)
    try:
        subprocess.Popen(argv, start_new_session=True)
    except Exception as e:
        logger.warning("Piper: worker failed to start: %s", e)
        _write_job({**_IDLE, "kind": spec["kind"], "target": spec["target"],
                    "done": True, "error": str(e)})


def _installed_voices() -> list:
    try:
        return sorted(
            f[: -len(".onnx")] for f in os.listdir(VOICES_DIR) if f.endswith(".onnx")
        )
    except OSError:
        return []


def _discard_voice_files(name: str) -> None:
    """Remove every trace of a voice, half-installed or whole."""
    for path in (
        os.path.join(VOICES_DIR, f"{name}.onnx"),
        os.path.join(VOICES_DIR, f"{name}.onnx.part"),
        os.path.join(VOICES_DIR, f"{name}.onnx.json"),
        os.path.join(VOICES_DIR, f"{name}.onnx.json.part"),
    ):
        try:
            os.remove(path)
        except OSError:
            pass


def _sweep_orphans() -> None:
    """Drop sidecars and .part files with no model beside them, skipping a running job's files."""
    job = _read_job()
    busy_with = job["target"] if job["active"] and job["kind"] == "voice" else ""
    try:
        names = os.listdir(VOICES_DIR)
    except OSError:
        return
    models = {f[: -len(".onnx")] for f in names if f.endswith(".onnx")}
    for f in names:
        if busy_with and f.startswith(busy_with + "."):
            continue
        if f.endswith(".part"):
            stale = True
        elif f.endswith(".onnx.json"):
            stale = f[: -len(".onnx.json")] not in models
        else:
            continue
        if stale:
            try:
                os.remove(os.path.join(VOICES_DIR, f))
                logger.info("Piper: removed orphaned %s", f)
            except OSError:
                pass


_sweep_orphans()


@router.get("/voice/piper/status")
def piper_status():
    """What is installed, what can be installed, and any job in flight."""
    installed = _installed_voices()
    return {
        "engine_installed": os.path.isfile(BIN_PATH) and os.access(BIN_PATH, os.X_OK),
        "voices_installed": installed,
        "default_voice": DEFAULT_VOICE,
        "catalog": [
            {
                "name": name,
                "installed": name in installed,
                **{k: v for k, v in meta.items() if k != "path"},
            }
            for name, meta in CATALOG.items()
        ],
        "job": _read_job(),
    }


class VoiceRequest(BaseModel):
    name: Optional[str] = None


@router.post("/voice/piper/install")
def piper_install():
    """Install the engine; idempotent (already-installed is success)."""
    if os.path.isfile(BIN_PATH):
        return {"status": "ok", "already": True}
    if not _claim("engine", "piper"):
        return {"status": "busy", "job": _read_job()}
    _spawn({"kind": "engine", "target": "piper", "url": BINARY_URL,
            "dir": PIPER_DIR, "voices_dir": VOICES_DIR})
    return {"status": "started", "job": _read_job()}


@router.post("/voice/piper/voice")
def piper_voice(req: VoiceRequest):
    """Download one catalogue voice."""
    name = (req.name or DEFAULT_VOICE).strip()
    if name not in CATALOG:
        return {"status": "error", "message": f"unknown voice {name!r}"}
    if name in _installed_voices():
        return {"status": "ok", "already": True}
    if not _claim("voice", name):
        return {"status": "busy", "job": _read_job()}
    onnx_url, json_url = voice_urls(name)
    _spawn({
        "kind": "voice", "target": name,
        "steps": [
            # Sidecar first: it carries the sample rate; a model without it plays pitched.
            {"url": json_url, "dest": os.path.join(VOICES_DIR, f"{name}.onnx.json"),
             "from": 0, "to": 3, "track": False},
            {"url": onnx_url, "dest": os.path.join(VOICES_DIR, f"{name}.onnx"),
             "from": 3, "to": 100, "track": True},
        ],
    })
    return {"status": "started", "job": _read_job()}


@router.post("/voice/piper/voice/remove")
def piper_voice_remove(req: VoiceRequest):
    """Delete one downloaded voice (catalogue names only)."""
    name = (req.name or "").strip()
    if name not in CATALOG:
        return {"status": "error", "message": f"unknown voice {name!r}"}
    if name not in _installed_voices():
        return {"status": "ok", "already": True}
    if _read_job()["active"]:
        return {"status": "busy", "job": _read_job()}
    # Never delete the last model; the device must keep a voice.
    if len(_installed_voices()) <= 1:
        return {
            "status": "error",
            "message": "cannot remove the only installed voice — the device would have none",
        }
    _discard_voice_files(name)
    logger.info("Piper voice %s removed", name)
    return {"status": "ok"}
