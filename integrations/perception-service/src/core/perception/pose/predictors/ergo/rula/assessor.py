"""RULA (Rapid Upper Limb Assessment) ergonomic assessor.

Reference: McAtamney & Corlett (1993), "RULA: a survey method for the
investigation of work-related upper limb disorders."
"""
# TODO: PoC based on https://ergo-plus.com/wp-content/uploads/RULA-A-Step-by-Step-Guide1.pdf; logic needs verification.

from typing import Any

import numpy as np
import numpy.typing as npt
from typing_extensions import override

from core.enums.pose import GraphEnum
from core.models.pose import BodyPartScores, ErgoAssessment, SideAssessment
from core.perception.pose.graph.h36m import H36MSkeleton
from core.utils.common import get_or_default
from core.utils.compute import angle_between_3d, ensure_3d

from ..base import ErgoAssessor, ErgoInput
from .scores import (
    lookup_table_a,
    lookup_table_b,
    lookup_table_c,
    risk_level_from_score,
    score_lower_arm,
    score_neck,
    score_trunk,
    score_upper_arm,
)
from .utils import align_to_vertical, joint_name, signed_flexion_angle

_H36M = H36MSkeleton()


class RULAAssessor(ErgoAssessor):
    """RULA ergonomic assessment from H36M keypoints.

    Sides are assessed independently after aligning spine->thorax to vertical;
    the overall score is the worse side. Low-confidence joints are skipped.
    """

    GRAPH_TYPE: GraphEnum = GraphEnum.H36M

    DEFAULT_CONFIDENCE_THRESHOLD: float = 0.3
    DEFAULT_MUSCLE_USE_SCORE: int = 0
    DEFAULT_FORCE_LOAD_SCORE: int = 0

    def __init__(
        self,
        confidence_threshold: float | None = None,
        muscle_use_score: int | None = None,
        force_load_score: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        super().__init__(batch_size=batch_size)
        self._confidence_threshold: float = get_or_default(
            confidence_threshold, self.DEFAULT_CONFIDENCE_THRESHOLD
        )
        self._muscle_use_score: int = get_or_default(
            muscle_use_score, self.DEFAULT_MUSCLE_USE_SCORE
        )
        self._force_load_score: int = get_or_default(
            force_load_score, self.DEFAULT_FORCE_LOAD_SCORE
        )

    def _assess_side(
        self,
        kps: npt.NDArray[np.float32],
        confs: npt.NDArray[np.float32],
        side: str,
    ) -> SideAssessment:
        """Assess one side. All keypoints must be 3D and already aligned."""
        if side == "right":
            shoulder_idx = _H36M.joint("R_SHOULDER")
            elbow_idx = _H36M.joint("R_ELBOW")
            wrist_idx = _H36M.joint("R_WRIST")
        else:
            shoulder_idx = _H36M.joint("L_SHOULDER")
            elbow_idx = _H36M.joint("L_ELBOW")
            wrist_idx = _H36M.joint("L_WRIST")

        threshold: float = self._confidence_threshold
        neck_idx: int = _H36M.joint("NECK")
        head_idx: int = _H36M.joint("HEAD")

        relevant_joints: list[int] = [shoulder_idx, elbow_idx, wrist_idx, neck_idx, head_idx]
        skipped_set: set[str] = {
            joint_name(idx) for idx in relevant_joints if confs[idx] < threshold
        }

        shoulder_ok: bool = confs[shoulder_idx] >= threshold
        elbow_ok: bool = confs[elbow_idx] >= threshold
        wrist_ok: bool = confs[wrist_idx] >= threshold
        neck_ok: bool = confs[neck_idx] >= threshold
        head_ok: bool = confs[head_idx] >= threshold

        # Trunk direction (spine -> thorax) = up after alignment
        trunk_up: npt.NDArray[np.float32] = (
            kps[_H36M.joint("THORAX")] - kps[_H36M.joint("SPINE")]
        )

        if shoulder_ok and elbow_ok:
            upper_arm_vec: npt.NDArray[np.float32] = kps[elbow_idx] - kps[shoulder_idx]
            upper_arm_angle: float = signed_flexion_angle(upper_arm_vec, trunk_up)
            upper_arm_score: int = score_upper_arm(upper_arm_angle)
        else:
            upper_arm_angle = 0.0
            upper_arm_score = 1

        # Low-confidence joints fall back to neutral (score 1) and are reported in skipped_joints.
        if shoulder_ok and elbow_ok and wrist_ok:
            forearm_vec: npt.NDArray[np.float32] = kps[wrist_idx] - kps[elbow_idx]
            upper_arm_neg: npt.NDArray[np.float32] = kps[shoulder_idx] - kps[elbow_idx]
            lower_arm_angle: float = angle_between_3d(forearm_vec, upper_arm_neg)
            lower_arm_score: int = score_lower_arm(lower_arm_angle)
        else:
            # 80° ≈ mid-point of the neutral 60–100° elbow-flexion band → score 1.
            lower_arm_angle = 80.0
            lower_arm_score = 1

        # Wrist — H36M has no hand keypoints, always neutral
        wrist_score: int = 1
        wrist_twist_score: int = 1

        if neck_ok and head_ok:
            neck_vec: npt.NDArray[np.float32] = kps[head_idx] - kps[neck_idx]
            neck_angle: float = signed_flexion_angle(neck_vec, trunk_up)
            neck_score: int = score_neck(neck_angle)
        else:
            neck_angle = 0.0
            neck_score = 1

        true_vertical: npt.NDArray[np.float32] = np.array([0.0, -1.0, 0.0], dtype=np.float32)
        trunk_angle: float = signed_flexion_angle(trunk_up, true_vertical)
        trunk_score: int = score_trunk(trunk_angle)

        leg_score: int = 1

        table_a_score: int = lookup_table_a(
            upper_arm_score, lower_arm_score, wrist_score, wrist_twist_score
        )
        score_a: int = table_a_score + self._muscle_use_score + self._force_load_score

        table_b_score: int = lookup_table_b(neck_score, trunk_score, leg_score)
        score_b: int = table_b_score + self._muscle_use_score + self._force_load_score

        final_score: int = lookup_table_c(score_a, score_b)

        return SideAssessment(
            score=final_score,
            risk_level=risk_level_from_score(final_score),
            body_scores=BodyPartScores(
                upper_arm=upper_arm_score,
                lower_arm=lower_arm_score,
                wrist=wrist_score,
                wrist_twist=wrist_twist_score,
                neck=neck_score,
                trunk=trunk_score,
                legs=leg_score,
                table_a=table_a_score,
                table_b=table_b_score,
                score_a=score_a,
                score_b=score_b,
                upper_arm_angle=int(upper_arm_angle),
                lower_arm_angle=int(lower_arm_angle),
                neck_angle=int(neck_angle),
                trunk_angle=int(trunk_angle),
            ),
            skipped_joints=sorted(skipped_set),
        )

    def _assess_single(
        self,
        keypoints: npt.NDArray[np.float32],
        scores: npt.NDArray[np.float32],
    ) -> ErgoAssessment | None:
        """Run RULA on both sides of the body for a single frame.

        Args:
            keypoints: (17, 2) or (17, 3) H36M joint positions.
            scores:    (17,) confidence scores.

        Returns:
            ErgoAssessment with left/right results, or None if spine/thorax
            are not confident enough to define the trunk.
        """
        spine_idx: int = _H36M.joint("SPINE")
        thorax_idx: int = _H36M.joint("THORAX")

        if (
            scores[spine_idx] < self._confidence_threshold
            or scores[thorax_idx] < self._confidence_threshold
        ):
            return None

        kps_3d: npt.NDArray[np.float32] = ensure_3d(keypoints)
        aligned: npt.NDArray[np.float32] = align_to_vertical(kps_3d)

        left: SideAssessment = self._assess_side(aligned, scores, "left")
        right: SideAssessment = self._assess_side(aligned, scores, "right")

        overall_score: int = max(left.score, right.score)

        return ErgoAssessment(
            left=left,
            right=right,
            score=overall_score,
            risk_level=risk_level_from_score(overall_score),
        )

    @override
    def _predict_impl(
        self,
        input: list[ErgoInput],
        *,
        preprocess: bool = True,
        **kwargs: Any,
    ) -> list[ErgoAssessment | None]:
        """Run RULA assessment on a batch of (keypoints, scores) pairs."""
        results: list[ErgoAssessment | None] = [
            self._assess_single(kps, scores) for kps, scores in input
        ]

        for result in results:
            if result is not None:
                self._logger.info(
                    "RULA score=%d (L=%d, R=%d), risk=%s",
                    result.score,
                    result.left.score,
                    result.right.score,
                    result.risk_level.name,
                )

        return results
