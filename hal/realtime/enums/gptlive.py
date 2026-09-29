"""GPT-Live (OpenAI /v1/live) specific enumerations."""

from enum import StrEnum


class GPTLiveVoice(StrEnum):
    """Voices accepted by gpt-live-1; Realtime-only SDK names (alloy, ash, ...) kill a Live session."""

    MARIN = "marin"
    QUARTZ = "quartz"
    RIPPLE = "ripple"
    VESPER = "vesper"
    WILLOW = "willow"
    STONE = "stone"
    GLEAM = "gleam"
    MERIDIAN = "meridian"
    BOSSA = "bossa"
    TEMPO = "tempo"
    BEACON = "beacon"
    DELTA = "delta"
    CINDER = "cinder"
