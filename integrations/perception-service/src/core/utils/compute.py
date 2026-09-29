from typing import Any

import numpy as np
import numpy.typing as npt

EPSILON: float = 1e-8


def softmax(x: npt.NDArray[np.number[Any]], axis: int = -1) -> npt.NDArray[np.float32]:
    e: npt.NDArray[np.float32] = np.exp(x - x.max(axis=axis, keepdims=True)).astype(np.float32)
    return e / (e.sum(axis=axis, keepdims=True) + EPSILON)


def angle_between_3d(
    v1: npt.NDArray[np.float32],
    v2: npt.NDArray[np.float32],
) -> float:
    """Unsigned angle in degrees between two 3D vectors."""
    denom: float = float(np.linalg.norm(v1) * np.linalg.norm(v2))
    if denom < EPSILON:
        return 0.0
    cos: float = float(np.clip(np.dot(v1, v2) / denom, -1.0, 1.0))
    return float(np.degrees(np.arccos(cos)))


def ensure_3d(keypoints: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
    """Pad 2D (N, 2) to 3D (N, 3) with z=0 (x=right, y=down, z=depth); copies if already 3D."""
    if keypoints.shape[1] >= 3:
        return keypoints.copy()
    zeros: npt.NDArray[np.float32] = np.zeros((keypoints.shape[0], 1), dtype=np.float32)
    return np.concatenate([keypoints, zeros], axis=1).astype(np.float32)


def rotate_to(
    points: npt.NDArray[np.float32],
    src_vec: npt.NDArray[np.float32],
    dst_vec: npt.NDArray[np.float32],
    center: npt.NDArray[np.float32] | None = None,
) -> npt.NDArray[np.float32]:
    """Rotate *points* so that *src_vec* aligns with *dst_vec* using Rodrigues' formula.

    Args:
        points: (N, 3) array of points to rotate.
        src_vec: Source direction vector (will be normalized).
        dst_vec: Target direction vector (will be normalized).
        center: Optional center of rotation. If None, rotates around origin.

    Returns:
        (N, 3) rotated points.
    """
    src_norm: float = float(np.linalg.norm(src_vec))
    dst_norm: float = float(np.linalg.norm(dst_vec))
    if src_norm < EPSILON or dst_norm < EPSILON:
        return points.copy()

    src: npt.NDArray[np.float32] = (src_vec / src_norm).astype(np.float32)
    dst: npt.NDArray[np.float32] = (dst_vec / dst_norm).astype(np.float32)

    cross: npt.NDArray[np.float32] = np.cross(src, dst).astype(np.float32)
    sin_a: float = float(np.linalg.norm(cross))
    cos_a: float = float(np.dot(src, dst))

    if sin_a < EPSILON:
        return points.copy()

    centered: npt.NDArray[np.float32] = points.copy()
    if center is not None:
        centered = centered - center

    k: npt.NDArray[np.float32] = (cross / sin_a).astype(np.float32)
    K: npt.NDArray[np.float32] = np.array([
        [0, -k[2], k[1]],
        [k[2], 0, -k[0]],
        [-k[1], k[0], 0],
    ], dtype=np.float32)

    R: npt.NDArray[np.float32] = (
        np.eye(3, dtype=np.float32) + sin_a * K + (1 - cos_a) * (K @ K)
    )

    rotated: npt.NDArray[np.float32] = (centered @ R.T).astype(np.float32)
    if center is not None:
        rotated = rotated + center
    return rotated
