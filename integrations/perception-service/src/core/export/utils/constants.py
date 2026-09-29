from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[4]

# Weights and export artifacts (outside src/)
MODELS_DIR = PROJECT_ROOT / "checkpoints"
PRETRAINED_DIR = MODELS_DIR / "pretrained"
ONNX_DIR = MODELS_DIR / "onnx"

# Export dependencies and evaluation data
DEPS_DIR = PROJECT_ROOT / "deps"
DATA_DIR = PROJECT_ROOT / "data"
IMAGES_DIR = DATA_DIR / "images"
AUDIO_DIR = DATA_DIR / "audio"
