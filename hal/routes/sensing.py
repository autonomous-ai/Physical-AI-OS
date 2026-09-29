"""Sensing route handlers -- /sensing, /presence/*, /face/*, /user/* endpoints."""

import base64
import json
import logging
import os
import time
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

import hal.app_state as state
from hal import config
from hal.models import (
    FaceEnrollRequest,
    FaceEnrollResponse,
    FaceOwnersDetailResponse,
    FacePersonDetail,
    FacePhotoRemoveRequest,
    FaceRemoveRequest,
    FaceRemoveResponse,
    FaceResetResponse,
    FaceStatusResponse,
    PresenceResponse,
    SensingResponse,
    StatusResponse,
    UserInfoResponse,
)



logger = logging.getLogger(__name__)


def _notify_user_reconcile(reason: str) -> None:
    """Tell os-server an enrollment directory under USERS_DIR changed (fire-and-forget)."""
    try:
        requests.post(config.OS_USER_RECONCILE_URL, json={"reason": reason}, timeout=2)
    except requests.RequestException as e:
        logger.warning(
            "[user-reconcile] notify failed after %s: %s "
            "(profile will be retired on next os-server start)",
            reason,
            e,
        )


class UserRenameRequest(BaseModel):
    """Rename a user folder under /root/local/users/."""

    old_label: str = Field(min_length=1, max_length=64)
    new_label: str = Field(min_length=1, max_length=64)

FacePerception = None
try:
    from hal.drivers.sensing.perceptions.processors import FacePerception
except ImportError:
    pass

router = APIRouter()


def _require_face_recognizer():
    """Return FacePerception instance or raise 503."""
    if not state.sensing_service or FacePerception is None:
        raise HTTPException(503, "Sensing not available")
    try:
        fr = state.sensing_service._perception_orchestrator._processors.face_recognizer
    except AttributeError:
        fr = None
    if fr is None:
        raise HTTPException(503, "Face recognition not available (no camera)")
    return fr


@router.get("/sensing", response_model=SensingResponse, tags=["Sensing"])
def get_sensing_state():
    """Get perception state."""
    if not state.sensing_service:
        raise HTTPException(503, "Sensing not available")
    return state.sensing_service.to_dict()


def _pose_buckets_dir() -> Path:
    import hal.config as _cfg
    return Path(_cfg.SNAPSHOT_TMP_DIR) / "sensing_pose" / "buckets"


def _find_pose_snapshot_for_ts(ts: int) -> Path | None:
    """Locate a snapshot named `<ts>_<score>.jpg` across all buckets (newest mtime wins)."""
    root: Path = _pose_buckets_dir()
    if not root.is_dir():
        return None
    needle: str = f"{int(ts)}_"
    best: Path | None = None
    best_mtime: float = -1.0
    try:
        for bdir in root.iterdir():
            if not bdir.is_dir():
                continue
            for entry in bdir.iterdir():
                if not entry.is_file() or entry.suffix != ".jpg":
                    continue
                if not entry.name.startswith(needle):
                    continue
                try:
                    mtime: float = entry.stat().st_mtime
                except OSError:
                    continue
                if mtime > best_mtime:
                    best_mtime = mtime
                    best = entry
    except OSError:
        return None
    return best


@router.get("/sensing/pose-snapshot", tags=["Sensing"])
def get_pose_snapshot():
    """Return the most recent annotated pose frame as JPEG."""
    root: Path = _pose_buckets_dir()
    if not root.is_dir():
        raise HTTPException(404, "No pose snapshot yet")
    newest: Path | None = None
    newest_mtime: float = -1.0
    try:
        for bdir in root.iterdir():
            if not bdir.is_dir():
                continue
            for entry in bdir.iterdir():
                if not entry.is_file() or entry.suffix != ".jpg":
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if mtime > newest_mtime:
                    newest_mtime = mtime
                    newest = entry
    except OSError as e:
        raise HTTPException(500, f"scan failed: {e}") from e
    if newest is None:
        raise HTTPException(404, "No pose snapshot yet")
    try:
        data = newest.read_bytes()
    except OSError as e:
        raise HTTPException(500, f"read failed: {e}") from e
    return Response(content=data, media_type="image/jpeg")


