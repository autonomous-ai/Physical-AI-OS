"""Face alignment geometry — similarity-transform warp/crop helpers."""

import cv2
import numpy as np
from numpy.linalg import inv, lstsq, matrix_rank, norm

_REFERENCE_FACIAL_POINTS = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)


class _FaceWarpException(Exception):
    def __str__(self) -> str:
        return f"In File {__file__}: {super().__str__()}"


def _tformfwd(trans: np.ndarray, uv: np.ndarray) -> np.ndarray:
    """Apply forward affine transform."""
    uv_h = np.hstack((uv, np.ones((uv.shape[0], 1))))
    xy = uv_h @ trans
    return xy[:, :-1]


def _find_nonreflective_similarity(
    uv: np.ndarray, xy: np.ndarray, options: dict | None = None
):
    """Find non-reflective similarity transform between uv and xy."""
    K = options.get("K", 2) if options else 2
    M = xy.shape[0]

    x, y = xy[:, 0:1], xy[:, 1:2]
    u, v = uv[:, 0:1], uv[:, 1:2]

    X = np.vstack(
        (
            np.hstack((x, y, np.ones((M, 1)), np.zeros((M, 1)))),
            np.hstack((y, -x, np.zeros((M, 1)), np.ones((M, 1)))),
        )
    )
    U = np.vstack((u, v))

    if matrix_rank(X) >= 2 * K:
        r, _, _, _ = lstsq(X, U, rcond=None)
    else:
        raise ValueError("cp2tform:twoUniquePointsReq")

    sc, ss, tx, ty = r.flatten()
    Tinv = np.array([[sc, -ss, 0], [ss, sc, 0], [tx, ty, 1]])
    T = inv(Tinv)
    T[:, 2] = [0, 0, 1]
    return T, Tinv


def _find_similarity(uv: np.ndarray, xy: np.ndarray, options: dict | None = None):
    """Find similarity transform with optional reflection."""
    trans1, trans1_inv = _find_nonreflective_similarity(uv, xy, options)

    xyR = xy.copy()
    xyR[:, 0] *= -1
    trans2r, _ = _find_nonreflective_similarity(uv, xyR, options)

    TreflectY = np.array([[-1, 0, 0], [0, 1, 0], [0, 0, 1]])
    trans2 = trans2r @ TreflectY

    norm1 = norm(_tformfwd(trans1, uv) - xy)
    norm2 = norm(_tformfwd(trans2, uv) - xy)

    return (trans1, trans1_inv) if norm1 <= norm2 else (trans2, inv(trans2))


def _get_similarity_transform_for_cv2(
    src_pts: np.ndarray, dst_pts: np.ndarray, reflective: bool = True
) -> np.ndarray:
    """Get cv2-compatible affine transform matrix."""
    trans, _ = (
        _find_similarity(src_pts, dst_pts)
        if reflective
        else _find_nonreflective_similarity(src_pts, dst_pts)
    )
    return trans[:, :2].T


def _warp_and_crop_face(
    src_img: np.ndarray,
    facial_pts,
    reference_pts: np.ndarray = _REFERENCE_FACIAL_POINTS,
    crop_size: tuple[int, int] = (112, 112),
    scale: float = 1.0,
) -> np.ndarray:
    """Warp and crop face using a similarity transform to the reference template."""
    ref_pts = reference_pts * scale
    ref_pts += np.mean(reference_pts, axis=0) - np.mean(ref_pts, axis=0)

    src_pts = np.array(facial_pts, dtype=np.float32)
    if src_pts.shape != ref_pts.shape:
        raise _FaceWarpException(
            "facial_pts and reference_pts must have the same shape"
        )

    tfm = _get_similarity_transform_for_cv2(src_pts, ref_pts)
    return cv2.warpAffine(src_img, tfm, crop_size)


def _landmarks_out_of_bounds(pts5: np.ndarray, bbox, frame_shape) -> bool:
    """True if any of the 5 alignment points falls outside the bbox or image."""
    h, w = frame_shape[:2]
    x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
    x_lo, x_hi = max(0.0, x1), min(float(w), x2)
    y_lo, y_hi = max(0.0, y1), min(float(h), y2)
    xs, ys = pts5[:, 0], pts5[:, 1]
    return bool(
        np.any(xs < x_lo) or np.any(xs > x_hi)
        or np.any(ys < y_lo) or np.any(ys > y_hi)
    )


def _box_from_landmarks(landmarks_xy: np.ndarray, w: int, h: int) -> list[int]:
    """Axis-aligned face box ``[x1, y1, x2, y2]`` from dense landmarks in FRAME
    pixel coords.
    """
    xs = np.minimum(np.floor(landmarks_xy[:, 0]).astype(int), w - 1)
    ys = np.minimum(np.floor(landmarks_xy[:, 1]).astype(int), h - 1)
    start_x = max(0, int(xs.min()))
    start_y = max(0, int(ys.min()))
    end_x = min(w - 1, int(xs.max()))
    end_y = min(h - 1, int(ys.max()))
    return [start_x, start_y, end_x, end_y]
