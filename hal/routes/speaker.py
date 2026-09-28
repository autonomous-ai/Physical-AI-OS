"""FastAPI router for speaker (voice-identity) recognition: /speaker/* and /voice/strangers*.

Routes accept local WAV filepaths only.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import hal.app_state as state
from hal import privacy
from hal import config
from hal.drivers.voice.speaker_recognizer import (
    EmbeddingAPIUnavailableError,
    SpeakerRecognizer,
    SpeakerRecognizerError,
    get_shared_recognizer,
)

_STRANGER_HASH_RE = re.compile(r"^voice_\d+$")
_STRANGER_SAMPLE_RE = re.compile(r"^[A-Za-z0-9_.-]+\.wav$")

logger = logging.getLogger("hal.speaker_router")

router = APIRouter(tags=["Speaker"])

def get_speaker_recognizer() -> SpeakerRecognizer:
    """The process-wide SpeakerRecognizer shared with the voice pipeline; 503 if unusable."""
    sr = get_shared_recognizer()
    if sr is None:
        raise HTTPException(
            status_code=503,
            detail="Speaker recognizer unavailable",
        )
    return sr


class EnrollSpeakerRequest(BaseModel):
    """Enroll one speaker from 1+ local WAV filepaths."""

    name: str = Field(min_length=1, description="Display name to enroll as.")
    wav_paths: list[str] = Field(
        min_length=1,
        description="Local filepaths of WAV files (any sample rate — will be "
        "normalized to 16kHz mono).",
    )
    telegram_username: Optional[str] = Field(
        default=None,
        description="Optional Telegram @handle (e.g. 'chloe_92'). Merged into "
        "/root/local/users/<name>/metadata.json — same file face-enroll writes.",
    )
    telegram_id: Optional[str] = Field(
        default=None,
        description="Optional numeric Telegram user ID (for DM targeting).",
    )
    origin: Optional[str] = Field(
        default=None,
        description="Channel the audio came from: 'mic' | 'telegram' | "
        "'web' | 'other'. Auto-inferred from presence of telegram_* fields if "
        "omitted. Encoded in the stored sample filename so list_registered "
        "can show which channels contributed. Single token, no underscore — "
        "anything else becomes 'other'.",
    )


class RecordEnrollRequest(BaseModel):
    """Record from the device's own mic (browser mic needs HTTPS), then enroll under ``name``."""

    name: str = Field(min_length=1, description="Display name to enroll as.")
    duration_sec: int = Field(
        default=15, ge=1, le=60,
        description="Recording length in seconds. Capped at 60 to bound ALSA hold.",
    )
    origin: Optional[str] = Field(
        default="web",
        description="Tagged into stored sample filenames so list_registered "
        "can distinguish web-triggered enrolls from telegram / mic ambient. "
        "Must be a single token with no underscore — the tag is recovered from "
        "the filename by splitting on '_'. Anything unrecognised becomes "
        "'other'.",
    )


class UpdateIdentityRequest(BaseModel):
    """Attach / update Telegram identity on an existing voice profile."""

    name: str = Field(min_length=1)
    telegram_username: Optional[str] = None
    telegram_id: Optional[str] = None


class RemoveSpeakerRequest(BaseModel):
    name: str = Field(min_length=1)


class RecognizeSpeakerRequest(BaseModel):
    wav_path: str = Field(min_length=1, description="Local filepath of WAV file.")


class SpeakerMeta(BaseModel):
    """Full metadata — used for enroll / identity confirmation responses."""

    name: str
    display_name: str
    telegram_username: Optional[str] = None
    telegram_id: Optional[str] = None
    has_telegram_identity: bool = False
    enrollment_sources: list[str] = []
    last_enrollment_source: Optional[str] = None
    # Samples the user deliberately enrolled (the permanent anchor tier).
    num_samples: int
    # Auto-collected samples, capped and counted separately from enrolled audio.
    num_extended: int = 0
    embedding_dim: int
    enrolled_at: Optional[str] = None
    updated_at: Optional[str] = None
    sample_files: list[str] = []
    sample_origins: dict[str, str] = {}
    embed_model_version: Optional[str] = None
    extended_files: list[str] = []


class SpeakerListItem(BaseModel):
    """Trimmed public view for /speaker/list (no internal bookkeeping fields)."""

    name: str
    display_name: str
    telegram_username: Optional[str] = None
    telegram_id: Optional[str] = None
    has_telegram_identity: bool = False
    enrollment_sources: list[str] = []
    num_samples: int
    num_extended: int = 0


class EnrollResponse(BaseModel):
    status: str
    meta: SpeakerMeta


