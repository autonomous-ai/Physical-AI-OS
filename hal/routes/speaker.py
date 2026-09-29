"""FastAPI router for speaker (voice-identity) recognition.

Routes accept local WAV filepaths only (no base64 — keeps the HTTP surface
minimal; internally the service handles both). Mounted by
:mod:`hal.server` on application startup.

Enrolled users live under ``/speaker/*``; unknown-voice-cluster browsing
lives under ``/voice/*`` (input side — avoids semantic clash with the
loudspeaker hardware). Both sets live in this file since they're two
halves of the same speaker recognition surface.

Routes:
    POST   /speaker/enroll                       — enroll / re-enroll a user from WAV paths
    POST   /speaker/identity                     — attach Telegram identity to existing profile
    POST   /speaker/reset                        — wipe all voice profiles
    POST   /speaker/remove                       — delete a user's voice folder
    POST   /speaker/recognize                    — identify the speaker of a WAV file
    GET    /speaker/list                         — list users with registered voice
    GET    /voice/strangers                      — list unknown-voice clusters + samples
    GET    /voice/strangers/audio/{hash}/{file}  — stream a cluster sample WAV
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
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
    """Lazy accessor — raises 503 if unusable.

    Returns the ONE process-wide instance shared with the voice pipeline (see
    ``get_shared_recognizer``), so commit locks / migration state / stranger
    clusters stay unified against the single on-disk store. The HTTP layer maps
    an unavailable recognizer to a 503 here (the shared accessor itself returns
    None so non-HTTP callers can degrade instead).
    """
    sr = get_shared_recognizer()
    if sr is None:
        raise HTTPException(
            status_code=503,
            detail="Speaker recognizer unavailable",
        )
    return sr


# ----------------------------------------------------------------- Pydantic


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
    """Capture audio from the device's mic, then enroll under ``name``.

    Web Setup / Edit pages can't reach the browser microphone without HTTPS
    (insecure-context restriction on getUserMedia), so the device records its
    own ALSA mic instead. The user stands near the device and reads the
    prompted text while ``arecord`` writes a 16kHz mono WAV which is then
    fed straight to ``SpeakerRecognizer.enroll``.
    """

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
    # Auto-collected samples: unknown-cluster audio claimed at enroll plus
    # confidently-recognized later turns. Capped and diversity-pruned, and
    # counted separately so it can never be mistaken for enrolled audio.
    num_extended: int = 0
    embedding_dim: int
    enrolled_at: Optional[str] = None
    updated_at: Optional[str] = None
    sample_files: list[str] = []
    sample_origins: dict[str, str] = {}
    embed_model_version: Optional[str] = None
    extended_files: list[str] = []


class SpeakerListItem(BaseModel):
    """Trimmed public view for /speaker/list — identity-focused, no internals.

    Drops internal bookkeeping fields (embedding_dim, sample_files,
    sample_origins, enrolled_at, updated_at, last_enrollment_source) — those
    belong in log/debug output, not the public API response.
    """

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
    # Stable cluster label for unknown voices (e.g. "voice_7"). Null when the
    # speaker matched a known user — their name already serves as identity.
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


# ------------------------------------------------------------------ helpers


def _validate_paths(paths: list[str]) -> None:
    for p in paths:
        if not p or not Path(p).is_file():
            raise HTTPException(status_code=400, detail=f"wav file not found: {p}")


# ------------------------------------------------------------------- routes


@router.post("/speaker/enroll", response_model=EnrollResponse)
def speaker_enroll(req: EnrollSpeakerRequest) -> EnrollResponse:
    """Enroll or re-enroll a speaker from 1+ local WAV filepaths.

    New samples are appended to the user's voice folder and the embedding is
    recomputed from all samples in the folder (old + new).

    Missing paths are tolerated: a successful enroll deletes the consumed
    stranger cluster (rmtree), so a retry by OpenClaw can legitimately point
    at a path that no longer exists. Missing paths are skipped with a log
    line. If every path is missing AND the user is already enrolled, return
    the existing meta as an idempotent success rather than a 400.
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


# ALSA capture device for the voice mic — the SAME alias voice_service records
# through (HAL_AUDIO_INPUT_ALSA), so enroll and recognize see identical
# acoustics. The alias is defined per device in /etc/asound.conf as a `plug:`
# route over that device's USB mic card. Read from env (not hardcoded) so it
# can't drift from the runtime; device-agnostic fallback for dev/test.
_DEVICE_MIC_ALSA = os.environ.get("HAL_AUDIO_INPUT_ALSA") or "plug:device_micro2"

# Mono 16-bit is what the embedding model wants; the rate is negotiable because
# the recognizer resamples from the WAV header.
_ENROLL_RATE = 16000


