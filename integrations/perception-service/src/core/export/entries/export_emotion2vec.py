"""Export Emotion2Vec (FunASR) to ONNX with softmax appended."""

import argparse
import logging
from pathlib import Path

import torch
from typing_extensions import override

from core.enums.files import ModelEnum
from core.export.utils.evaluation import evaluate_audio
from core.export.utils.onnx import run_shape_inference
from core.utils.files import get_default_model_path

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _normalize_label(raw: str) -> str | None:
    """Extract English label from bilingual token (e.g. '开心/happy' → 'happy')."""
    line = raw.strip()
    if not line:
        return None
    if "/" in line:
        parts = [p.strip() for p in line.split("/") if p.strip()]
        if parts:
            return parts[-1]
    return line


def _materialize_labels(token_list: list[str], output_dir: Path):
    """Write labels.txt next to the ONNX file."""
    labels = [lbl for lbl in (_normalize_label(tok) for tok in token_list) if lbl]
    if not labels:
        logger.warning("No labels materialized. Provide labels.txt manually.")
        return None

    labels_dest = output_dir / "labels.txt"
    labels_dest.write_text("\n".join(labels) + "\n", encoding="utf-8")
    logger.info(f"Wrote {len(labels)} labels -> {labels_dest}")
    return labels_dest


class Emotion2VecONNX(torch.nn.Module):
    """Wraps FunASR emotion2vec for ONNX export, matching FunASR's inference path."""

    def __init__(self, model: torch.nn.Module, token_list: list[str]):
        super().__init__()
        self.model = model
        self.normalize = model.cfg.normalize

        # Build bias to mask "unused" tokens with -inf before softmax
        n = model.proj.out_features
        bias = torch.zeros(n, dtype=torch.float32)
        for i, lab in enumerate(token_list):
            if str(lab).startswith("unuse"):
                bias[i] = -1e4
        self.register_buffer("logit_bias", bias)

    @override
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # Instance norm on raw waveform (equivalent to F.layer_norm(x, x.shape)
        # but ONNX-traceable since normalized_shape must be static)
        if self.normalize:
            mean = torch.mean(x, dim=-1, keepdim=True)
            var = torch.var(x, dim=-1, keepdim=True, unbiased=False)
            x = (x - mean) / torch.sqrt(var + 1e-5)
        x = x.view(x.shape[0], -1)

        feats = self.model.extract_features(x, padding_mask=None)
        h = feats["x"]  # [B, T, D]

        z = h.mean(dim=1)  # [B, D]
        logits = self.model.proj(z)  # [B, C]
        logits = logits + self.logit_bias
        return torch.softmax(logits, dim=-1)


def export(model_id: str, output: str | None = None, opset: int = 17):
    output = output or str(get_default_model_path(ModelEnum.EMOTION2VEC_ONNX))
    dest = Path(output).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Loading model via FunASR (model_id={model_id})...")
    from funasr import AutoModel

    fm = AutoModel(model=model_id, hub="hf", disable_update=True)
    net = fm.model
    net.eval()

    token_list = fm.kwargs.get("tokenizer").token_list if fm.kwargs.get("tokenizer") else []
    if not token_list:
        logger.warning("No token list found — labels.txt will not be generated")

    if getattr(net, "proj", None) is None:
        raise RuntimeError(
            f"Model '{model_id}' has no classifier head (proj layer). "
            "Use a variant with a classifier (e.g. emotion2vec/emotion2vec_plus_large)."
        )

    wrapper = Emotion2VecONNX(net, token_list)
    wrapper.eval()

    # 30s dummy so ALiBi bias is traced large enough for real audio
    dummy = torch.randn(1, 16000 * 30)

    logger.info(f"Exporting to {dest}...")
    with torch.no_grad():
        torch.onnx.export(
            wrapper,
            (dummy,),
            str(dest),
            do_constant_folding=True,
            input_names=["audio"],
            output_names=["probs"],
            dynamic_axes={
                "audio": {0: "batch_size", 1: "sequence_length"},
                "probs": {0: "batch_size"},
            },
            opset_version=opset,
        )

    run_shape_inference(dest)
    _materialize_labels(token_list, dest.parent)

    size_mb = dest.stat().st_size / 1024 / 1024
    logger.info(f"Exported to {dest} ({size_mb:.1f} MB)")

    errors = evaluate_audio(wrapper, dest)

    logger.info("Verification:")
    for i, e in enumerate(errors):
        logger.info(f"\tChannel {i}: mean_err = {e[0]:.6f} | max_err = {e[1]:.6f}")


def entry():
    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser(description="Export Emotion2Vec SER to ONNX")
    parser.add_argument(
        "--model-id",
        default="emotion2vec/emotion2vec_plus_large",
        help="FunASR / HuggingFace model identifier",
    )
    parser.add_argument("--output", default=None)
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    export(args.model_id, args.output, args.opset)