class RemoveResponse(BaseModel):
    status: str
    name: str
    removed: bool


class RecognizeResponse(BaseModel):
    name: str
    confidence: float
    match: bool
    display_name: Optional[str] = None
    telegram_username: Optional[str] = None
    telegram_id: Optional[str] = None
    has_telegram_identity: bool = False
    unknown_audio_path: Optional[str] = None
    # Stable unknown-voice cluster label (e.g. "voice_7"); null for a known user.
    voiceprint_hash: Optional[str] = None
    candidates: list[dict[str, Any]] = []
    error: Optional[str] = None


class ListResponse(BaseModel):
    total: int
    enrolled_names: list[str]
    speakers: list[SpeakerListItem]


class StrangerSample(BaseModel):
    filename: str
    size_bytes: int
    mtime: float


class StrangerCluster(BaseModel):
    hash: str
    sample_count: int
    latest_mtime: float
    samples: list[StrangerSample]


class StrangersResponse(BaseModel):
    total: int
    clusters: list[StrangerCluster]


def _validate_paths(paths: list[str]) -> None:
    for p in paths:
        if not p or not Path(p).is_file():
            raise HTTPException(status_code=400, detail=f"wav file not found: {p}")


@router.post("/speaker/enroll", response_model=EnrollResponse)
def speaker_enroll(req: EnrollSpeakerRequest) -> EnrollResponse:
    """Enroll or re-enroll a speaker from 1+ local WAV filepaths.

    Missing paths are skipped; all missing on an enrolled user is an idempotent success.
    """
    logger.info(
        "POST /speaker/enroll name=%r wav_paths=%d tg_user=%r tg_id=%r origin=%r",
        req.name, len(req.wav_paths),
        req.telegram_username or "", req.telegram_id or "", req.origin or "",
    )

    valid_paths: list[str] = []
    skipped: list[str] = []
    for p in req.wav_paths:
        if p and Path(p).is_file():
            valid_paths.append(p)
        else:
            skipped.append(p or "")
    if skipped:
        logger.info(
            "POST /speaker/enroll skipping %d missing path(s): %s",
            len(skipped), skipped,
        )

    sr = get_speaker_recognizer()

    if not valid_paths:
        existing = sr.get_meta(req.name)
        if existing is not None:
            logger.info(
                "POST /speaker/enroll all paths missing but %r already enrolled — "
                "returning existing meta (idempotent)",
                req.name,
            )
            return EnrollResponse(status="ok", meta=SpeakerMeta(**existing))
        raise HTTPException(
            status_code=400,
            detail="all wav paths missing and no existing voice profile",
        )

    try:
        meta = sr.enroll(
            req.name,
            valid_paths,
            source_type="filepath",
            telegram_username=req.telegram_username or "",
            telegram_id=req.telegram_id or "",
            origin=req.origin or "",
        )
    except EmbeddingAPIUnavailableError as e:
        logger.warning("POST /speaker/enroll API unavailable for %r: %s", req.name, e)
        raise HTTPException(
            status_code=503,
            detail=f"embedding service unavailable — please try again: {e}",
        ) from e
    except SpeakerRecognizerError as e:
        logger.warning("POST /speaker/enroll failed for %r: %s", req.name, e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    return EnrollResponse(status="ok", meta=SpeakerMeta(**meta))


# Same ALSA alias voice_service records through, so enroll and recognize match.
_DEVICE_MIC_ALSA = os.environ.get("HAL_AUDIO_INPUT_ALSA") or "plug:device_micro2"

# Mono 16-bit for the embedding model; the recognizer resamples from the WAV header.
_ENROLL_RATE = 16000


def _capture_enroll_wav(wav_path: str, duration: int) -> None:
    """Record `duration` seconds of mono 16-bit audio to `wav_path` (arecord, else PortAudio)."""
    if shutil.which("arecord"):
        _capture_enroll_wav_arecord(wav_path, duration)
    else:
        _capture_enroll_wav_sounddevice(wav_path, duration)


def _capture_enroll_wav_arecord(wav_path: str, duration: int) -> None:
    cmd = [
        "arecord",
        "-D", _DEVICE_MIC_ALSA,
        "-f", "S16_LE",
        "-r", str(_ENROLL_RATE),
        "-c", "1",
        "-d", str(duration),
        "-q",
        wav_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=duration + 10)
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip()
        raise HTTPException(
            status_code=500,
            detail=f"arecord failed: {stderr or proc.returncode}",
        )


def _capture_enroll_wav_sounddevice(wav_path: str, duration: int) -> None:
    """PortAudio capture via the voice pipeline's input device (simulator / hosts without ALSA)."""
    try:
        import sounddevice as sd
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail="no arecord and sounddevice is not installed — cannot record",
        ) from e

    device = getattr(state.voice_service, "_input_device", None)
    rates = [_ENROLL_RATE]
    try:
        native = int(sd.query_devices(device, "input")["default_samplerate"])
        if native != _ENROLL_RATE:
            rates.append(native)
    except Exception as e:
        logger.debug("record-enroll: could not query device %r: %s", device, e)

    last_err: Optional[Exception] = None
    for rate in rates:
        try:
            frames = sd.rec(
                int(duration * rate),
                samplerate=rate,
                channels=1,
                dtype="int16",
                device=device,
            )
            sd.wait()
            break
        except Exception as e:
            last_err = e
            logger.info("record-enroll: capture at %dHz failed: %s", rate, e)
    else:
        raise HTTPException(
            status_code=500,
            detail=f"microphone capture failed: {last_err}",
        )

    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames.tobytes())


