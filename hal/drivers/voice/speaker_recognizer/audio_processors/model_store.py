"""STOI model path + download-on-first-use from cloud storage."""

import logging
import os
import shutil
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_CDN_BASE: str = os.environ.get(
    "HAL_SPEAKER_MODEL_CDN_BASE", "https://storage.googleapis.com/autonomous-models"
)

_CDN_OBJECTS: dict[str, str] = {
    "squimm_stoi.onnx": "onnx_models/squimm_stoi.onnx",
}


def _remote_for(local_path: Path) -> str | None:
    """Full CDN URL for a model, resolved by its basename (or None if unknown)."""
    obj = _CDN_OBJECTS.get(local_path.name)
    if obj is None:
        return None
    return f"{_CDN_BASE.rstrip('/')}/{obj}"


def _download_url(url: str, dest: Path) -> None:
    """Atomic download from a direct URL.

    Downloads to a per-PID temp file then atomically renames into place, so a crash/kill
    mid-download never leaves a truncated file a later run would mistake for a complete
    cached model.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path = dest.with_suffix(dest.suffix + f".part.{os.getpid()}")
    logger.info("[stoi] downloading %s -> %s", url, dest)
    try:
        with urllib.request.urlopen(url) as response, open(tmp, "wb") as out_file:
            shutil.copyfileobj(response, out_file)
        tmp.replace(dest)
        logger.info("[stoi] download complete: %s", dest)
    except Exception as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise RuntimeError(f"Failed to download {url}: {exc}") from exc


def ensure_stoi_model(model_path: str) -> str:
    """Ensure the STOI model exists at ``model_path``, downloading if needed."""
    path = Path(model_path)
    if path.exists():
        return str(path)
    remote = _remote_for(path)
    if remote is None:
        raise FileNotFoundError(
            f"STOI model not found: {path}. No download URL is known for "
            f"'{path.name}' — set HAL_SPEAKER_PROC_STOI_MODEL_PATH to a "
            "pre-provisioned file, or add the basename to _CDN_OBJECTS."
        )
    _download_url(remote, path)
    return str(path)
