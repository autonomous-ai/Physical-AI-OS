"""Output data models streamed from the realtime voice agent."""

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict
from typing_extensions import ClassVar

from hal.realtime.enums import OutputTypeEnum


class OutputBase(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, arbitrary_types_allowed=True
    )
    type: OutputTypeEnum
    # Provider user-turn key; empty means ownership could not be established.
    user_turn_id: str = ""


class TextOutput(OutputBase):
    type: OutputTypeEnum = OutputTypeEnum.TEXT
    text: str


class MainAgentFallbackOutput(OutputBase):
    """Provider explicitly could not establish an outcome for the user's turn."""

    type: OutputTypeEnum = OutputTypeEnum.MAIN_AGENT_FALLBACK
    transcript: str = ""
    handoff_context: str = ""


class AudioOutput(OutputBase):
    type: OutputTypeEnum = OutputTypeEnum.AUDIO
    audio: npt.NDArray[np.float32]
    transcript: str | None = None


class ExecutionOutput(OutputBase):
    """Metric-only proof retained when the original control terminal is discarded."""

    type: OutputTypeEnum = OutputTypeEnum.EXECUTION
    execution_completed: bool = False


class UserSpeechOutput(OutputBase):
    """Observed user speech; transcription alone provides no speech endpoint."""

    type: OutputTypeEnum = OutputTypeEnum.USER_SPEECH
    turn_id: str
    # Incremental provider transcription, attributed to this exact input turn.
    transcript: str = ""
    # Provider transcription completion is emotion evidence, not a VAD timestamp.
    transcript_finished: bool = False
    endpoint_at: float | None = None
    method: str = "provider_transcript"


class InterruptedOutput(OutputBase):
    type: OutputTypeEnum = OutputTypeEnum.INTERRUPTED
    reason: str = "output_reset"
    at: float | None = None


class FunctionCallOutput(OutputBase):
    type: OutputTypeEnum = OutputTypeEnum.FUNCTION_CALL
    name: str
    arguments: str  # JSON string
    call_id: str
    # Provider-side transcript of the utterance that triggered the call
    # (Gemini input_transcription); empty when the provider has none.
    user_transcript: str = ""
    handoff_context: str = ""
