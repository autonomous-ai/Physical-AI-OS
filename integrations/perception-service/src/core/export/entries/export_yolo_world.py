"""Export YOLO-World zero-shot object detector (with CLIP text encoder) to ONNX.

Inputs: images [B, 3, H, W], class_tokens [K, context_len].
Outputs: boxes [B, D, 4] cxcywh in [0,1], probs [B, D, K], labels [B, D] (-1 = padding).
"""

import argparse
import logging
from copy import deepcopy
from pathlib import Path
from typing_extensions import override

import torch
import torch.nn.functional as F
from ultralytics import YOLOWorld
from ultralytics.nn.text_model import build_text_model

from core.enums.files import ModelEnum
from core.export.utils.evaluation import evaluate_image
from core.export.utils.nms import onnx_nms, xyxy_to_xywh_normalized
from core.export.utils.onnx import run_shape_inference
from core.utils.files import ensure_downloaded, get_default_cdn_url, get_default_model_path

_PATCHED = False


def _patch_world_detect():
    """Monkey-patch WorldDetect.forward to infer `no` from the tensor shape so num_classes stays dynamic in ONNX."""
    global _PATCHED
    if _PATCHED:
        return
    from ultralytics.nn.modules.head import WorldDetect

    def _forward(self, x, text):
        feats = [xi.clone() for xi in x]
        for i in range(self.nl):
            x[i] = torch.cat((self.cv2[i](x[i]), self.cv4[i](self.cv3[i](x[i]), text)), 1)

        bs = x[0].shape[0]
        no = x[0].shape[1]  # dynamic — inferred from actual tensor
        x_cat = torch.cat([xi.view(bs, no, -1) for xi in x], 2)

        nc = no - self.reg_max * 4
        boxes, scores = x_cat.split((self.reg_max * 4, nc), 1)
        preds = dict(boxes=boxes, scores=scores, feats=feats)
        return self._inference(preds)

    WorldDetect.forward = _forward
    _PATCHED = True


logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class YOLOWorldONNX(torch.nn.Module):
    """Wraps WorldModel + CLIP text encoder into a single traceable module."""

    def __init__(
        self, world_model: torch.nn.Module, clip_text_encoder: torch.nn.Module, nms: bool = False
    ):
        super().__init__()
        self.world_model = world_model
        self.clip_text_encoder = clip_text_encoder
        self.nms = nms

    @override
    def forward(self, images: torch.Tensor, class_tokens: torch.Tensor):
        txt_feats = self.clip_text_encoder.encode_text(class_tokens)
        txt_feats = F.normalize(txt_feats, p=2, dim=-1)
        txt_feats = txt_feats.unsqueeze(0)

        raw = self.world_model.predict(images, txt_feats=txt_feats)
        boxes_xywh = raw[:, :4, :].permute(0, 2, 1)
        scores = raw[:, 4:, :].permute(0, 2, 1)

        xy = boxes_xywh[..., :2]
        wh = boxes_xywh[..., 2:]
        boxes_xyxy = torch.cat([xy - wh / 2, xy + wh / 2], dim=-1)

        if self.nms:
            return onnx_nms(boxes_xyxy, scores, input_hw=(images.shape[2], images.shape[3]))
        xywh = xyxy_to_xywh_normalized(boxes_xyxy, input_hw=(images.shape[2], images.shape[3]))
        _, labels = scores.max(dim=-1)
        return xywh, scores, labels


def export(checkpoint: str | None = None, output: str | None = None, imgsz: int = 640, opset: int = 17, nms: bool = False):
    model_enum = ModelEnum.YOLO_WORLD_NMS_ONNX if nms else ModelEnum.YOLO_WORLD_ONNX
    output = output or str(get_default_model_path(model_enum))
    dest = Path(output).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if checkpoint is None:
        model_path = get_default_model_path(ModelEnum.YOLO_WORLD_PTH)
        remote_url = get_default_cdn_url(ModelEnum.YOLO_WORLD_PTH)
        checkpoint = str(ensure_downloaded(model_path, remote=remote_url))

    _patch_world_detect()

    logger.info(f"Loading YOLO-World from {checkpoint}")
    yolo = YOLOWorld(checkpoint)

    net = deepcopy(yolo.model).float()
    net.eval()
    net = net.fuse()
    for m in net.modules():
        if hasattr(m, "export"):
            m.export = True

    text_model_name = getattr(net, "text_model", None) or "clip:ViT-B/32"
    logger.info(f"Building text encoder: {text_model_name}")
    clip_model = build_text_model(text_model_name)

    wrapper = YOLOWorldONNX(net, clip_model, nms=nms)
    wrapper.eval()

    dummy_images = torch.zeros(1, 3, imgsz, imgsz)
    dummy_class_tokens = clip_model.tokenize(["person", "car", "dog"])

    if nms:
        output_names = ["boxes", "probs", "labels"]
        dynamic_axes = {
            "images": {0: "batch", 2: "height", 3: "width"},
            "class_tokens": {0: "num_classes"},
            "boxes": {0: "batch", 1: "num_det"},
            "probs": {0: "batch", 1: "num_det", 2: "num_classes"},
            "labels": {0: "batch", 1: "num_det"},
        }
    else:
        output_names = ["boxes", "probs", "labels"]
        dynamic_axes = {
            "images": {0: "batch", 2: "height", 3: "width"},
            "class_tokens": {0: "num_classes"},
            "boxes": {0: "batch"},
            "probs": {0: "batch", 2: "num_classes"},
        }

    logger.info(f"Exporting to {dest} (nms={nms})...")
    with torch.no_grad():
        torch.onnx.export(
            wrapper,
            (dummy_images, dummy_class_tokens),
            str(dest),
            input_names=["images", "class_tokens"],
            output_names=output_names,
            dynamic_axes=dynamic_axes,
            opset_version=opset,
            do_constant_folding=True,
        )
    run_shape_inference(dest)

    size_mb = dest.stat().st_size / 1024 / 1024
    logger.info(f"Exported to {dest} ({size_mb:.1f} MB)")

    errors = evaluate_image(
        wrapper,
        dest,
        dummy_images.shape[-2:],
        original_kwargs={"class_tokens": dummy_class_tokens},
        onnx_kwargs={"class_tokens": dummy_class_tokens.cpu().numpy()},
    )

    logger.info("Verification:")
    for i, e in enumerate(errors):
        logger.info(f"\tChannel {i}: mean_err = {e[0]:.6f} | max_err = {e[1]:.6f}")


def entry():
    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser(description="Export YOLO-World to ONNX")
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--nms", action="store_true", default=False)
    parser.add_argument("--no-nms", dest="nms", action="store_false")
    args = parser.parse_args()

    export(args.checkpoint, args.output, args.imgsz, args.opset, args.nms)