def _capture_enroll_wav(wav_path: str, duration: int) -> None:
    """Record `duration` seconds of mono 16-bit audio to `wav_path`.

    arecord is the board path. A laptop has no ALSA at all, so fall back to
    PortAudio through the same input device the voice pipeline records with —
    without it the call died on a missing binary and FastAPI answered a
    plain-text 500 the web UI could not parse into an error message.
    """
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
    """PortAudio capture — the simulator and any host without ALSA.

    Uses the voice pipeline's own input device so enroll and recognize hear the
    same mic. If that device refuses 16 kHz we record at its native rate: the
    recognizer reads the rate off the WAV header and resamples.
    """
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
    """Record from the device mic and enroll the captured audio.

    Coordinates with the running voice_service to release ALSA cleanly:
    pause the listener thread, run arecord, restart the listener, then
    enroll. ``voice_service`` is *paused* (not torn down) so we don't lose
    the configured tts/stt credentials — restart needs no extra args.
    """
    if state._mic_muted or privacy.mic_locked():
        raise HTTPException(409, "Privacy switch is on -- microphone recording is blocked")
    name = req.name.strip().lower()
    duration = req.duration_sec
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    # Virtual audio has no microphone to enroll from, and silently recording the
    # developer's real mic here would contradict what SIM_MEDIA=virtual promises.
    if state.simulation_audio:
        raise HTTPException(
            status_code=503,
            detail="voice enroll needs a real microphone — restart with SIM_MEDIA=host",
        )

    voice = state.voice_service
    music = state.music_service
    was_running = bool(voice and getattr(voice, "_running", False))
    prev_speaker_muted = state._speaker_muted

    # Step 1: release ALSA + suppress speaker output. The mic listener
    # holds the capture device, music can hold the playback device, and
    # most importantly: a TTS reply from a turn that was already in flight
    # before the user clicked "enroll" would otherwise play out of the
    # speaker mid-recording and bleed into the captured WAV (room
    # acoustics → embedding contamination). Setting _speaker_muted blocks
    # TTS, music, and backchannel paths via the existing speaker-gate
    # checks; we restore in finally below.
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
    # ALSA may need a moment to fully release on slow hardware — without
    # this, arecord can fail with "Device or resource busy".
    time.sleep(0.4)

    wav_path = None
    try:
        # Create privately and independently of the user-supplied display name.
        fd, wav_path = tempfile.mkstemp(prefix="voice-enroll-", suffix=".wav")
        os.close(fd)
        logger.info("POST /speaker/record-enroll name=%r duration=%ds", name, duration)
        if state._mic_muted or privacy.mic_locked():
            raise HTTPException(409, "Microphone is muted")
        _capture_enroll_wav(wav_path, duration)
        if state._mic_muted or privacy.mic_locked():
            raise HTTPException(409, "Microphone was muted during enrollment")
        if not Path(wav_path).is_file() or Path(wav_path).stat().st_size < 4096:
            raise HTTPException(status_code=500, detail="recorded file empty/missing")

        # Step 2: enroll. SpeakerRecognizer.enroll copies the WAV into the
        # user's voice/ folder — we can clean up our /tmp original after.
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
        # Anything unhandled here reaches the browser as Starlette's plain-text
        # "Internal Server Error", which the web UI parses as JSON and reports
        # as a syntax error instead of the real cause.
        logger.exception("record-enroll crashed for %r", name)
        raise HTTPException(status_code=500, detail=f"record-enroll failed: {e}") from e
    finally:
        # Belt-and-braces cleanup of the temp WAV (sr.enroll already copied it).
        if wav_path is not None:
            try:
                os.remove(wav_path)
            except OSError:
                pass
        # Restore speaker mute state — only relax the gate if we set it.
        # Don't overwrite a pre-existing mute the user/scene may have asked for.
        state._enrolling = False
        if not prev_speaker_muted and not privacy.speaker_muted:
            state._speaker_muted = False
        # Restore only if the user has not muted the microphone during capture.
        if was_running and state.voice_service is not None and not state._mic_muted and not privacy.mic_locked():
            try:
                state.voice_service.start()
            except Exception as e:
                logger.warning("voice_service.start failed after record-enroll: %s", e)


@router.post("/speaker/identity", response_model=EnrollResponse)
def speaker_update_identity(req: UpdateIdentityRequest) -> EnrollResponse:
    """Attach / update Telegram identity on an existing voice profile.

    Use when a user was first enrolled via mic (no Telegram info) and later
    introduces themselves via Telegram — we can link the two without
    re-uploading audio.
    """
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
    """Delete every voice profile (mirrors /face/reset).

    Shared identity (``metadata.json``) is preserved — face / mood /
    wellbeing still depend on it.
    """
    logger.info("POST /speaker/reset — wiping all voice profiles")
    sr = get_speaker_recognizer()
    n = sr.reset_all()
    return RemoveResponse(status="ok", name="*", removed=n > 0)


