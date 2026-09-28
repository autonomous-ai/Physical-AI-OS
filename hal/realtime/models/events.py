"""Queue event models for the voice agent send/receive loops.

Send queue: AgentInputEvent subclasses.
Receive queue: AgentOutputEvent subclasses.
"""

import time

from pydantic import BaseModel, ConfigDict, Field

from hal.realtime.enums import InputEventTypeEnum, OutputEventTypeEnum
from hal.realtime.models.input import InputBase
from hal.realtime.models.output import OutputBase

# --- Send queue events ---


class AgentInputEvent(BaseModel):
    """Base for all send queue events."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    type: InputEventTypeEnum


class InputEvent(AgentInputEvent):
    """Send queue: a model input (audio, text, image, function result)."""

    type: InputEventTypeEnum = InputEventTypeEnum.INPUT
    input: InputBase


class AudioCommitEvent(AgentInputEvent):
    """Send queue: commit buffered audio (end of speech turn)."""

    type: InputEventTypeEnum = InputEventTypeEnum.AUDIO_COMMIT
    # Monotonic timestamp captured on the voice thread when local end-of-turn is
    # detected. It lets providers separate queue/commit delay from model latency.
    queued_at: float = Field(default_factory=time.monotonic)


class AudioStreamEndEvent(AgentInputEvent):
    """End an automatic-VAD input stream on its original transport only."""

    type: InputEventTypeEnum = InputEventTypeEnum.AUDIO_STREAM_END
    session: object


# --- Receive queue events ---


class AgentOutputEvent(BaseModel):
    """Base for all receive queue events."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    type: OutputEventTypeEnum


class OutputEvent(AgentOutputEvent):
    """Receive queue: a model output (audio, text, function call)."""

    type: OutputEventTypeEnum = OutputEventTypeEnum.OUTPUT
    output: OutputBase
    gen: int = 0


class TurnDoneEvent(AgentOutputEvent):
    """Receive queue: model finished its turn."""

    type: OutputEventTypeEnum = OutputEventTypeEnum.TURN_DONE
    # Synthetic unblock/error sentinels retain False; only provider proof sets True.
    execution_completed: bool = False
    user_turn_id: str = ""
    # Providers opt in when speech alone does not establish task completion.
    fallback_to_main: bool = False
    user_transcript: str = ""
    handoff_context: str = ""
