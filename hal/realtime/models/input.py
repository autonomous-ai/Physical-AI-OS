"""Input data models sent to the realtime voice agent."""


import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict

from hal.realtime.enums import InputTypeEnum


class InputBase(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    type: InputTypeEnum


class TextInput(InputBase):
    type: InputTypeEnum = InputTypeEnum.TEXT
    text: str


class AnnounceInput(InputBase):
    """Device-initiated text that must produce a spoken reply (providers with `supports_announce` only)."""

    type: InputTypeEnum = InputTypeEnum.ANNOUNCE
    text: str


class AudioInput(InputBase):
    type: InputTypeEnum = InputTypeEnum.AUDIO
    audio: npt.NDArray[np.float32]


class ImageInput(InputBase):
    type: InputTypeEnum = InputTypeEnum.IMAGE
    image: cv2t.MatLike


class FunctionCallResultInput(InputBase):
    type: InputTypeEnum = InputTypeEnum.FUNCTION_CALL_RESULT
    call_id: str
    output: str  # JSON string
    image: cv2t.MatLike | None = None
    # False records the result without a new model response (fire-and-forget tools like express_emotion).
    trigger_response: bool = True
