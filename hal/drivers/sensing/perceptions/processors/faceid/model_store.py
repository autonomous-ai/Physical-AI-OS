"""Face-model paths + download-on-first-use from cloud storage."""

import logging
import os
import shutil
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_FACE_MODEL_DIR: str = os.environ.get("HAL_FACE_MODEL_PATH", "/root/local/models")

_SCRFD_MODEL_PATH: str = os.environ.get(
    "HAL_FACE_SCRFD_MODEL_PATH", os.path.join(_FACE_MODEL_DIR, "scrfd_2.5g_fp32.onnx")
)
_EDGEFACE_MODEL_PATH: str = os.environ.get(
    "HAL_FACE_EDGEFACE_MODEL_PATH",
    os.path.join(_FACE_MODEL_DIR, "edgeface_s_gamma_05_opt.onnx"),
)
# MediaPipe FaceMesh landmark regressor exported to ONNX (replaces the pip
# `mediapipe` dependency, which cannot be installed on the target device).
_LANDMARK_MODEL_PATH: str = os.environ.get(
    "HAL_FACE_LANDMARK_MODEL_PATH",
    os.path.join(_FACE_MODEL_DIR, "MediaPipeFaceLandmarkDetector.onnx"),
)

# Face-presence probability above which the ONNX landmarks are trusted for alignment;
# below it the detection is dropped (no SCRFD keypoint fallback), so this doubles as a
# false-alarm gate. 0.99, not 0.6: the model's score SATURATES.
_LANDMARK_CONF_THRESHOLD: float = float(
    os.environ.get("HAL_FACE_LANDMARK_CONF_THRESHOLD", "0.99")
)

_CDN_BASE: str = os.environ.get(
    "HAL_FACE_MODEL_CDN_BASE", "https://storage.googleapis.com/autonomous-models"
)

_CDN_OBJECTS: dict[str, str] = {
    "scrfd_2.5g_fp32.onnx": "onnx_models/scrfd_2.5g_fp32.onnx",
    "edgeface_s_gamma_05_opt.onnx": "onnx_models/edgeface_s_gamma_05_opt.onnx",
    "MediaPipeFaceLandmarkDetector.onnx": "onnx_models/MediaPipeFaceLandmarkDetector.onnx",
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
    mid-download never leaves a truncated file that a later run would mistake for a
    complete cached model.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path = dest.with_suffix(dest.suffix + f".part.{os.getpid()}")
    logger.info("[face-v2] downloading %s -> %s", url, dest)
    try:
        with urllib.request.urlopen(url) as response, open(tmp, "wb") as out_file:
            shutil.copyfileobj(response, out_file)
        tmp.replace(dest)
        logger.info("[face-v2] download complete: %s", dest)
    except Exception as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise RuntimeError(f"Failed to download {url}: {exc}") from exc


def ensure_downloaded(local_path: Path, remote: str | None) -> Path:
    """Ensure a model file exists at ``local_path``, downloading if needed."""
    if local_path.exists():
        return local_path
    if remote is None:
        raise FileNotFoundError(
            f"Face model not found: {local_path}. No download URL is known for "
            f"'{local_path.name}' — set the matching HAL_FACE_*_MODEL_PATH env var "
            "to a pre-provisioned file."
        )
    _download_url(remote, local_path)
    return local_path


def ensure_face_models(*model_paths: str) -> None:
    """Ensure each given model path exists locally, fetching from the weights
    bucket on first use.
    """
    for raw in model_paths:
        path = Path(raw)
        if path.exists():
            continue
        ensure_downloaded(path, _remote_for(path))