@router.get("/identity/current-user", tags=["Speaker"])
def identity_current_user():
    """Return who the device is with right now, across BOTH modalities.

    ``/face/current-user`` answers only "who does the camera see", which is
    empty on a device with no camera and on any turn where nobody is in frame —
    even immediately after speaker-ID recognized an enrolled user. This
    endpoint applies the device-wide rule instead (face wins; the voice speaker
    fills the slot only when face has nobody), so callers get an identity
    whenever either modality has one.

    - ``user`` — normalized label ("long"), the same shape ``/face/current-user``
      returns and the same slug the per-user folders use. Empty when neither
      modality has anyone.
    - ``display`` — human spelling ("Long") for display; falls back to ``user``.
    - ``source`` — "face", "voice", or "" when nobody is known.
    - ``age_s`` — seconds since this identity was last positively observed: last
      seen in frame (face) or last spoken (voice). NOT zero just because a face
      answered — ``current_user()`` keeps returning a friend for
      FACE_OWNER_FORGET_S (1h) and a stranger for FACE_STRANGER_FORGET_S (30m)
      after they leave, so this is what separates "standing here" from "left 50
      minutes ago". 0.0 only when nobody is known.
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
    """Forget the current voice user — the voice twin of /face/cooldowns/reset.

    Presence state only: enrolled voice profiles are untouched (that is
    ``/speaker/reset``). Use it to emulate "a brand new day, the device has
    not heard anyone yet" without waiting out ``VOICE_USER_FORGET_S``. Pair it
    with ``/face/cooldowns/reset`` to blank both modalities at once.
    """
    from hal import app_state as identity_state

    logger.info("POST /speaker/current-user/reset — forgetting current voice user")
    identity_state.clear_voice_user()
    # The per-turn recognizer cache is the same presence state one layer down:
    # leaving it would keep handing the next turn the identity this endpoint was
    # just asked to forget (see SpeakerDecorator._cached_identity).
    voice = getattr(state, "voice_service", None)
    decorator = getattr(voice, "_decorator", None) if voice else None
    if decorator is not None and hasattr(decorator, "forget_identity"):
        decorator.forget_identity()
    return {"status": "ok"}


@router.post("/speaker/remove", response_model=RemoveResponse)
def speaker_remove(req: RemoveSpeakerRequest) -> RemoveResponse:
    """Delete the user's voice folder (embedding + samples + metadata).

    Returns 404 if the user has no voice profile — mirrors ``/face/remove``
    behaviour so callers don't silently no-op on a typo. Other per-user data
    (face photos, mood, wellbeing, ...) is preserved regardless.
    """
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
    """Recognize the speaker of a single WAV file.

    Returns ``{name: "unknown"}`` when no registered speaker exceeds the match
    threshold, along with ``unknown_audio_path`` so the skill can reuse that
    path for a later enrollment call.
    """
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
    """List users with a registered voice — public identity-focused view.

    Internal bookkeeping (sample filenames, embedding dim, timestamps) is
    computed by the service but intentionally not exposed here — see
    :class:`SpeakerListItem` for the trimmed schema.
    """
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


# ----------------------------------------- unknown-voice cluster browsing


@router.get("/voice/strangers", response_model=StrangersResponse)
def voice_strangers() -> StrangersResponse:
    """List unknown-voice clusters with their saved WAV samples.

    Scans the per-cluster sub-dirs the speaker service writes under
    ``SPEAKER_UNKNOWN_AUDIO_DIR/voice_<N>/`` so the web UI can play back
    clips the device has grouped as "same unknown voice" before deciding to
    enroll them as a known speaker.
    """
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
    """Delete a whole unknown-voice cluster — centroid row + on-disk dir.

    Used from the web card when the operator decides a cluster is noise or
    belongs to someone they don't want tracked any further.
    """
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
    """Delete a single WAV from a cluster.

    If the cluster becomes empty after deletion, auto-drop the centroid so
    the .npy state stays in sync with what the web list can show (an empty
    cluster dir is filtered out of ``GET /voice/strangers``, which would
    otherwise orphan the centroid forever).
    """
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
    """Stream a stranger-cluster WAV by cluster hash + filename.

    Path components are whitelisted (``voice_<digits>`` / ``<safe>.wav``) and
    the resolved file must sit inside ``SPEAKER_UNKNOWN_AUDIO_DIR`` — blocks
    path-traversal attempts like ``../../etc/passwd``.
    """
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
