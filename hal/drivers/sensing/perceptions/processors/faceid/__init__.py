"""Face recognition processor — v2 pipeline (SCRFD + ONNX landmark + EdgeFace)."""

from .constants import USERS_DIR
from .perception import FacePerception
from .recognizer import FaceRecognizer

__all__ = ["FacePerception", "FaceRecognizer", "USERS_DIR"]
