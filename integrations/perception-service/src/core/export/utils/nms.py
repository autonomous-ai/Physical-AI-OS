"""ONNX-compatible NMS and box conversion utilities."""

import torch
from torchvision.ops import nms


def xyxy_to_xywh_normalized(
    boxes_xyxy: torch.Tensor,
    input_hw: tuple[float, float] = (1, 1),
) -> torch.Tensor:
    """Convert xyxy boxes to center-based xywh, normalized to [0, 1].

    Args:
        boxes_xyxy: [..., 4] — boxes in xyxy format.
        input_hw: (H, W) for normalization. Use (1, 1) if already normalized.

    Returns:
        [..., 4] — center-based xywh normalized to [0, 1].
    """
    inp_h, inp_w = input_hw
    scale = torch.tensor(
        [inp_w, inp_h, inp_w, inp_h], device=boxes_xyxy.device, dtype=boxes_xyxy.dtype
    )
    xyxy = boxes_xyxy / scale
    return torch.stack(
        [
            (xyxy[..., 0] + xyxy[..., 2]) / 2,
            (xyxy[..., 1] + xyxy[..., 3]) / 2,
            xyxy[..., 2] - xyxy[..., 0],
            xyxy[..., 3] - xyxy[..., 1],
        ],
        dim=-1,
    )


def onnx_nms(
    boxes_xyxy: torch.Tensor,
    scores: torch.Tensor,
    input_hw: tuple[float, float] = (1, 1),
    conf_threshold: float = 0.25,
    iou_threshold: float = 0.45,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """NMS that traces cleanly to ONNX (topk instead of boolean masking).

    Args:
        boxes_xyxy: [B, A, 4] — decoded boxes in xyxy format.
        scores: [B, A, K] — per-class scores (after sigmoid).
        input_hw: (H, W) of model input for normalizing output to [0, 1].
            Use (1, 1) if boxes are already normalized.
        conf_threshold: minimum score to keep.
        iou_threshold: IoU threshold for suppression.

    Returns:
        boxes:  [B, num_det, 4]  — center-based xywh, normalized to [0, 1].
        probs:  [B, num_det, K]  — full probability vectors, zero-padded.
        labels: [B, num_det]     — class indices, -1 for padding.
    """
    B, A, K = scores.shape
    conf, labels = scores.max(dim=-1)  # [B, A]

    kept: list[torch.Tensor] = []
    for i in range(B):
        s = conf[i] * (conf[i] > conf_threshold)
        idx = s.topk(min(A, A)).indices
        keep = nms(boxes_xyxy[i][idx], s[idx], iou_threshold)
        kept.append(idx[keep])

    max_det = max(k.shape[0] for k in kept)
    max_det = max(max_det, 1)

    out_boxes = torch.zeros(B, max_det, 4, device=boxes_xyxy.device)
    out_probs = torch.zeros(B, max_det, K, device=scores.device)
    out_labels = torch.full((B, max_det), -1, dtype=labels.dtype, device=labels.device)

    for i, idx in enumerate(kept):
        n = idx.shape[0]
        out_boxes[i, :n] = xyxy_to_xywh_normalized(boxes_xyxy[i][idx], input_hw)
        out_probs[i, :n] = scores[i][idx]
        out_labels[i, :n] = labels[i][idx]

    return out_boxes, out_probs, out_labels
