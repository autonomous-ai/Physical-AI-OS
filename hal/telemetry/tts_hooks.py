"""Playback hooks for every TTSService instance (built at boot and on every /voice/start).

Measurement only; never raises into the audio path.
"""

import logging

logger = logging.getLogger("hal.telemetry")


def on_playback_audio(owner: str) -> None:
    """First real audio frame of a playback reached the stream."""
    try:
        from hal import app_state as state
        from hal.telemetry import voice_metrics

        voice_metrics.playback_audio(owner, state.tts_service)
    except Exception:
        logger.exception("[voice-metrics] playback audio hook failed")


def on_playback_muted(owner: str) -> None:
    """Speech was refused because the speaker is muted."""
    try:
        from hal.telemetry import voice_metrics

        voice_metrics.playback_muted(owner)
    except Exception:
        logger.exception("[voice-metrics] playback muted hook failed")


def on_playback_done() -> None:
    """Playback finished or was interrupted."""
    try:
        from hal.telemetry import voice_metrics

        voice_metrics.playback_end()
    except Exception:
        logger.exception("[voice-metrics] playback done hook failed")
