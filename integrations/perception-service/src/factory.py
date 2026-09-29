"""Perception builders; predictors are created and started inside Perception.start()."""

import logging
from pathlib import Path
from typing import Any

from config import settings
from core.enums.face import FaceDetectorEnum
from core.enums.object import ObjectDetectorEnum
from core.models.action import ActionPerceptionSessionConfig
from core.models.facial_emotion import EmotionPerceptionSessionConfig
from core.models.object import ObjectPerceptionSessionConfig
from core.models.pose import PosePerceptionSessionConfig
from core.perception.action.perception import ActionPerception
from core.perception.action.utils import ActionRecognizerFactory
from core.perception.audio.predictors.base import AudioEmbedder
from core.perception.audio.processors.utils import AudioProcessorFactory
from core.perception.audio.utils import create_embedder
from core.perception.audio_emotion.perception import AudioEmotionPerception
from core.perception.audio_emotion.utils import AudioEmotionRecognizerFactory
from core.perception.face.utils import FaceDetectorFactory
from core.perception.facial_emotion.perception import EmotionPerception
from core.perception.facial_emotion.utils import EmotionRecognizerFactory
from core.perception.object.perception import ObjectPerception
from core.perception.object.utils import ObjectDetectorFactory
from core.perception.person.utils import PersonDetectorFactory
from core.perception.pose.perception import PosePerception
from core.perception.pose.utils import (
    ErgoAssessorFactory,
    PoseEstimator2DFactory,
    PoseLifter3DFactory,
)

logger: logging.Logger = logging.getLogger(__name__)


def build_action_perception() -> ActionPerception:
    """Create ActionPerception with factories from settings."""
    action_ckpt: Path | None = (
        Path(settings.action.ckpt_path) if settings.action.ckpt_path else None
    )
    action_frame_size: tuple[int, int] | None = None
    if settings.action.w is not None and settings.action.h is not None:
        action_frame_size = (settings.action.h, settings.action.w)

    action_factory = ActionRecognizerFactory(
        model_name=settings.action.model,
        model_path=action_ckpt,
        remote_url=settings.action.remote_url,
        max_frames=settings.action.max_frames,
        frame_size=action_frame_size,
        batch_size=settings.action.batch_size,
    )

    person_factory: PersonDetectorFactory | None = None
    if settings.person_detector.enabled:
        person_factory = PersonDetectorFactory(
            model_name=settings.person_detector.model,
            model_path=settings.person_detector.model_name,
            threshold=settings.person_detector.confidence_threshold,
            bbox_expand_scale=settings.person_detector.bbox_expand_scale,
            batch_size=settings.person_detector.batch_size,
        )

    default_config: ActionPerceptionSessionConfig = ActionPerceptionSessionConfig()
    if settings.action.frame_interval is not None:
        default_config.frame_interval = settings.action.frame_interval
    if settings.action.confidence_threshold is not None:
        default_config.threshold = settings.action.confidence_threshold

    return ActionPerception(
        action_recognizer_factory=action_factory,
        person_detector_factory=person_factory,
        default_config=default_config,
        batch_size=settings.action.batch_size,
        batch_timeout=settings.action.batch_timeout,
    )


def build_emotion_perception() -> EmotionPerception:
    """Create EmotionPerception with factories from settings."""
    emotion_ckpt: Path | None = (
        Path(settings.fer.ckpt_path) if settings.fer.ckpt_path else None
    )

    emotion_factory = EmotionRecognizerFactory(
        model_name=settings.fer.model,
        model_path=emotion_ckpt,
        remote_url=settings.fer.remote_url,
        batch_size=settings.fer.batch_size,
    )

    face_factory = FaceDetectorFactory(model_name=FaceDetectorEnum.YUNET)

    default_config: EmotionPerceptionSessionConfig | None = None
    if (
        settings.fer.confidence_threshold is not None
        or settings.fer.frame_interval is not None
        or settings.fer.label_thresholds is not None
    ):
        default_config = EmotionPerceptionSessionConfig(
            confidence_threshold=settings.fer.confidence_threshold or 0.5,
            frame_interval=settings.fer.frame_interval or 1.0,
            label_thresholds=settings.fer.label_thresholds,
        )

    return EmotionPerception(
        emotion_recognizer_factory=emotion_factory,
        face_detector_factory=face_factory,
        default_config=default_config,
        batch_size=settings.fer.batch_size,
        batch_timeout=settings.fer.batch_timeout,
    )


