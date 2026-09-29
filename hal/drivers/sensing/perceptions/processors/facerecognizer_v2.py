"""Back-compat shim for the v2 face pipeline."""

from .faceid import FacePerception, FaceRecognizer, USERS_DIR

__all__ = ["FacePerception", "FaceRecognizer", "USERS_DIR"]