@router.get("/sensing/pose-snapshot/{ts}", tags=["Sensing"])
def get_pose_snapshot_at(ts: int):
    """Return the annotated pose frame for `ts` (int unix seconds); 404 once pruned."""
    path: Path | None = _find_pose_snapshot_for_ts(ts)
    if path is None:
        raise HTTPException(404, "Snapshot not found (expired or never written)")
    try:
        data = path.read_bytes()
    except OSError as e:
        raise HTTPException(500, f"read failed: {e}") from e
    return Response(content=data, media_type="image/jpeg")


@router.get("/sensing/pose-bucket/{bucket_id}", tags=["Sensing"])
def get_pose_bucket(bucket_id: str):
    """Return the bucket.json manifest for a kept pose window."""
    if "/" in bucket_id or ".." in bucket_id or not bucket_id.isdigit():
        raise HTTPException(404, "Bucket not found")
    bdir: Path = _pose_buckets_dir() / bucket_id
    manifest: Path = bdir / "bucket.json"
    if not manifest.is_file() or bdir.resolve().parent != _pose_buckets_dir().resolve():
        raise HTTPException(404, "Bucket not found (expired or never kept)")
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HTTPException(500, f"read failed: {e}") from e


@router.get("/sensing/pose-bucket/{bucket_id}/img/{filename}", tags=["Sensing"])
def get_pose_bucket_image(bucket_id: str, filename: str):
    """Serve a single annotated frame from a kept bucket."""
    if "/" in bucket_id or ".." in bucket_id or not bucket_id.isdigit():
        raise HTTPException(404, "Image not found")
    if (
        "/" in filename
        or "\\" in filename
        or ".." in filename
        or not filename.endswith(".jpg")
    ):
        raise HTTPException(404, "Image not found")
    path: Path = _pose_buckets_dir() / bucket_id / filename
    root: Path = _pose_buckets_dir().resolve()
    if not path.is_file() or root not in path.resolve().parents:
        raise HTTPException(404, "Image not found")
    try:
        data = path.read_bytes()
    except OSError as e:
        raise HTTPException(500, f"read failed: {e}") from e
    return Response(content=data, media_type="image/jpeg")


@router.get("/presence", response_model=PresenceResponse, tags=["Presence"])
def get_presence():
    """Get current presence state."""
    if not state.sensing_service:
        return {
            "state": "unknown",
            "enabled": False,
            "seconds_since_motion": 0,
            "idle_timeout": 0,
            "away_timeout": 0,
        }
    return state.sensing_service.presence.to_dict()


@router.post("/presence/enable", response_model=StatusResponse, tags=["Presence"])
def enable_presence():
    """Enable automatic presence-based light control."""
    if not state.sensing_service:
        raise HTTPException(503, "Sensing not available")
    state.sensing_service.presence.enable()
    return {"status": "ok"}


@router.post("/presence/disable", response_model=StatusResponse, tags=["Presence"])
def disable_presence():
    """Disable automatic presence-based light control."""
    if not state.sensing_service:
        raise HTTPException(503, "Sensing not available")
    state.sensing_service.presence.disable()
    return {"status": "ok"}


