"""HTTP endpoints for audio embedding service."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from core.models.media import Audio
from core.perception.audio.processors.exceptions import PreprocessRejected
from dlserver.models.audio import (
    EmbedAudioRequest,
    EmbedAudioResponse,
)
from dlserver.utils.audio import decode_b64_wav
from dlserver.utils.state import get_audio_embedder

logger: logging.Logger = logging.getLogger(__name__)

router = APIRouter(tags=["audio-recognizer"])


@router.post("/audio-recognizer/embed", response_model=EmbedAudioResponse)
async def embed_audio(req: EmbedAudioRequest):
    """Return per-chunk and/or aggregated L2-normalized embeddings (stateless)."""
    embedder = get_audio_embedder()
    if embedder is None:
        raise HTTPException(status_code=503, detail="Audio embedder is unavailable")

    try:
        audios: list[Audio] = []
        for item in req.audios_b64:
            audios.append(decode_b64_wav(item))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid audio format")

    if not audios:
        raise HTTPException(status_code=400, detail="No audio extracted from inputs")

    audio_durations_s = [round(a.waveform.shape[0] / a.sample_rate, 2) for a in audios]

    try:
        results = await asyncio.to_thread(
            embedder.predict,
            audios,
            preprocess=req.preprocess,
            use_sliding_window=req.use_sliding_window,
        )
    except PreprocessRejected as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as exc:
        logger.exception(
            "Audio embedding inference failed (num_audios=%d, durations_s=%s, use_sliding_window=%s)",
            len(audios), audio_durations_s, req.use_sliding_window,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Audio embedding inference failed: {type(exc).__name__}: {exc}",
        ) from exc

    try:
        return EmbedAudioResponse.from_raw_embedding(
            results[0],
            use_sliding_window=req.use_sliding_window,
            embed_model_version=embedder.model_version,
        )
    except Exception as exc:
        logger.exception(
            "Failed to build audio embedding response (num_audios=%d, durations_s=%s, use_sliding_window=%s)",
            len(audios), audio_durations_s, req.use_sliding_window,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to format audio embedding response: {type(exc).__name__}: {exc}",
        ) from exc