def build_pose_perception() -> PosePerception:
    """Create PosePerception with factories from settings."""
    pose_ckpt: Path | None = Path(settings.pose.ckpt_path) if settings.pose.ckpt_path else None

    estimator_2d_factory = PoseEstimator2DFactory(
        model_name=settings.pose.model,
        model_path=pose_ckpt,
        remote_url=settings.pose.remote_url,
        batch_size=settings.pose.batch_size,
    )

    lifter_3d_factory: PoseLifter3DFactory | None = None
    if settings.pose.lifter_3d is not None:
        lifter_3d_ckpt: Path | None = (
            Path(settings.pose.lifter_3d_ckpt_path) if settings.pose.lifter_3d_ckpt_path else None
        )
        lifter_3d_input_size: tuple[int, int] | None = None
        if (
            settings.pose.lifter_3d_frame_w is not None
            and settings.pose.lifter_3d_frame_h is not None
        ):
            lifter_3d_input_size = (
                settings.pose.lifter_3d_frame_w,
                settings.pose.lifter_3d_frame_h,
            )
        lifter_3d_factory = PoseLifter3DFactory(
            model_name=settings.pose.lifter_3d,
            model_path=lifter_3d_ckpt,
            remote_url=settings.pose.lifter_3d_remote_url,
            input_size=lifter_3d_input_size,
            batch_size=settings.pose.batch_size,
        )

    ergo_factory: ErgoAssessorFactory | None = None
    if settings.pose.ergo_assessor is not None:
        ergo_factory = ErgoAssessorFactory(
            model_name=settings.pose.ergo_assessor,
            confidence_threshold=settings.pose.ergo_confidence_threshold,
        )

    default_config: PosePerceptionSessionConfig = PosePerceptionSessionConfig()
    if settings.pose.confidence_threshold_2d is not None:
        default_config.confidence_threshold_2d = settings.pose.confidence_threshold_2d
    if settings.pose.min_valid_keypoints is not None:
        default_config.min_valid_keypoints = settings.pose.min_valid_keypoints

    return PosePerception(
        estimator_2d_factory=estimator_2d_factory,
        lifter_3d_factory=lifter_3d_factory,
        ergo_assessor_factory=ergo_factory,
        default_config=default_config,
        batch_size=settings.pose.batch_size,
        batch_timeout=settings.pose.batch_timeout,
    )


def build_object_perceptions() -> dict[str, ObjectPerception]:
    """Create one ObjectPerception per enabled detector from settings."""
    detector_settings: dict[ObjectDetectorEnum, Any] = {
        ObjectDetectorEnum.YOLO_WORLD: settings.object_detector.yolo_world,
        ObjectDetectorEnum.OWLV2: settings.object_detector.owlv2,
    }

    perceptions: dict[str, ObjectPerception] = {}
    for name, det_settings in detector_settings.items():
        if not det_settings.enabled:
            continue

        model_path: Path | None = Path(det_settings.model_path) if det_settings.model_path else None
        classes_path: Path | None = (
            Path(det_settings.classes_path) if det_settings.classes_path else None
        )

        factory = ObjectDetectorFactory(
            model_name=name,
            use_onnx=det_settings.use_onnx,
            model_path=model_path,
            remote_url=det_settings.remote_url,
            classes_path=classes_path,
            threshold=det_settings.threshold,
            batch_size=det_settings.batch_size,
        )

        default_config: ObjectPerceptionSessionConfig = ObjectPerceptionSessionConfig()
        if det_settings.threshold is not None:
            default_config.threshold = det_settings.threshold

        perceptions[name.value] = ObjectPerception(
            object_detector_factory=factory,
            default_config=default_config,
            batch_size=det_settings.batch_size,
            batch_timeout=det_settings.batch_timeout,
        )

    return perceptions


def build_audio_embedder() -> AudioEmbedder:
    """Create an AudioEmbedder from settings."""
    model_path: Path | None = (
        Path(settings.audio_embedder.model_path) if settings.audio_embedder.model_path else None
    )

    proc = settings.audio_embedder.processor
    processor_factory = AudioProcessorFactory(
        target_sample_rate=proc.target_sample_rate,
        enable_mono=proc.enable_mono,
        enable_resample=proc.enable_resample,
        enable_high_pass=proc.enable_high_pass,
        high_pass_cutoff_hz=proc.high_pass_cutoff_hz,
        enable_noise_reduce=proc.enable_noise_reduce,
        noise_reduce_stationary=proc.noise_reduce_stationary,
        enable_vad=proc.enable_vad,
        vad_min_duration_sec=proc.vad_min_duration_sec,
        vad_min_voice_ratio=proc.vad_min_voice_ratio,
        enable_rms_normalize=proc.enable_rms_normalize,
        rms_target=proc.rms_target,
    )

    return create_embedder(
        model_name=settings.audio_embedder.model,
        model_path=model_path,
        remote_url=settings.audio_embedder.remote_url,
        processor_factory=processor_factory,
        batch_size=settings.audio_embedder.batch_size,
    )


def build_audio_emotion_perception() -> AudioEmotionPerception:
    """Create AudioEmotionPerception from settings."""
    ckpt_path: Path | None = (
        Path(settings.ser.ckpt_path) if settings.ser.ckpt_path else None
    )

    factory = AudioEmotionRecognizerFactory(
        model_name=settings.ser.model,
        model_path=ckpt_path,
        remote_url=settings.ser.remote_url,
        batch_size=settings.ser.batch_size,
    )

    return AudioEmotionPerception(
        audio_emotion_recognizer_factory=factory,
        batch_size=settings.ser.batch_size,
        batch_timeout=settings.ser.batch_timeout,
    )
