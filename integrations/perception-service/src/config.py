"""Application configuration loaded from environment variables."""

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.enums import (
    EmotionRecognizerEnum,
    HumanActionRecognizerEnum,
    PersonDetectorEnum,
    PoseEstimator2DEnum,
    SpeechEmotionRecognizerEnum,
)
from core.enums.audio import AudioEmbedderEnum
from core.enums.pose import ErgoAssessorEnum, PoseLifter3DEnum


class PersonDetectorSetting(BaseModel):
    enabled: bool = True
    model: PersonDetectorEnum = PersonDetectorEnum.YOLO
    model_name: str = "yolo12x.pt"
    confidence_threshold: float = 0.4
    bbox_expand_scale: float = 2.0
    min_area_ratio: float = 0.25  # skip persons covering less than 1/4 of frame
    batch_size: int | None = None
    batch_timeout: float | None = None


class ActionSetting(BaseModel):
    enabled: bool = True
    model: HumanActionRecognizerEnum = HumanActionRecognizerEnum.UNIFORMERV2
    ckpt_path: str | None = None
    remote_url: str | None = None
    confidence_threshold: float | None = None
    max_frames: int | None = None
    frame_interval: float | None = None
    w: int | None = None
    h: int | None = None
    batch_size: int | None = None
    batch_timeout: float | None = None


class FERSetting(BaseModel):
    enabled: bool = True
    model: EmotionRecognizerEnum = EmotionRecognizerEnum.POSTERV2
    ckpt_path: str | None = None
    remote_url: str | None = None
    confidence_threshold: float | None = None
    frame_interval: float | None = None
    label_thresholds: dict[str, float] | None = None
    batch_size: int | None = None
    batch_timeout: float | None = None


class PoseSetting(BaseModel):
    enabled: bool = True
    model: PoseEstimator2DEnum = PoseEstimator2DEnum.RTMPOSE
    ckpt_path: str | None = None
    remote_url: str | None = None
    confidence_threshold_2d: float | None = None
    min_valid_keypoints: int | None = None
    lifter_3d: PoseLifter3DEnum | None = PoseLifter3DEnum.TCPFORMER
    lifter_3d_ckpt_path: str | None = None
    lifter_3d_remote_url: str | None = None
    lifter_3d_frame_w: int | None = None
    lifter_3d_frame_h: int | None = None
    ergo_assessor: ErgoAssessorEnum | None = ErgoAssessorEnum.RULA
    ergo_confidence_threshold: float | None = None
    batch_size: int | None = None
    batch_timeout: float | None = None


class SERSetting(BaseModel):
    enabled: bool = True
    model: SpeechEmotionRecognizerEnum = SpeechEmotionRecognizerEnum.EMOTION2VEC
    ckpt_path: str | None = None
    remote_url: str | None = None
    labels_path: str | None = None
    batch_size: int | None = None
    batch_timeout: float | None = None


class AudioProcessorSetting(BaseModel):
    target_sample_rate: int = 16000
    enable_mono: bool = True
    enable_resample: bool = True
    enable_high_pass: bool = False
    high_pass_cutoff_hz: float = 80.0
    enable_noise_reduce: bool = False
    noise_reduce_stationary: bool = False
    enable_vad: bool = True
    vad_min_duration_sec: float = 0.5
    vad_min_voice_ratio: float = 0.4
    enable_rms_normalize: bool = True
    rms_target: float = 0.1


class AudioEmbedderSetting(BaseModel):
    enabled: bool = True
    model: AudioEmbedderEnum = AudioEmbedderEnum.RESNET293
    model_path: str | None = None
    remote_url: str | None = None
    batch_size: int | None = None
    batch_timeout: float | None = None
    processor: AudioProcessorSetting = AudioProcessorSetting()


class CryptoSetting(BaseModel):
    enabled: bool = True
    key_dir: Path = Path.home() / ".perception-service" / "keys"
    key_size: int = 2048
    require_encryption: bool = False  # reject plain payloads if True


class SingleObjectDetectorSetting(BaseModel):
    enabled: bool = False
    use_onnx: bool = True
    model_path: str | None = None
    remote_url: str | None = None
    classes_path: str | None = None
    threshold: float | None = None
    batch_size: int | None = None
    batch_timeout: float | None = None


class ObjectDetectorSetting(BaseModel):
    yolo_world: SingleObjectDetectorSetting = SingleObjectDetectorSetting(enabled=True)
    owlv2: SingleObjectDetectorSetting = SingleObjectDetectorSetting(enabled=True)


class InputLimitSetting(BaseModel):
    max_image_bytes: int = 50 * 1024 * 1024  # 50 MB decoded
    max_image_dim: int = 4096  # px per side
    max_audio_bytes: int = 20 * 1024 * 1024  # 20 MB decoded
    max_audio_duration_s: float = 60.0  # seconds
    max_audio_batch: int = 16  # max items in a single audio batch request


class LBSetting(BaseModel):
    backends: str = ""  # comma-separated backend URLs, e.g. "http://127.0.0.1:8888"
    host: str = "0.0.0.0"
    port: int = 7999
    internal_prefix: str = ""
    # Must stay below nginx proxy_read_timeout (45s) so the LB fails first with a clear 504.
    http_timeout: float = 30.0  # seconds, httpx read/write/pool timeout for backend requests
    # Mirrors nginx proxy_connect_timeout 5s.
    connect_timeout: float = 5.0  # seconds, httpx connect timeout
    ws_open_timeout: float = 30.0  # seconds, websocket handshake timeout to backend

    # Bounds requests a hung backend can tie up; past it httpx PoolTimeout surfaces as 504.
    max_connections: int = 100
    max_keepalive: int = 20

    # Two-slot deploy (scripts/deploy-dlserver.sh). When set, lbserver replaces the
    # loopback entry of `backends` with the URL in this file, at startup and on
    # SIGHUP, and records what it applied in "<state_file>.applied". Empty = off,
    # which is exactly the single-dlserver behaviour.
    state_file: str = ""


class Settings(BaseSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env", env_nested_delimiter="__", extra="allow"
    )

    dl_api_key: str = ""

    @field_validator("dl_api_key")
    @classmethod
    def require_api_key(cls, v: str) -> str:
        if not v:
            raise ValueError("DL_API_KEY must be set — server refuses to start without auth")
        return v

    cache_dir: Path = Path.home() / ".cache" / "perception-service"
    model_cache_dir: Path = Path.home() / ".cache" / "perception-service" / "models"  # default: cache_dir / "models"
    cdn_base: str = "https://storage.googleapis.com/autonomous-models"

    action: ActionSetting = ActionSetting()
    fer: FERSetting = FERSetting()
    ser: SERSetting = SERSetting()
    pose: PoseSetting = PoseSetting()
    person_detector: PersonDetectorSetting = PersonDetectorSetting()
    object_detector: ObjectDetectorSetting = ObjectDetectorSetting()
    audio_embedder: AudioEmbedderSetting = AudioEmbedderSetting()

    input_limits: InputLimitSetting = InputLimitSetting()

    crypto: CryptoSetting = CryptoSetting()
    lb: LBSetting = LBSetting()


settings = Settings()
