"""Export YOLO person detector to ONNX."""

import argparse
import logging
from copy import deepcopy
from pathlib import Path

import torch
from typing_extensions import override
from ultralytics import YOLO

from core.enums.files import ModelEnum
from core.export.utils.evaluation import evaluate_image
from core.export.utils.nms import onnx_nms, xyxy_to_xywh_normalized
from core.export.utils.onnx import run_shape_inference
from core.utils.files import ensure_downloaded, get_default_cdn_url, get_default_model_path

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class YOLOONNX(torch.nn.Module):
    def __init__(self, model: torch.nn.Module, nms: bool = False):
        super().__init__()
        self.model = model
        self.nms = nms

    @override
    def forward(self, x: torch.Tensor):
        raw = self.model(x)  # [B, 4 + nc, A]
        boxes_xywh = raw[:, :4, :].permute(0, 2, 1)  # [B, A, 4]
        scores = raw[:, 4:, :].permute(0, 2, 1)  # [B, A, K]

        # xywh → xyxy
        xy = boxes_xywh[..., :2]
        wh = boxes_xywh[..., 2:]
        boxes_xyxy = torch.cat([xy - wh / 2, xy + wh / 2], dim=-1)

        if self.nms:
            return onnx_nms(boxes_xyxy, scores, input_hw=(x.shape[2], x.shape[3]))

        xywh = xyxy_to_xywh_normalized(boxes_xyxy, input_hw=(x.shape[2], x.shape[3]))
        _, labels = scores.max(dim=-1)
        return xywh, scores, labels


def export(checkpoint: str | None = None, output: str | None = None, imgsz: int = 640, opset: int = 17, nms: bool = False):
    model_enum = ModelEnum.YOLO_PERSON_NMS_ONNX if nms else ModelEnum.YOLO_PERSON_ONNX
    output = output or str(get_default_model_path(model_enum))
    dest = Path(output).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if checkpoint is None:
        model_path = get_default_model_path(ModelEnum.YOLO_PERSON_PTH)
        remote_url = get_default_cdn_url(ModelEnum.YOLO_PERSON_PTH)
        checkpoint = str(ensure_downloaded(model_path, remote=remote_url))

    logger.info(f"Loading YOLO from {checkpoint}")
    yolo = YOLO(checkpoint)

    net = deepcopy(yolo.model).float()
    net.eval()
    net = net.fuse()
    for m in net.modules():
        if hasattr(m, "export"):
            m.export = True

    wrapper = YOLOONNX(net, nms=nms)
    wrapper.eval()

    dummy = torch.zeros(1, 3, imgsz, imgsz)

    if nms:
        output_names = ["boxes", "probs", "labels"]
        dynamic_axes = {
            "images": {0: "batch", 2: "height", 3: "width"},
            "boxes": {0: "batch", 1: "num_det"},
            "probs": {0: "batch", 1: "num_det"},
            "labels": {0: "batch", 1: "num_det"},
        }
    else:
        output_names = ["boxes", "probs", "labels"]
        dynamic_axes = {
            "images": {0: "batch", 2: "height", 3: "width"},
            "boxes": {0: "batch"},
            "probs": {0: "batch"},
        }

    logger.info(f"Exporting to {dest} (nms={nms})...")
    torch.onnx.export(
        wrapper,
        dummy,
        str(dest),
        input_names=["images"],
        output_names=output_names,
        dynamic_axes=dynamic_axes,
        opset_version=opset,
        do_constant_folding=True,
    )
    run_shape_inference(dest)

    size_mb = dest.stat().st_size / 1024 / 1024
    logger.info(f"Exported to {dest} ({size_mb:.1f} MB)")

    errors = evaluate_image(wrapper, dest, dummy.shape[-2:])

    logger.info("Verification:")
    for i, e in enumerate(errors):
        logger.info(f"\tChannel {i}: mean_err = {e[0]:.6f} | max_err = {e[1]:.6f}")


def entry():
    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser(description="Export YOLO to ONNX")
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--nms", action="store_true", default=False)
    parser.add_argument("--no-nms", dest="nms", action="store_false")
    args = parser.parse_args()

    export(args.checkpoint, args.output, args.imgsz, args.opset, args.nms)
