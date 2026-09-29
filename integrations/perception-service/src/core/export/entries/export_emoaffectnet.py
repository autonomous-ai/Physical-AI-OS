"""Export Emo-AffectNet static ResNet-50 (7-class AffectNet) to ONNX.

The native preprocessing (uint8 RGB -> BGR, subtract BGR means, no std) and softmax
are baked in, so the graph takes RGB NCHW in [0,1] and outputs probabilities.
"""

import argparse
import collections
import logging
from pathlib import Path

import torch
from typing_extensions import override

from core.enums.files import ModelEnum
from core.export.components.emoaffectnet import ResNet50
from core.export.utils.evaluation import evaluate_image
from core.export.utils.onnx import run_shape_inference
from core.utils.files import ensure_downloaded, get_default_cdn_url, get_default_model_path

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Canonical source of the static ResNet-50 checkpoint (MIT).
HF_CHECKPOINT_URL: str = (
    "https://huggingface.co/ElenaRyumina/face_emotion_recognition/"
    "resolve/main/FER_static_ResNet50_AffectNet.pt"
)


class EmoAffectNetONNX(torch.nn.Module):
    """Wraps ResNet-50 with its native preprocessing + softmax. Input: (N, 3, H, W) RGB in [0, 1]."""

    # Per-channel means in BGR order, exactly as the reference subtracts them.
    MEAN_BGR: list[float] = [91.4953, 103.8827, 131.0912]

    def __init__(self, net: torch.nn.Module):
        super().__init__()
        self.net: torch.nn.Module = net
        self.register_buffer("mean", torch.tensor(self.MEAN_BGR).view(1, 3, 1, 1))

    @override
    def forward(self, x: torch.Tensor):
        x = x * 255.0
        x = torch.flip(x, dims=[1])  # RGB -> BGR
        x = x - self.mean
        return torch.softmax(self.net(x), dim=-1)


def export(checkpoint: str | None = None, output: str | None = None, num_classes: int = 7, opset: int = 17):
    output = output or str(get_default_model_path(ModelEnum.EMOAFFECTNET_ONNX))
    dest = Path(output).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if checkpoint is None:
        model_path = get_default_model_path(ModelEnum.EMOAFFECTNET_PTH)
        remote_url = get_default_cdn_url(ModelEnum.EMOAFFECTNET_PTH) or HF_CHECKPOINT_URL
        checkpoint = str(ensure_downloaded(model_path, remote=remote_url))

    ckpt = torch.load(checkpoint, map_location="cpu", weights_only=False)
    state_dict = ckpt.get("state_dict", ckpt) if isinstance(ckpt, dict) else ckpt.state_dict()
    clean = collections.OrderedDict()
    for k, v in state_dict.items():
        clean[k.removeprefix("module.")] = v

    net = ResNet50(num_classes, channels=3)
    missing, unexpected = net.load_state_dict(clean, strict=False)
    if missing:
        logger.warning(f"missing keys ({len(missing)}): {missing[:5]}...")
    if unexpected:
        logger.warning(f"unexpected keys ({len(unexpected)}): {unexpected[:5]}...")
    net.eval()

    wrapper = EmoAffectNetONNX(net)
    wrapper.eval()

    dummy = torch.rand(1, 3, 224, 224)

    logger.info(f"Exporting to {dest}...")
    torch.onnx.export(
        wrapper,
        dummy,
        str(dest),
        opset_version=opset,
        input_names=["images"],
        output_names=["probs"],
        dynamic_axes={"images": {0: "batch"}, "probs": {0: "batch"}},
    )
    run_shape_inference(dest)

    size_mb = dest.stat().st_size / 1024 / 1024
    logger.info(f"Exported to {dest} ({size_mb:.1f} MB)")

    errors = evaluate_image(wrapper, dest, input_size=(224, 224))

    logger.info("Verification:")
    for i, e in enumerate(errors):
        logger.info(f"\tChannel {i}: mean_err = {e[0]:.6f} | max_err = {e[1]:.6f}")


def entry():
    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser(description="Export Emo-AffectNet static ResNet-50 to ONNX")
    parser.add_argument("--checkpoint", default=None, help="Path to FER_static_ResNet50_AffectNet.pt (downloads from HF if omitted)")
    parser.add_argument("--output", default=None)
    parser.add_argument("--num-classes", type=int, default=7)
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    export(args.checkpoint, args.output, args.num_classes, args.opset)
