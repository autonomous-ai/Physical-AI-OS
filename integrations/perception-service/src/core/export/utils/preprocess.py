"""Preprocessing utilities for ONNX model input."""

import cv2
import numpy as np
import numpy.typing as npt


def letterbox(
    img: npt.NDArray[np.uint8],
    target_size: int = 640,
    pad_value: int = 114,
) -> tuple[npt.NDArray[np.float32], float, tuple[int, int]]:
    """Resize image with letterbox padding, matching ultralytics preprocessing.

    Args:
        img: BGR image (H, W, 3) uint8.
        target_size: target square size (e.g. 640).
        pad_value: fill value for padding (default 114, same as ultralytics).

    Returns:
        img_chw: [3, target_size, target_size] float32 RGB, normalized to [0, 1].
        scale: scale factor applied to the image.
        pad: (pad_x, pad_y) padding added to each side.
    """
    h, w = img.shape[:2]
    scale = target_size / max(h, w)
    new_w, new_h = int(w * scale), int(h * scale)

    resized = cv2.resize(img, (new_w, new_h))

    pad_x = (target_size - new_w) // 2
    pad_y = (target_size - new_h) // 2

    padded = np.full((target_size, target_size, 3), pad_value, dtype=np.uint8)
    padded[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = resized

    # BGR → RGB, HWC → CHW, normalize to [0, 1]
    img_chw = padded[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0
    return img_chw, scale, (pad_x, pad_y)


def owlv2_preprocess(
    img: npt.NDArray[np.uint8],
    target_size: int = 1008,
    mean: tuple[float, ...] = (0.48145466, 0.4578275, 0.40821073),
    std: tuple[float, ...] = (0.26862954, 0.26130258, 0.27577711),
) -> tuple[npt.NDArray[np.float32], float]:
    """Preprocess image for OWLv2, matching HuggingFace Owlv2Processor.

    Args:
        img: BGR image (H, W, 3) uint8.
        target_size: target square size (default 1008).
        mean: CLIP normalization mean.
        std: CLIP normalization std.

    Returns:
        img_chw: [3, target_size, target_size] float32 RGB, normalized.
        max_dim: max(H, W) of original image, for unscaling boxes.
    """
    h, w = img.shape[:2]
    max_dim = max(h, w)

    # BGR → RGB, float [0, 1]
    img_rgb = img[:, :, ::-1].astype(np.float32) / 255.0

    # Pad bottom-right to square with 0.5
    padded = np.full((max_dim, max_dim, 3), 0.5, dtype=np.float32)
    padded[:h, :w] = img_rgb

    resized = cv2.resize(padded, (target_size, target_size))

    mean_arr = np.array(mean, dtype=np.float32).reshape(1, 1, 3)
    std_arr = np.array(std, dtype=np.float32).reshape(1, 1, 3)
    normalized = (resized - mean_arr) / std_arr

    img_chw = normalized.transpose(2, 0, 1).astype(np.float32)
    return img_chw, max_dim


def unletterbox_boxes(
    boxes_xywh: npt.NDArray[np.float32],
    scale: float,
    pad: tuple[int, int],
    target_size: int = 640,
) -> npt.NDArray[np.float32]:
    """Convert boxes from letterboxed model coords back to original image pixel coords.

    Args:
        boxes_xywh: [N, 4] center-based xywh normalized to [0, 1].
        scale: scale factor from letterbox().
        pad: (pad_x, pad_y) from letterbox().
        target_size: model input size.

    Returns:
        [N, 4] center-based xywh in original image pixel coords.
    """
    out = boxes_xywh.copy()
    # Normalized [0,1] → pixel coords in letterboxed image
    out *= target_size
    # Remove padding offset (center coords)
    out[:, 0] -= pad[0]
    out[:, 1] -= pad[1]
    # Undo scale
    out /= scale
    return out


def unowlv2_boxes(
    boxes_xywh: npt.NDArray[np.float32],
    max_dim: float,
) -> npt.NDArray[np.float32]:
    """Convert OWLv2 boxes (normalized to the padded max_dim square) to original pixel coords.

    Args:
        boxes_xywh: [N, 4] center-based xywh normalized to [0, 1].
        max_dim: max(H, W) from owlv2_preprocess().

    Returns:
        [N, 4] center-based xywh in original image pixel coords.
    """
    return boxes_xywh * max_dim
