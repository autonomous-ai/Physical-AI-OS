"""Skeleton format conversions between graph types."""

from collections.abc import Callable

import numpy as np
import numpy.typing as npt

from core.enums.pose import GraphEnum

from .coco import COCOJoint
from .h36m import H36MJoint

# Type alias for converter functions:
#   (keypoints (N, K_src, 2), scores (N, K_src))
#   -> (keypoints (N, K_dst, 2), scores (N, K_dst))
GraphConverter = Callable[
    [npt.NDArray[np.float32], npt.NDArray[np.float32]],
    tuple[npt.NDArray[np.float32], npt.NDArray[np.float32]],
]

# COCO → H36M direct joint mapping: {h36m_idx: coco_idx}
COCO_TO_H36M_DIRECT: dict[int, int] = {
    H36MJoint.L_HIP: COCOJoint.L_HIP,
    H36MJoint.L_KNEE: COCOJoint.L_KNEE,
    H36MJoint.L_FOOT: COCOJoint.L_ANKLE,
    H36MJoint.R_HIP: COCOJoint.R_HIP,
    H36MJoint.R_KNEE: COCOJoint.R_KNEE,
    H36MJoint.R_FOOT: COCOJoint.R_ANKLE,
    H36MJoint.NECK: COCOJoint.NOSE,
    H36MJoint.L_SHOULDER: COCOJoint.L_SHOULDER,
    H36MJoint.L_ELBOW: COCOJoint.L_ELBOW,
    H36MJoint.L_WRIST: COCOJoint.L_WRIST,
    H36MJoint.R_SHOULDER: COCOJoint.R_SHOULDER,
    H36MJoint.R_ELBOW: COCOJoint.R_ELBOW,
    H36MJoint.R_WRIST: COCOJoint.R_WRIST,
}


def coco_to_h36m(
    keypoints: npt.NDArray[np.float32],
    scores: npt.NDArray[np.float32],
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.float32]]:
    """Convert COCO 17-joint keypoints to H36M 17-joint format.

    Args:
        keypoints: (N, 17, 2) array of COCO keypoints (x, y).
        scores:    (N, 17) array of COCO confidence scores.

    Returns:
        h36m_kps:    (N, 17, 2) array in H36M joint order.
        h36m_scores: (N, 17) array of scores in H36M joint order.
    """
    N: int = keypoints.shape[0]
    h36m_kps: npt.NDArray[np.float32] = np.zeros((N, 17, 2), dtype=np.float32)
    h36m_scores: npt.NDArray[np.float32] = np.zeros((N, 17), dtype=np.float32)

    for h36m_idx, coco_idx in COCO_TO_H36M_DIRECT.items():
        h36m_kps[:, h36m_idx] = keypoints[:, coco_idx]
        h36m_scores[:, h36m_idx] = scores[:, coco_idx]

    # Pelvis = midpoint(COCO Left Hip, COCO Right Hip)
    h36m_kps[:, H36MJoint.PELVIS] = (keypoints[:, COCOJoint.L_HIP] + keypoints[:, COCOJoint.R_HIP]) / 2
    h36m_scores[:, H36MJoint.PELVIS] = np.minimum(scores[:, COCOJoint.L_HIP], scores[:, COCOJoint.R_HIP])

    # Thorax = midpoint(COCO Left Shoulder, COCO Right Shoulder)
    h36m_kps[:, H36MJoint.THORAX] = (keypoints[:, COCOJoint.L_SHOULDER] + keypoints[:, COCOJoint.R_SHOULDER]) / 2
    h36m_scores[:, H36MJoint.THORAX] = np.minimum(scores[:, COCOJoint.L_SHOULDER], scores[:, COCOJoint.R_SHOULDER])

    # Spine = midpoint(Pelvis, Thorax)
    h36m_kps[:, H36MJoint.SPINE] = (h36m_kps[:, H36MJoint.PELVIS] + h36m_kps[:, H36MJoint.THORAX]) / 2
    h36m_scores[:, H36MJoint.SPINE] = np.minimum(h36m_scores[:, H36MJoint.PELVIS], h36m_scores[:, H36MJoint.THORAX])

    # Head = midpoint(COCO Left Ear, COCO Right Ear)
    h36m_kps[:, H36MJoint.HEAD] = (keypoints[:, COCOJoint.L_EAR] + keypoints[:, COCOJoint.R_EAR]) / 2
    h36m_scores[:, H36MJoint.HEAD] = np.minimum(scores[:, COCOJoint.L_EAR], scores[:, COCOJoint.R_EAR])

    return h36m_kps, h36m_scores


CONVERTER_REGISTRY: dict[tuple[GraphEnum, GraphEnum], GraphConverter] = {
    (GraphEnum.COCO, GraphEnum.H36M): coco_to_h36m,
}


def get_graph_converter(
    source: GraphEnum,
    target: GraphEnum,
) -> GraphConverter | None:
    """Look up a converter for source -> target; None if equal, ValueError if unregistered."""
    if source == target:
        return None
    converter: GraphConverter | None = CONVERTER_REGISTRY.get((source, target))
    if converter is None:
        raise ValueError(f"No converter registered for {source} → {target}")
    return converter


def convert_graph(
    keypoints: npt.NDArray[np.float32],
    scores: npt.NDArray[np.float32],
    source: GraphEnum,
    target: GraphEnum,
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.float32]]:
    """Convert keypoints/scores between graph types. No-op if same."""
    converter = get_graph_converter(source, target)
    if converter is not None:
        return converter(keypoints, scores)
    return keypoints, scores