@router.post("/speaker/record-enroll", response_model=EnrollResponse)
def speaker_record_enroll(req: RecordEnrollRequest) -> EnrollResponse:
    """Record from the device mic and enroll (pauses voice_service around the capture)."""
    if privacy.mic_locked():
        raise HTTPException(409, "Privacy switch is on -- microphone recording is blocked")
    name = req.name.strip().lower()
    duration = req.duration_sec
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    # Virtual audio has no mic; never record the developer's real mic.
    if state.simulation_audio:
        raise HTTPException(
            status_code=503,
            detail="voice enroll needs a real microphone — restart with SIM_MEDIA=host",
        )

    voice = state.voice_service
    music = state.music_service
    was_running = bool(voice and getattr(voice, "_running", False))
    prev_speaker_muted = state._speaker_muted

    # Release ALSA and mute the speaker so in-flight TTS can't bleed into the recording.
    state._speaker_muted = True
    state._enrolling = True
    if state.tts_service and getattr(state.tts_service, "speaking", False):
        try:
            state.tts_service.stop()
        except Exception as e:
            logger.warning("tts_service.stop failed: %s", e)
    if was_running:
        try:
            voice.stop()
        except Exception as e:
            logger.warning("voice_service.stop failed: %s", e)
    if music and getattr(music, "playing", False):
        try:
            music.stop()
        except Exception as e:
            logger.warning("music_service.stop failed: %s", e)
    # ALSA may need a moment to release; else arecord fails with "Device or resource busy".
    time.sleep(0.4)

    wav_path = f"/tmp/voice-enroll-{name}-{int(time.time() * 1000)}.wav"
    try:
        logger.info("POST /speaker/record-enroll name=%r duration=%ds", name, duration)
        _capture_enroll_wav(wav_path, duration)
        if not Path(wav_path).is_file() or Path(wav_path).stat().st_size < 4096:
            raise HTTPException(status_code=500, detail="recorded file empty/missing")

        sr = get_speaker_recognizer()
        try:
            meta = sr.enroll(
                name,
                [wav_path],
                source_type="filepath",
                origin=req.origin or "web",
            )
        except EmbeddingAPIUnavailableError as e:
            logger.warning("record-enroll embedding API unavailable for %r: %s", name, e)
            raise HTTPException(
                status_code=503,
                detail=f"embedding service unavailable — please try again: {e}",
            ) from e
        except SpeakerRecognizerError as e:
            logger.warning("record-enroll failed for %r: %s", name, e)
            raise HTTPException(status_code=400, detail=str(e)) from e

        return EnrollResponse(status="ok", meta=SpeakerMeta(**meta))
    except HTTPException:
        raise
    except Exception as e:
        # Unhandled errors reach the browser as plain text the web UI can't parse.
        logger.exception("record-enroll crashed for %r", name)
        raise HTTPException(status_code=500, detail=f"record-enroll failed: {e}") from e
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass
        # Only relax the mute gate if we set it.
        state._enrolling = False
        if not prev_speaker_muted and not privacy.speaker_muted:
            state._speaker_muted = False
        # Always restart the listener, even after a failed enroll.
        if was_running and state.voice_service is not None and not privacy.mic_locked():
            try:
                state.voice_service.start()
            except Exception as e:
                logger.warning("voice_service.start failed after record-enroll: %s", e)


