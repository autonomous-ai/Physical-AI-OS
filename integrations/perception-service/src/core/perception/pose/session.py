"""Per-connection pose estimation session."""

import time
from typing import Any, cast

import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
from typing_extensions import override

from core.enums.pose import GraphEnum
from core.models.pose import (
    ErgoAssessment,
    Point2D,
    Point3D,
    Pose2D,
    Pose3D,
    PoseDetection,
    PosePerceptionSessionConfig,
    RawPose2DDetection,
    RawPose3DDetection,
)
from core.perception.base import PerceptionSessionBase
from core.perception.base.batching import InputBatcher
from core.perception.pose.graph.convert import convert_graph
from core.perception.pose.predictors.ergo.base import ErgoAssessor, ErgoInput
from core.perception.pose.predictors.pose2d.base import PoseEstimator2D
from core.perception.pose.predictors.pose3d.base import Pose3DInput, PoseEstimator3DLifting
from core.types import Omit, omit
from core.utils.common import get_or_default


class PosePerceptionSession(
    PerceptionSessionBase[
        cv2t.MatLike,
        PoseDetection,
        PosePerceptionSessionConfig,
    ]
):
    DEFAULT_CONFIG: PosePerceptionSessionConfig = PosePerceptionSessionConfig()

    def __init__(
        self,
        pose2d_batcher: InputBatcher[cv2t.MatLike, RawPose2DDetection],
        pose3d_batcher: InputBatcher[Pose3DInput, RawPose3DDetection | None] | None = None,
        ergo_batcher: InputBatcher[ErgoInput, ErgoAssessment | None] | None = None,
        config: PosePerceptionSessionConfig | None = None,
    ) -> None:
        config = get_or_default(config, PosePerceptionSessionConfig())
        super().__init__(config)

        self._pose2d_batcher: InputBatcher[cv2t.MatLike, RawPose2DDetection] = pose2d_batcher
        self._pose3d_batcher: InputBatcher[Pose3DInput, RawPose3DDetection | None] | None = (
            pose3d_batcher
        )
        self._ergo_batcher: InputBatcher[ErgoInput, ErgoAssessment | None] | None = ergo_batcher

        self._kps_buffer: list[npt.NDArray[np.float32]] = []
        self._scores_buffer: list[npt.NDArray[np.float32]] = []

        self._running: bool = False

    @override
    async def start(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return
        self._running = True

    @override
    async def stop(self) -> None:
        self._running = False

    @override
    def is_ready(self) -> bool:
        if not self._pose2d_batcher.is_ready():
            return False
        if self._pose3d_batcher is not None and not self._pose3d_batcher.is_ready():
            return False
        return self._running

    @override
    async def update(self, input: cv2t.MatLike) -> PoseDetection | None:
        """Run pose estimation (2D, optional 3D and ergo) on one frame; None if rate-limited."""
        cur_ts: float = time.time()
        if cur_ts - self._last_update_ts < self._config.frame_interval:
            return self._last_prediction

        estimator_2d = cast(PoseEstimator2D, self._pose2d_batcher.predictor)

        futures_2d = await self._pose2d_batcher.submit([input])
        raw_2d: RawPose2DDetection = await futures_2d[0]

        kps: npt.NDArray[np.float32] = raw_2d.keypoints[0]  # (K, 2)
        confs: npt.NDArray[np.float32] = raw_2d.scores[0]  # (K,)
        src_graph: GraphEnum = estimator_2d.GRAPH_TYPE

        pose_2d: Pose2D = Pose2D(
            graph_type=src_graph,
            joints=[Point2D(x=float(kps[i, 0]), y=float(kps[i, 1])) for i in range(len(kps))],
            confs=[float(c) for c in confs],
        )

        result: PoseDetection = PoseDetection(pose_2d=pose_2d)

        num_valid: int = int((confs >= self._config.confidence_threshold_2d).sum())
        has_valid_pose: bool = num_valid >= self._config.min_valid_keypoints

        lifter_kps: npt.NDArray[np.float32] | None = None
        lifter_scores: npt.NDArray[np.float32] | None = None
        if self._pose3d_batcher is not None and has_valid_pose:
            lifter_3d = cast(PoseEstimator3DLifting, self._pose3d_batcher.predictor)

            lifter_kps, lifter_scores = convert_graph(
                raw_2d.keypoints,
                raw_2d.scores,
                src_graph,
                lifter_3d.GRAPH_TYPE,
            )

            self._kps_buffer.append(lifter_kps[0])
            self._scores_buffer.append(lifter_scores[0])

            n_frames: int = lifter_3d.n_frames
            if len(self._kps_buffer) > n_frames:
                self._kps_buffer = self._kps_buffer[-n_frames:]
            if len(self._scores_buffer) > n_frames:
                self._scores_buffer = self._scores_buffer[-n_frames:]

            kps_stack: npt.NDArray[np.float32] = np.stack(self._kps_buffer, axis=0)
            scores_stack: npt.NDArray[np.float32] = np.stack(self._scores_buffer, axis=0)

            futures_3d = await self._pose3d_batcher.submit([(kps_stack, scores_stack)])
            raw_3d: RawPose3DDetection | None = await futures_3d[0]

            if raw_3d is not None:
                joints_last: npt.NDArray[np.float32] = raw_3d.joints_3d[-1]  # (K, 3)
                result.pose_3d = Pose3D(
                    graph_type=lifter_3d.GRAPH_TYPE,
                    joints=[
                        Point3D(
                            x=float(joints_last[i, 0]),
                            y=float(joints_last[i, 1]),
                            z=float(joints_last[i, 2]),
                        )
                        for i in range(len(joints_last))
                    ],
                    confs=[float(c) for c in lifter_scores[0]],
                )

        if self._ergo_batcher is not None and has_valid_pose:
            ergo_assessor = cast(ErgoAssessor, self._ergo_batcher.predictor)
            ergo_graph: GraphEnum = ergo_assessor.GRAPH_TYPE

            if (
                result.pose_3d is not None
                and self._pose3d_batcher is not None
                and lifter_kps is not None
                and lifter_scores is not None
            ):
                lifter_3d = cast(PoseEstimator3DLifting, self._pose3d_batcher.predictor)
                ergo_kps, ergo_scores = convert_graph(
                    lifter_kps,
                    lifter_scores,
                    lifter_3d.GRAPH_TYPE,
                    ergo_graph,
                )
            else:
                ergo_kps, ergo_scores = convert_graph(
                    raw_2d.keypoints,
                    raw_2d.scores,
                    src_graph,
                    ergo_graph,
                )

            ergo_input: ErgoInput = (ergo_kps[0], ergo_scores[0])
            ergo_futures = await self._ergo_batcher.submit([ergo_input])
            ergo_result: ErgoAssessment | None = await ergo_futures[0]
            if ergo_result is not None:
                result.ergo = ergo_result

        self._last_update_ts = cur_ts
        self._last_prediction = result

        if result.pose_2d.joints:
            self._logger.info(
                "[session %s] Pose: %d joints, 3D=%s, ergo=%d",
                self._session_id,
                len(result.pose_2d.joints),
                result.pose_3d is not None,
                result.ergo.score if result.ergo is not None else -1,
            )

        return result

    @override
    def update_config(
        self,
        *,
        frame_interval: float | Omit = omit,
        confidence_threshold_2d: float | Omit = omit,
        min_valid_keypoints: int | Omit = omit,
        **kwargs: Any,
    ) -> None:
        super().update_config(
            frame_interval=frame_interval,
            confidence_threshold_2d=confidence_threshold_2d,
            min_valid_keypoints=min_valid_keypoints,
        )

    @override
    def _post_config_update(self) -> None:
        self._logger.info(
            "[session %s] Config updated — frame_interval=%.2f, confidence_threshold_2d=%.2f, min_valid_keypoints=%d",
            self._session_id,
            self._config.frame_interval,
            self._config.confidence_threshold_2d,
            self._config.min_valid_keypoints,
        )
