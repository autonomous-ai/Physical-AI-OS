"""RULA-specific geometry helpers."""

import numpy as np
import numpy.typing as npt

from core.perception.pose.graph.h36m import H36MSkeleton
from core.utils.compute import angle_between_3d, rotate_to

_H36M = H36MSkeleton()


def signed_flexion_angle(
    v: npt.NDArray[np.float32],
    trunk_up: npt.NDArray[np.float32],
) -> float:
    """Signed flexion angle (degrees) of *v* relative to *trunk_up*; positive = forward flexion.

    Assumes ``align_to_vertical`` was applied (x=right, y=down, z=depth); the sign
    comes from the X component of ``cross(trunk_up, v)``.
    """
    angle: float = angle_between_3d(v, trunk_up)
    cross: npt.NDArray[np.float32] = np.cross(trunk_up, v)
    # cross[0] = X component = rotation direction in the sagittal plane.
    if cross[0] > 0:
        return angle
    else:
        return -angle


def align_to_vertical(
    keypoints: npt.NDArray[np.float32],
) -> npt.NDArray[np.float32]:
    """Rotate 3D keypoints so spine->thorax points to [0, -1, 0] (x=right, y=down, z=depth)."""
    spine_idx: int = _H36M.joint("SPINE")
    thorax_idx: int = _H36M.joint("THORAX")
    spine: npt.NDArray[np.float32] = keypoints[spine_idx]
    thorax: npt.NDArray[np.float32] = keypoints[thorax_idx]
    trunk_vec: npt.NDArray[np.float32] = thorax - spine

    return rotate_to(
        keypoints,
        src_vec=trunk_vec,
        dst_vec=np.array([0.0, -1.0, 0.0], dtype=np.float32),
        center=spine,
    )


def joint_name(idx: int) -> str:
    """Get lowercase joint name for reporting."""
    return H36MSkeleton.JOINT_NAMES.get(idx, str(idx)).lower()