@router.post("/speaker/identity", response_model=EnrollResponse)
def speaker_update_identity(req: UpdateIdentityRequest) -> EnrollResponse:
    """Attach / update Telegram identity on an existing voice profile."""
    logger.info(
        "POST /speaker/identity name=%r tg_user=%r tg_id=%r",
        req.name, req.telegram_username or "", req.telegram_id or "",
    )
    sr = get_speaker_recognizer()
    try:
        meta = sr.update_identity(
            req.name,
            telegram_username=req.telegram_username or "",
            telegram_id=req.telegram_id or "",
        )
    except SpeakerRecognizerError as e:
        logger.warning("POST /speaker/identity failed for %r: %s", req.name, e)
        raise HTTPException(status_code=404, detail=str(e)) from e
    return EnrollResponse(status="ok", meta=SpeakerMeta(**meta))


@router.post("/speaker/reset", response_model=RemoveResponse)
def speaker_reset() -> RemoveResponse:
    """Delete every voice profile (shared metadata.json is preserved)."""
    logger.info("POST /speaker/reset — wiping all voice profiles")
    sr = get_speaker_recognizer()
    n = sr.reset_all()
    return RemoveResponse(status="ok", name="*", removed=n > 0)


@router.get("/identity/current-user", tags=["Speaker"])
def identity_current_user():
    """Who the device is with right now across face and voice (face wins).

    Returns user, display, source ("face"/"voice"/"") and age_s since last observed.
    """
    from hal import app_state as identity_state

    user, display, source, age_s = identity_state.resolve_current_user()
    return {
        "user": user,
        "display": display,
        "source": source,
        "age_s": round(age_s, 1),
    }


@router.post("/speaker/current-user/reset", tags=["Speaker"])
def speaker_current_user_reset():
    """Forget the current voice user (presence only; profiles untouched)."""
    from hal import app_state as identity_state

    logger.info("POST /speaker/current-user/reset — forgetting current voice user")
    identity_state.clear_voice_user()
    # Also clear the per-turn recognizer cache, or the next turn keeps the identity.
    voice = getattr(state, "voice_service", None)
    decorator = getattr(voice, "_decorator", None) if voice else None
    if decorator is not None and hasattr(decorator, "forget_identity"):
        decorator.forget_identity()
    return {"status": "ok"}


@router.post("/speaker/remove", response_model=RemoveResponse)
def speaker_remove(req: RemoveSpeakerRequest) -> RemoveResponse:
    """Delete the user's voice folder; 404 if the user has no voice profile."""
    logger.info("POST /speaker/remove name=%r", req.name)
    sr = get_speaker_recognizer()
    removed = sr.remove(req.name)
    if not removed:
        logger.warning("POST /speaker/remove: voice profile not found for %r", req.name)
        raise HTTPException(
            status_code=404,
            detail=f"voice profile not found: {req.name}",
        )
    return RemoveResponse(status="ok", name=req.name, removed=removed)


