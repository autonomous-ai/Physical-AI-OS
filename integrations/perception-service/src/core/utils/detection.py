"""Shared detection utilities: NMS, box conversion, preprocessing."""

import cv2
import numpy as np
import numpy.typing as npt


def nms_xywh(
    boxes_xywh: npt.NDArray[np.float32],
    scores: npt.NDArray[np.float32],
    iou_threshold: float = 0.5,
) -> npt.NDArray[np.intp]:
    """Non-maximum suppression on center-based xywh boxes; returns kept indices."""
    if len(boxes_xywh) == 0:
        return np.array([], dtype=np.intp)

    # Center-based xywh → top-left xywh for cv2
    tl_boxes = boxes_xywh.copy()
    tl_boxes[:, 0] -= tl_boxes[:, 2] / 2
    tl_boxes[:, 1] -= tl_boxes[:, 3] / 2

    indices = cv2.dnn.NMSBoxes(
        tl_boxes.tolist(), scores.tolist(), 0.0, iou_threshold,
    )
    if len(indices) == 0:
        return np.array([], dtype=np.intp)
    return np.asarray(indices, dtype=np.intp).flatten()


def letterbox(
    img: npt.NDArray[np.uint8],
    target_size: int = 640,
    pad_value: int = 114,
) -> tuple[npt.NDArray[np.float32], float, tuple[int, int]]:
    """Resize image with letterbox padding for YOLO models.

    Returns:
        img_chw: [3, target_size, target_size] float32 RGB, [0, 1].
        scale: scale factor applied.
        pad: (pad_x, pad_y) center padding.
    """
    h, w = img.shape[:2]
    scale = target_size / max(h, w)
    new_w, new_h = int(w * scale), int(h * scale)

    resized = cv2.resize(img, (new_w, new_h))

    pad_x = (target_size - new_w) // 2
    pad_y = (target_size - new_h) // 2

    padded = np.full((target_size, target_size, 3), pad_value, dtype=np.uint8)
    padded[pad_y: pad_y + new_h, pad_x: pad_x + new_w] = resized

    img_chw = padded[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0
    return img_chw, scale, (pad_x, pad_y)


def unletterbox_boxes(
    boxes_xywh: npt.NDArray[np.float32],
    orig_hw: tuple[int, int],
    target_size: int = 640,
) -> npt.NDArray[np.float32]:
    """Convert boxes from letterboxed normalized [0,1] to original-image normalized [0,1] xywh."""
    h, w = orig_hw
    scale = target_size / max(h, w)
    pad_x = (target_size - int(w * scale)) // 2
    pad_y = (target_size - int(h * scale)) // 2

    out = boxes_xywh.copy()
    out *= target_size
    out[:, 0] -= pad_x
    out[:, 1] -= pad_y
    out /= scale
    # Re-normalize to [0,1] relative to original image
    out[:, [0, 2]] /= w
    out[:, [1, 3]] /= h
    return out


def owlv2_preprocess(
    img: npt.NDArray[np.uint8],
    target_size: int = 1008,
    mean: tuple[float, ...] = (0.48145466, 0.4578275, 0.40821073),
    std: tuple[float, ...] = (0.26862954, 0.26130258, 0.27577711),
) -> tuple[npt.NDArray[np.float32], float]:
    """Preprocess image for OWLv2 — pad to square, resize, CLIP-normalize.

    Returns:
        img_chw: [3, target_size, target_size] float32, normalized.
        max_dim: max(H, W) for box unscaling.
    """
    h, w = img.shape[:2]
    max_dim = max(h, w)

    img_rgb = img[:, :, ::-1].astype(np.float32) / 255.0

    padded = np.full((max_dim, max_dim, 3), 0.5, dtype=np.float32)
    padded[:h, :w] = img_rgb

    resized = cv2.resize(padded, (target_size, target_size))

    mean_arr = np.array(mean, dtype=np.float32).reshape(1, 1, 3)
    std_arr = np.array(std, dtype=np.float32).reshape(1, 1, 3)
    normalized = (resized - mean_arr) / std_arr

    img_chw = normalized.transpose(2, 0, 1).astype(np.float32)
    return img_chw, float(max_dim)


def unowlv2_boxes(
    boxes_xywh: npt.NDArray[np.float32],
    orig_hw: tuple[int, int],
) -> npt.NDArray[np.float32]:
    """Convert OWLv2 boxes (relative to the padded max(H,W) square) to original-image normalized xywh."""
    h, w = orig_hw
    max_dim = float(max(h, w))
    out = boxes_xywh * max_dim
    out[:, [0, 2]] /= w
    out[:, [1, 3]] /= h
    return out


def xyxy_to_normalized_xywh(
    xyxy: npt.NDArray[np.float32],
    img_w: float,
    img_h: float,
) -> npt.NDArray[np.float32]:
    """Convert pixel xyxy to center-based xywh normalized [0, 1]."""
    xywh = np.empty_like(xyxy)
    xywh[:, 0] = ((xyxy[:, 0] + xyxy[:, 2]) / 2) / img_w
    xywh[:, 1] = ((xyxy[:, 1] + xyxy[:, 3]) / 2) / img_h
    xywh[:, 2] = (xyxy[:, 2] - xyxy[:, 0]) / img_w
    xywh[:, 3] = (xyxy[:, 3] - xyxy[:, 1]) / img_h
    return xywh


def xywh_to_xyxy(
    boxes_xywh: npt.NDArray[np.float32],
    img_w: float,
    img_h: float,
) -> npt.NDArray[np.float32]:
    """Convert center-based xywh (pixel coords) to xyxy, clipped to image bounds."""
    xyxy = np.empty_like(boxes_xywh)
    xyxy[:, 0] = np.clip(boxes_xywh[:, 0] - boxes_xywh[:, 2] / 2, 0, img_w)
    xyxy[:, 1] = np.clip(boxes_xywh[:, 1] - boxes_xywh[:, 3] / 2, 0, img_h)
    xyxy[:, 2] = np.clip(boxes_xywh[:, 0] + boxes_xywh[:, 2] / 2, 0, img_w)
    xyxy[:, 3] = np.clip(boxes_xywh[:, 1] + boxes_xywh[:, 3] / 2, 0, img_h)
    return xyxy


def expand_boxes_xyxy(
    xyxy: npt.NDArray[np.float32],
    scale: float,
    img_w: float,
    img_h: float,
) -> npt.NDArray[np.float32]:
    """Expand xyxy boxes by scale around their center, clipped to image bounds."""
    cx = (xyxy[:, 0] + xyxy[:, 2]) / 2
    cy = (xyxy[:, 1] + xyxy[:, 3]) / 2
    w = (xyxy[:, 2] - xyxy[:, 0]) * scale
    h = (xyxy[:, 3] - xyxy[:, 1]) * scale
    out = np.empty_like(xyxy)
    out[:, 0] = np.clip(cx - w / 2, 0, img_w)
    out[:, 1] = np.clip(cy - h / 2, 0, img_h)
    out[:, 2] = np.clip(cx + w / 2, 0, img_w)
    out[:, 3] = np.clip(cy + h / 2, 0, img_h)
    return out
