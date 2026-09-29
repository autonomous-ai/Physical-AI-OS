"""ResNet293 speaker embedding model."""

from pathlib import Path

from core.enums.audio import AudioEmbedderEnum
from core.enums.files import ModelEnum
from core.perception.audio.predictors.base import AudioEmbedder
from core.utils.files import get_default_cdn_url, get_default_model_path


class ResNet293Embedder(AudioEmbedder):
    """WeSpeaker ResNet293-LM speaker embedder (256-dim)."""

    MODEL_NAME: AudioEmbedderEnum | None = AudioEmbedderEnum.RESNET293
    DEFAULT_MODEL_PATH: Path | None = get_default_model_path(ModelEnum.WESPEAKER_RESNET293)
    DEFAULT_REMOTE_URL: str | None = get_default_cdn_url(ModelEnum.WESPEAKER_RESNET293)
