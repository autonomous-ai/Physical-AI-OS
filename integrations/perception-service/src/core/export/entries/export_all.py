"""Run all ONNX exports. Detection models are exported twice: raw and with NMS baked in."""

import logging
from pathlib import Path

from . import (
    export_emoaffectnet,
    export_emonet,
    export_emotion2vec,
    export_owlv2,
    export_posterv2,
    export_tcpformer,
    export_uniformerv2,
    export_yolo,
    export_yolo_world,
)

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _run(name: str, fn, required: bool = True) -> bool:
    logger.info("=" * 60)
    logger.info("Running: %s", name)
    logger.info("=" * 60)
    try:
        fn()
        return True
    except Exception as e:
        if required:
            logger.exception("FAILED: %s", name)
            return False
        logger.warning("SKIPPED (optional): %s with error=%s", name, e)
        return True


def _output(output_dir: Path | None, filename: str) -> str | None:
    """Build output path if output_dir is set, otherwise None (use default)."""
    if output_dir is None:
        return None
    return str(output_dir / filename)


def export_all(output_dir: Path | None = None, opset: int = 17):
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, bool] = {}

    for n in (5, 8):
        results[f"emonet_{n}"] = _run(
            f"emonet_{n}",
            lambda n=n: export_emonet.export(
                n, output=_output(output_dir, f"emonet_{n}.onnx"), opset=opset,
            ),
        )

    results["posterv2"] = _run(
        "posterv2",
        lambda: export_posterv2.export(
            output=_output(output_dir, "posterv2_7cls.onnx"), opset=opset,
        ),
    )

    results["emoaffectnet"] = _run(
        "emoaffectnet",
        lambda: export_emoaffectnet.export(
            output=_output(output_dir, "emoaffectnet_resnet50.onnx"), opset=opset,
        ),
        required=False,
    )

    results["tcpformer"] = _run(
        "tcpformer",
        lambda: export_tcpformer.export(
            output=_output(output_dir, "tcpformer_h36m_243.onnx"), opset=opset,
        ),
    )

    results["emotion2vec"] = _run(
        "emotion2vec",
        lambda: export_emotion2vec.export(
            "emotion2vec/emotion2vec_plus_large",
            output=_output(output_dir, "emotion2vec.onnx"),
            opset=opset,
        ),
    )

    results["uniformerv2"] = _run(
        "uniformerv2",
        lambda: export_uniformerv2.export(
            "large-k400",
            output=_output(output_dir, "uniformerv2-l-224-k400_fp32.onnx"),
            opset=opset,
        ),
    )

    results["owlv2_raw"] = _run(
        "owlv2 (raw)",
        lambda: export_owlv2.export(
            "google/owlv2-large-patch14-ensemble",
            output=_output(output_dir, "owlv2_raw.onnx"),
            opset=opset, nms=False,
        ),
        required=True,
    )
    results["owlv2_nms"] = _run(
        "owlv2 (nms)",
        lambda: export_owlv2.export(
            "google/owlv2-large-patch14-ensemble",
            output=_output(output_dir, "owlv2.onnx"),
            opset=opset, nms=True,
        ),
        required=True,
    )

    results["yolo_raw"] = _run(
        "yolo (raw)",
        lambda: export_yolo.export(
            output=_output(output_dir, "yolo12x_raw.onnx"),
            opset=opset, nms=False,
        ),
        required=True,
    )
    results["yolo_nms"] = _run(
        "yolo (nms)",
        lambda: export_yolo.export(
            output=_output(output_dir, "yolo12x.onnx"),
            opset=opset, nms=True,
        ),
        required=True,
    )

    results["yolo_world_raw"] = _run(
        "yolo_world (raw)",
        lambda: export_yolo_world.export(
            output=_output(output_dir, "yolov8x-worldv2_raw.onnx"),
            opset=opset, nms=False,
        ),
        required=True,
    )
    results["yolo_world_nms"] = _run(
        "yolo_world (nms)",
        lambda: export_yolo_world.export(
            output=_output(output_dir, "yolov8x-worldv2.onnx"),
            opset=opset, nms=True,
        ),
        required=True,
    )

    logger.info("=" * 60)
    logger.info("Summary:")
    logger.info("=" * 60)
    for name, ok in results.items():
        status = "PASSED" if ok else "FAILED"
        logger.info("  %-25s %s", name, status)


def entry():
    import argparse

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Export all models to ONNX")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for ONNX outputs. If omitted, uses default model cache path.",
    )
    parser.add_argument(
        "--opset",
        type=int,
        default=17,
        help="ONNX opset version (default: 17).",
    )
    args = parser.parse_args()

    export_all(output_dir=args.output_dir, opset=args.opset)
