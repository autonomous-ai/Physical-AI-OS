"""Export OWLv2 zero-shot object detector (with text encoder) to ONNX.

Inputs: images [B, 3, H, W], class_tokens [Q, seq_len].
Outputs: boxes [B, D, 4] cxcywh in [0,1], probs [B, D, Q], labels [B, D] (-1 = padding).
"""

import argparse
import logging
from pathlib import Path

import torch
from transformers import Owlv2ForObjectDetection, Owlv2Processor
from typing_extensions import override

from core.enums.files import ModelEnum
from core.export.utils.evaluation import evaluate_image
from core.export.utils.nms import onnx_nms, xyxy_to_xywh_normalized
from core.export.utils.onnx import run_shape_inference
from core.utils.files import get_default_model_path

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class OWLv2ONNX(torch.nn.Module):
    """Wraps OWLv2 model into the same interface as YOLO exports."""

    def __init__(self, model: Owlv2ForObjectDetection, nms: bool = False):
        super().__init__()
        self.model = model
        self.nms = nms

    @override
    def forward(self, images: torch.Tensor, class_tokens: torch.Tensor):
        attention_mask = (class_tokens != 0).long()

        outputs = self.model(
            input_ids=class_tokens,
            attention_mask=attention_mask,
            pixel_values=images,
        )

        pred_boxes = outputs.pred_boxes  # [B, P, 4] cxcywh
        logits = outputs.logits  # [B, P, Q]

        probs = logits.sigmoid()

        # cxcywh → xyxy
        cx, cy, w, h = pred_boxes.unbind(-1)
        boxes_xyxy = torch.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], dim=-1)

        if self.nms:
            return onnx_nms(boxes_xyxy, probs, input_hw=(1, 1), conf_threshold=0.1)

        xywh = xyxy_to_xywh_normalized(boxes_xyxy, input_hw=(1, 1))
        _, labels = probs.max(dim=-1)
        return xywh, probs, labels


def export(model_id: str, output: str | None = None, opset: int = 17, nms: bool = False):
    model_enum = ModelEnum.OWLV2_NMS_ONNX if nms else ModelEnum.OWLV2_ONNX
    output = output or str(get_default_model_path(model_enum))
    dest = Path(output).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Loading model from {model_id}...")
    model = Owlv2ForObjectDetection.from_pretrained(model_id)
    model.eval()

    wrapper = OWLv2ONNX(model, nms=nms)
    wrapper.eval()

    processor = Owlv2Processor.from_pretrained(model_id)
    dummy_text = ["a photo of a cat", "a photo of a dog", "a photo of a person"]
    dummy_inputs = processor(
        text=dummy_text, images=[torch.zeros(3, 960, 960)], return_tensors="pt"
    )

    dummy_images = dummy_inputs["pixel_values"]  # [1, 3, 960, 960]
    dummy_class_tokens = dummy_inputs["input_ids"]  # [3, 16]

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
        wrapper, dest, dummy_images.shape[-2:],
        original_kwargs={"class_tokens": dummy_class_tokens},
        onnx_kwargs={"class_tokens": dummy_class_tokens.cpu().numpy()},
    )

    logger.info("Verification:")
    for i, e in enumerate(errors):
        logger.info(f"\tChannel {i}: mean_err = {e[0]:.6f} | max_err = {e[1]:.6f}")


def entry():
    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser(description="Export OWLv2 to ONNX")
    parser.add_argument(
        "--model-id",
        default="google/owlv2-large-patch14-ensemble",
        help="HuggingFace model identifier",
    )
    parser.add_argument("--output", default=None)
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--nms", action="store_true", default=False)
    parser.add_argument("--no-nms", dest="nms", action="store_false")
    args = parser.parse_args()

    export(args.model_id, args.output, args.opset, args.nms)
