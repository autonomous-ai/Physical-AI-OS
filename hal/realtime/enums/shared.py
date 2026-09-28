"""Shared enumerations used by both providers."""

from enum import StrEnum


class AgentGateway(StrEnum):
    # Mirrors the Go side's domain.AgentRuntimes — every runtime os-server can
    # activate MUST be listed here, or HAL's voice auto-start dies with
    # "'<name>' is not a valid AgentGateway" (device-hit with codex 2026-07-07).
    OPENCLAW = "openclaw"
    HERMES = "hermes"
    PICOCLAW = "picoclaw"
    CODEX = "codex"
    CLAUDECODE = "claudecode"
    OPENCODE = "opencode"


class InputTypeEnum(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    FUNCTION_CALL_RESULT = "function_call_result"
    ANNOUNCE = "announce"


class OutputTypeEnum(StrEnum):
    TEXT = "text"
    AUDIO = "audio"
    FUNCTION_CALL = "function_call"
    INTERRUPTED = "interrupted"
    USER_SPEECH = "user_speech"
    EXECUTION = "execution"
    MAIN_AGENT_FALLBACK = "main_agent_fallback"


class InputEventTypeEnum(StrEnum):
    """Types for the agent send queue."""
    INPUT = "input"
    AUDIO_COMMIT = "audio_commit"
    AUDIO_STREAM_END = "audio_stream_end"


class OutputEventTypeEnum(StrEnum):
    """Types for the agent receive queue."""
    OUTPUT = "output"
    TURN_DONE = "turn_done"
