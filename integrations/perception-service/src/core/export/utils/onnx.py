"""ONNX post-export utilities."""

import logging
from pathlib import Path

import onnx
from onnx import shape_inference

logger = logging.getLogger(__name__)


def run_shape_inference(model_path: Path) -> None:
    """Run ONNX shape inference in-place; TensorRT needs known intermediate shapes."""
    logger.info("Running shape inference on %s", model_path.name)
    model = onnx.load(str(model_path))
    model = shape_inference.infer_shapes(model, data_prop=True)
    onnx.save(model, str(model_path))
    logger.info("Shape inference complete for %s", model_path.name)