@router.post("/face/enroll", response_model=FaceEnrollResponse, tags=["Face"])
def face_enroll(req: FaceEnrollRequest):
    """Save a JPEG photo, train embeddings, and persist under users/{label}/."""
    fr = _require_face_recognizer()
    try:
        raw = base64.b64decode(req.image_base64)
    except Exception as exc:
        raise HTTPException(400, "invalid base64") from exc
    if not raw:
        raise HTTPException(400, "empty image")
    tg_username = req.telegram_username or ""
    tg_id = req.telegram_id or ""
    try:
        path = fr.enroll_from_bytes(raw, req.label, tg_username, tg_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    norm = FacePerception.normalize_label(req.label)
    return FaceEnrollResponse(
        status="ok",
        label=norm,
        telegram_username=tg_username or None,
        telegram_id=tg_id or None,
        photo_path=path,
        enrolled_count=fr.enrolled_count(),
    )


@router.get("/face/status", response_model=FaceStatusResponse, tags=["Face"])
def face_status():
    """List enrolled persons and count."""
    fr = _require_face_recognizer()
    return FaceStatusResponse(
        enrolled_count=fr.enrolled_count(),
        enrolled_names=fr.enrolled_names(),
    )


_IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}

# Shared log bucket for an unidentified speaker; never an enrolled person.
SHARED_USER_BUCKET = "unknown"


def _has_enrollment_evidence(d: Path) -> bool:
    """True if a user dir holds a face photo, voice sample or metadata.json (#425)."""
    if (d / "metadata.json").is_file():
        return True
    if any(f.is_file() and f.suffix.lower() in _IMG_EXTS for f in d.iterdir()):
        return True
    voice_dir = d / "voice"
    return voice_dir.is_dir() and any(f.is_file() for f in voice_dir.iterdir())


