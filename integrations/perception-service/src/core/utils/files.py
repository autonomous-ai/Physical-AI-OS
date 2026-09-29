"""Model file utilities — ensure models are downloaded before use."""

import logging
import os
import shutil
import urllib.request
from pathlib import Path

from core.enums.files import ModelEnum

logger: logging.Logger = logging.getLogger(__name__)

# Model -> object path under settings.cdn_base; local cache filename is the basename.
# Keep in sync with docs/configuration.md ("Model downloading").
CDN_PATHS: dict[ModelEnum, str] = {
    # Action recognition
    ModelEnum.X3D_ONNX: "onnx_models/x3d_m_16x5x1_int8.onnx",
    ModelEnum.VIDEOMAE_ONNX: "onnx_models/videomae_fp32.onnx",
    ModelEnum.UNIFORMERV2_ONNX: "onnx_models/uniformerv2-l-224-k400_fp32.onnx",
    ModelEnum.UNIFORMERV2_PTH: "pytorch_models/uniformerv2-l-224-k400.pth",
    # Facial emotion (FER)
    ModelEnum.POSTERV2_ONNX: "onnx_models/posterv2_7cls.onnx",
    ModelEnum.POSTERV2_PTH: "pytorch_models/posterv2_7cls.pth",
    ModelEnum.EMONET_8_ONNX: "onnx_models/emonet_8.onnx",
    ModelEnum.EMONET_8_PTH: "pytorch_models/emonet_8.pth",
    ModelEnum.EMONET_5_ONNX: "onnx_models/emonet_5.onnx",
    ModelEnum.EMONET_5_PTH: "pytorch_models/emonet_5.pth",
    ModelEnum.EMOAFFECTNET_ONNX: "onnx_models/emoaffectnet_resnet50.onnx",
    ModelEnum.EMOAFFECTNET_PTH: "pytorch_models/emoaffectnet_resnet50.pth",
    # Audio emotion (SER)
    ModelEnum.EMOTION2VEC_ONNX: "onnx_models/emotion2vec.onnx",
    # Pose 2D
    ModelEnum.RTMPOSE_M_ONNX: "onnx_models/rtmpose-m.onnx",
    # Pose 3D
    ModelEnum.TCPFORMER_H36M_243_ONNX: "onnx_models/tcpformer_h36m_243.onnx",
    ModelEnum.TCPFORMER_H36M_243_PTH: "pytorch_models/TCPFormer_h36m_243_379.pth.tr",
    # Audio embedder (WeSpeaker)
    ModelEnum.WESPEAKER_RESNET34: "onnx_models/wespeaker_resnet34.onnx",
    ModelEnum.WESPEAKER_RESNET293: "onnx_models/voxceleb_resnet293_LM.onnx",
    ModelEnum.WESPEAKER_ECAPA_TDNN_1024: "onnx_models/wespeaker_ecapa_tdnn1024.onnx",
    ModelEnum.WESPEAKER_CAMPPLUS: "onnx_models/wespeaker_campplus.onnx",
    # Face detection
    ModelEnum.YUNET_ONNX: "onnx_models/face_detection_yunet_2023mar.onnx",
    # Person detection
    ModelEnum.YOLO_PERSON_ONNX: "onnx_models/yolo12x_raw.onnx",
    ModelEnum.YOLO_PERSON_NMS_ONNX: "onnx_models/yolo12x.onnx",
    ModelEnum.YOLO_PERSON_PTH: "pytorch_models/yolo12x.pt",
    # Object detection
    ModelEnum.YOLO_WORLD_ONNX: "onnx_models/yolov8x-worldv2_raw.onnx",
    ModelEnum.YOLO_WORLD_NMS_ONNX: "onnx_models/yolov8x-worldv2.onnx",
    ModelEnum.YOLO_WORLD_PTH: "pytorch_models/yolov8x-worldv2.pt",
    ModelEnum.OWLV2_ONNX: "onnx_models/owlv2_raw.onnx",
    ModelEnum.OWLV2_NMS_ONNX: "onnx_models/owlv2.onnx",
}


def get_models_cache_dir() -> Path:
    """Get models cache dir from settings."""
    from config import settings

    return settings.model_cache_dir


def get_default_cdn_url(model: ModelEnum) -> str | None:
    """Build the full CDN URL for a given model from settings.cdn_base + CDN_PATHS."""
    path: str | None = CDN_PATHS.get(model)
    if path is None:
        return None
    from config import settings

    return f"{settings.cdn_base.rstrip('/')}/{path}"


def get_default_model_path(model: ModelEnum) -> Path | None:
    """Get the default local cache path for a model."""
    path: str | None = CDN_PATHS.get(model)
    if path is None:
        return None
    filename: str = Path(path).name
    return get_models_cache_dir() / filename


def ensure_downloaded(local_path: Path, remote: str | None = None) -> Path:
    """Ensure a model file exists at ``local_path``, downloading if needed.

    Args:
        local_path: Expected local file path.
        remote: HTTP(S) URL or HuggingFace repo ID
            (e.g. ``Wespeaker/wespeaker-voxceleb-resnet34-LM``).

    Returns:
        ``local_path``. Raises FileNotFoundError if missing and no remote given.
    """
    if local_path.exists():
        return local_path

    if remote is None:
        raise FileNotFoundError(
            f"Model file not found: {local_path}. "
            "Provide a remote URL or HuggingFace repo ID to auto-download."
        )

    if remote.startswith("http://") or remote.startswith("https://"):
        if remote.startswith("http://") and not remote.startswith(("http://127.", "http://localhost")):
            logger.warning("Downloading model over insecure HTTP (MITM risk): %s", remote)
        _download_url(remote, local_path)
    else:
        _download_hf(remote, local_path)

    return local_path


def _download_url(url: str, dest: Path) -> None:
    """Atomic download from a direct URL."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Per-PID temp file + atomic rename: no truncated cache, no clobbering between workers.
    tmp: Path = dest.with_suffix(dest.suffix + f".part.{os.getpid()}")
    logger.info("Downloading %s → %s", url, dest)
    try:
        with urllib.request.urlopen(url) as response, open(tmp, "wb") as out_file:
            shutil.copyfileobj(response, out_file)
        tmp.replace(dest)
        logger.info("Download complete: %s", dest)
    except Exception as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise RuntimeError(f"Failed to download {url}: {exc}") from exc


def _download_hf(repo_id: str, dest: Path) -> None:
    """Download from HuggingFace Hub and symlink/copy to dest."""
    from huggingface_hub import hf_hub_download

    filename: str = dest.name
    logger.info("Downloading from HF repo %s/%s → %s", repo_id, filename, dest)
    cached_path: Path = Path(hf_hub_download(repo_id=repo_id, filename=filename))
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.symlink_to(cached_path)
    except OSError:
        shutil.copy2(cached_path, dest)
    logger.info("Download complete: %s", dest)