@router.post("/speaker/recognize", response_model=RecognizeResponse)
def speaker_recognize(req: RecognizeSpeakerRequest) -> RecognizeResponse:
    """Recognize the speaker of a WAV file; unknown includes ``unknown_audio_path`` for enrollment."""
    logger.info("POST /speaker/recognize wav_path=%r", req.wav_path)
    _validate_paths([req.wav_path])
    sr = get_speaker_recognizer()
    try:
        result = sr.recognize(req.wav_path, source_type="filepath")
    except SpeakerRecognizerError as e:
        logger.warning("POST /speaker/recognize failed for %r: %s", req.wav_path, e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    logger.info(
        "POST /speaker/recognize -> name=%r confidence=%.3f match=%s cluster=%s",
        result.get("name"), float(result.get("confidence", 0.0)),
        bool(result.get("match", False)), result.get("voiceprint_hash") or "(none)",
    )
    return RecognizeResponse(**result)


@router.get("/speaker/list", response_model=ListResponse)
def speaker_list() -> ListResponse:
    """List users with a registered voice (public view, see :class:`SpeakerListItem`)."""
    sr = get_speaker_recognizer()
    speakers = sr.list_registered()
    public_items = [
        SpeakerListItem(
            name=s["name"],
            display_name=s.get("display_name") or s["name"],
            telegram_username=s.get("telegram_username") or None,
            telegram_id=s.get("telegram_id") or None,
            has_telegram_identity=bool(s.get("has_telegram_identity", False)),
            enrollment_sources=list(s.get("enrollment_sources", [])),
            num_samples=int(s.get("num_samples", 0)),
            num_extended=int(s.get("num_extended", 0)),
        )
        for s in speakers
    ]
    return ListResponse(
        total=len(public_items),
        enrolled_names=[item.name for item in public_items],
        speakers=public_items,
    )


@router.get("/voice/strangers", response_model=StrangersResponse)
def voice_strangers() -> StrangersResponse:
    """List unknown-voice clusters with their saved WAV samples."""
    logger.info("GET /voice/strangers")
    root = Path(config.SPEAKER_UNKNOWN_AUDIO_DIR)
    if not root.is_dir():
        logger.info("GET /voice/strangers: dir %s does not exist", root)
        return StrangersResponse(total=0, clusters=[])

    clusters: list[StrangerCluster] = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir() or not _STRANGER_HASH_RE.match(sub.name):
            continue
        samples: list[StrangerSample] = []
        for wav in sub.glob("*.wav"):
            try:
                st = wav.stat()
            except OSError:
                continue
            samples.append(StrangerSample(
                filename=wav.name,
                size_bytes=int(st.st_size),
                mtime=float(st.st_mtime),
            ))
        if not samples:
            continue
        samples.sort(key=lambda s: s.mtime, reverse=True)
        clusters.append(StrangerCluster(
            hash=sub.name,
            sample_count=len(samples),
            latest_mtime=samples[0].mtime,
            samples=samples,
        ))
    clusters.sort(key=lambda c: c.latest_mtime, reverse=True)
    logger.info(
        "GET /voice/strangers -> %d cluster(s): %s",
        len(clusters),
        ", ".join(f"{c.hash}({c.sample_count})" for c in clusters) or "(none)",
    )
    return StrangersResponse(total=len(clusters), clusters=clusters)


class StrangerDeleteResponse(BaseModel):
    status: str
    hash: str
    filename: Optional[str] = None
    cluster_removed: bool = False


@router.delete("/voice/strangers/{hash}", response_model=StrangerDeleteResponse)
def voice_stranger_delete_cluster(hash: str) -> StrangerDeleteResponse:
    """Delete a whole unknown-voice cluster (centroid row + on-disk dir)."""
    if not _STRANGER_HASH_RE.match(hash):
        raise HTTPException(status_code=400, detail="invalid cluster hash")
    sr = get_speaker_recognizer()
    removed = sr.drop_stranger_cluster(hash)
    if not removed:
        raise HTTPException(status_code=404, detail=f"cluster not found: {hash}")
    logger.info("DELETE /voice/strangers/%s — removed", hash)
    return StrangerDeleteResponse(status="ok", hash=hash, cluster_removed=True)


@router.delete(
    "/voice/strangers/{hash}/{filename}", response_model=StrangerDeleteResponse,
)
def voice_stranger_delete_sample(hash: str, filename: str) -> StrangerDeleteResponse:
    """Delete a single WAV from a cluster; an emptied cluster drops its centroid."""
    if not _STRANGER_HASH_RE.match(hash) or not _STRANGER_SAMPLE_RE.match(filename):
        raise HTTPException(status_code=400, detail="invalid path")
    root = Path(config.SPEAKER_UNKNOWN_AUDIO_DIR).resolve()
    cluster_dir = (root / hash).resolve()
    try:
        cluster_dir.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid path") from exc
    target = cluster_dir / filename
    if not target.is_file():
        raise HTTPException(status_code=404, detail="sample not found")
    try:
        target.unlink()
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"delete failed: {exc}",
        ) from exc
    cluster_emptied = not any(cluster_dir.glob("*.wav"))
    if cluster_emptied:
        sr = get_speaker_recognizer()
        sr.drop_stranger_cluster(hash)
    logger.info(
        "DELETE /voice/strangers/%s/%s (cluster_emptied=%s)",
        hash, filename, cluster_emptied,
    )
    return StrangerDeleteResponse(
        status="ok",
        hash=hash,
        filename=filename,
        cluster_removed=cluster_emptied,
    )


@router.get("/voice/strangers/audio/{hash}/{filename}")
def voice_stranger_audio(hash: str, filename: str) -> FileResponse:
    """Stream a stranger-cluster WAV; path components are whitelisted against traversal."""
    if not _STRANGER_HASH_RE.match(hash) or not _STRANGER_SAMPLE_RE.match(filename):
        logger.warning(
            "GET /voice/strangers/audio: invalid path hash=%r filename=%r",
            hash, filename,
        )
        raise HTTPException(status_code=400, detail="invalid path")
    root = Path(config.SPEAKER_UNKNOWN_AUDIO_DIR).resolve()
    target = (root / hash / filename).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        logger.warning(
            "GET /voice/strangers/audio: path-traversal rejected target=%s", target,
        )
        raise HTTPException(status_code=400, detail="invalid path") from exc
    if not target.is_file():
        logger.warning("GET /voice/strangers/audio: not found %s", target)
        raise HTTPException(status_code=404, detail="sample not found")
    logger.info("GET /voice/strangers/audio/%s/%s -> %s", hash, filename, target)
    return FileResponse(str(target), media_type="audio/wav", filename=filename)