@router.get("/face/owners", response_model=FaceOwnersDetailResponse, tags=["Face"])
def face_owners_detail():
    """List enrolled persons with photo filenames (the "unknown" bucket is listed, not counted)."""
    _require_face_recognizer()
    from hal.drivers.sensing.perceptions.processors.facerecognizer_v2 import USERS_DIR

    persons: list[FacePersonDetail] = []
    enrolled_count = 0
    if USERS_DIR.is_dir():
        for d in sorted(USERS_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            enrolled = _has_enrollment_evidence(d)
            if not enrolled and d.name != SHARED_USER_BUCKET:
                logger.debug("[face/owners] skipping log-only dir with no enrollment evidence: %s", d.name)
                continue
            if enrolled and d.name != SHARED_USER_BUCKET:
                enrolled_count += 1
            photos = sorted(f.name for f in d.iterdir() if f.is_file() and f.suffix.lower() in _IMG_EXTS)
            other_files = sorted(f.name for f in d.iterdir() if f.is_file() and f.suffix.lower() not in _IMG_EXTS)
            mood_dir = d / "mood"
            mood_days = sorted(f.stem for f in mood_dir.iterdir() if f.suffix == ".jsonl") if mood_dir.is_dir() else []
            wb_dir = d / "wellbeing"
            wellbeing_days = sorted(f.stem for f in wb_dir.iterdir() if f.suffix == ".jsonl") if wb_dir.is_dir() else []
            music_sugg_dir = d / "music-suggestions"
            music_suggestion_days = sorted(f.stem for f in music_sugg_dir.iterdir() if f.suffix == ".jsonl") if music_sugg_dir.is_dir() else []
            posture_dir = d / "posture"
            posture_days = sorted(f.stem for f in posture_dir.iterdir() if f.suffix == ".jsonl") if posture_dir.is_dir() else []
            audio_hist_dir = d / "audio_history"
            audio_history_days = sorted(f.stem for f in audio_hist_dir.iterdir() if f.suffix == ".jsonl") if audio_hist_dir.is_dir() else []
            voice_dir = d / "voice"
            voice_samples = sorted(
                f.name for f in voice_dir.iterdir() if f.is_file()
            ) if voice_dir.is_dir() else []
            habit_patterns = (d / "habit" / "patterns.json").is_file()
            meta = FacePerception._read_metadata(d)
            persons.append(
                FacePersonDetail(
                    label=d.name,
                    telegram_username=meta.get("telegram_username"),
                    telegram_id=meta.get("telegram_id"),
                    photo_count=len(photos),
                    photos=photos,
                    mood_days=mood_days,
                    wellbeing_days=wellbeing_days,
                    music_suggestion_days=music_suggestion_days,
                    posture_days=posture_days,
                    audio_history_days=audio_history_days,
                    voice_samples=voice_samples,
                    habit_patterns=habit_patterns,
                    files=other_files,
                )
            )
    return FaceOwnersDetailResponse(enrolled_count=enrolled_count, persons=persons)


@router.get("/face/photo/{label}/{filename}", tags=["Face"])
def face_photo(label: str, filename: str):
    """Serve an owner photo as JPEG."""
    from hal.drivers.sensing.perceptions.processors.facerecognizer_v2 import USERS_DIR

    norm = FacePerception.normalize_label(label)
    path = (USERS_DIR / norm / filename).resolve()
    try:
        path.relative_to(USERS_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "invalid path") from None
    if not path.is_file():
        raise HTTPException(404, "photo not found")
    return Response(content=path.read_bytes(), media_type="image/jpeg")


@router.get("/face/file/{label}/{filepath:path}", tags=["Face"])
def face_file(label: str, filepath: str):
    """Serve any text file from a user's directory."""
    from hal.drivers.sensing.perceptions.processors.facerecognizer_v2 import USERS_DIR
    from hal.drivers.voice.music_service import canonicalize_person

    norm = canonicalize_person(label)
    path = (USERS_DIR / norm / filepath).resolve()
    try:
        path.relative_to(USERS_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "invalid path") from None
    if not path.is_file():
        raise HTTPException(404, "file not found")
    mime_map = {".json": "application/json", ".jsonl": "application/json", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".webm": "audio/webm", ".npy": "application/octet-stream"}
    mime = mime_map.get(path.suffix.lower(), "text/plain")
    return Response(content=path.read_bytes(), media_type=mime)


@router.post("/face/remove", response_model=FaceRemoveResponse, tags=["Face"])
def face_remove(req: FaceRemoveRequest):
    """Remove one person's saved photos and re-train from disk."""
    fr = _require_face_recognizer()
    norm = FacePerception.normalize_label(req.label)
    if not fr.remove_person(req.label):
        raise HTTPException(404, "person not found")
    _notify_user_reconcile(f"face/remove:{norm}")
    return FaceRemoveResponse(
        status="ok",
        label=norm,
        enrolled_count=fr.enrolled_count(),
    )


@router.post("/face/photo/remove", response_model=StatusResponse, tags=["Face"])
def face_photo_remove(req: FacePhotoRemoveRequest):
    """Remove a single photo from a person and re-train."""
    fr = _require_face_recognizer()
    if not fr.remove_photo(req.label, req.filename):
        raise HTTPException(404, "photo not found")
    return {"status": "ok"}


@router.post("/face/reset", response_model=FaceResetResponse, tags=["Face"])
def face_reset():
    """Clear all enrolled embeddings and delete all photos on disk."""
    fr = _require_face_recognizer()
    fr.reset_enrolled()
    _notify_user_reconcile("face/reset")
    return FaceResetResponse(status="ok", enrolled_count=0)


@router.post("/users/rename", response_model=StatusResponse, tags=["User"])
def user_rename(req: UserRenameRequest):
    """Rename a per-user folder (single fs rename); the new label must be valid and unused."""
    from hal.drivers.sensing.perceptions.processors.facerecognizer_v2 import (
        FacePerception,
        USERS_DIR,
    )

    old = FacePerception.normalize_label(req.old_label)
    new = FacePerception.normalize_label(req.new_label)
    if not old or not new:
        raise HTTPException(400, "label must contain at least one valid character")
    if old == new:
        return {"status": "ok"}
    # "unknown" is a sentinel label across face / voice / log paths.
    if old == "unknown" or new == "unknown":
        raise HTTPException(400, "'unknown' is reserved and cannot be renamed")

    src = USERS_DIR / old
    dst = USERS_DIR / new
    if not src.is_dir():
        raise HTTPException(404, f"user folder not found: {old}")
    if dst.exists():
        raise HTTPException(409, f"target name already exists: {new}")

    try:
        os.rename(src, dst)
    except OSError as e:
        raise HTTPException(500, f"rename failed: {e}") from e

    # metadata.json mirrors the label as `name` / `display_name`; update both copies.
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for meta_path in (dst / "metadata.json", dst / "voice" / "metadata.json"):
        if not meta_path.is_file():
            continue
        try:
            data = json.loads(meta_path.read_text()) or {}
        except (json.JSONDecodeError, OSError):
            continue
        changed = False
        if "name" in data and data.get("name") != new:
            data["name"] = new
            changed = True
        if data.get("display_name") != new:
            data["display_name"] = new
            changed = True
        if changed:
            data["updated_at"] = now_iso
            try:
                meta_path.write_text(json.dumps(data, indent=2))
            except OSError:
                pass

    # Re-key the speaker registry so /speaker/list reflects the rename immediately.
    registry_path = USERS_DIR / ".voice_registry.json"
    if registry_path.is_file():
        try:
            reg = json.loads(registry_path.read_text())
            if old in reg:
                entry = reg.pop(old)
                entry["display_name"] = new
                entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                reg[new] = entry
                registry_path.write_text(json.dumps(reg, indent=2))
        except (json.JSONDecodeError, OSError):
            pass

    # Touch a sentinel so the recognizer's 2s poller reloads even if dir mtime is unchanged.
    try:
        os.utime(USERS_DIR, None)
    except OSError:
        pass

    _notify_user_reconcile(f"users/rename:{old}->{new}")

    return {"status": "ok"}


@router.get("/face/stranger-stats", tags=["Face"])
def face_stranger_stats():
    """Return visit counts for all tracked stranger IDs."""
    fr = _require_face_recognizer()
    return fr.stranger_stats()


@router.get("/face/cooldowns", tags=["Face"])
def face_cooldowns():
    """Return current cooldown state for all tracked persons."""
    fr = _require_face_recognizer()
    return fr.cooldown_state()


@router.get("/face/current-user", tags=["Face"])
def face_current_user():
    """Who HAL considers in front of the device: newest friend, else "unknown", else ""."""
    fr = _require_face_recognizer()
    return {"current_user": fr.current_user()}


@router.post("/face/cooldowns/reset", tags=["Face"])
def face_cooldowns_reset():
    """Reset all face recognition cooldown timers."""
    fr = _require_face_recognizer()
    fr.reset_cooldowns()
    return {"status": "ok"}


def _resolve_user_dir(name: str) -> tuple[str, Path]:
    """Resolve user name and directory."""
    from hal.drivers.sensing.perceptions.processors.facerecognizer_v2 import USERS_DIR, FacePerception as FR

    norm = FR.normalize_label(name) if name else state.DEFAULT_USER
    user_dir = USERS_DIR / norm
    user_dir.mkdir(parents=True, exist_ok=True)
    return norm, user_dir


@router.get("/user/info", response_model=UserInfoResponse, tags=["User"])
def user_info(name: str = ""):
    """Get basic user info: name, is_friend, telegram identity."""
    from hal.drivers.sensing.perceptions.processors.facerecognizer_v2 import FacePerception as FR

    actual_name = name or state.DEFAULT_USER
    norm, user_dir = _resolve_user_dir(actual_name)
    meta = FR._read_metadata(user_dir)
    img_exts = {".jpg", ".jpeg", ".png", ".bmp"}
    is_friend = any(f.suffix.lower() in img_exts for f in user_dir.iterdir() if f.is_file())

    return UserInfoResponse(
        name=norm,
        is_friend=is_friend,
        telegram_id=meta.get("telegram_id"),
        telegram_username=meta.get("telegram_username"),
    )
