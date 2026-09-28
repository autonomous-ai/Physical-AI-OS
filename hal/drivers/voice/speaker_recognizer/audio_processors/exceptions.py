"""Exceptions for the audio preprocessing pipeline (ported from perception-service)."""

import math
from typing import Any, Dict, Optional

REJECT_EMPTY_INPUT = "empty_input"
REJECT_VAD_REMOVED_ALL = "vad_removed_all"
REJECT_TOO_SHORT = "too_short"
REJECT_LOW_VOICE_RATIO = "low_voice_ratio"
REJECT_LOW_INTELLIGIBILITY = "low_intelligibility"


def _round_or_none(value: float) -> Optional[float]:
    """Round for JSON, mapping NaN (metric not measured) to None."""
    return None if math.isnan(value) else round(value, 3)


class PreprocessRejected(ValueError):
    """Raised when the speech gate rejects an audio clip."""

    def __init__(
        self,
        reason: str,
        *,
        input_duration_sec: float = 0.0,
        stripped_duration_sec: float = 0.0,
        voice_ratio: float = 0.0,
        min_duration_sec: float = 0.0,
        min_voice_ratio: float = 0.0,
        stoi_score: float = float("nan"),
        stoi_threshold: float = float("nan"),
    ) -> None:
        self.reason: str = reason
        self.input_duration_sec: float = float(input_duration_sec)
        self.stripped_duration_sec: float = float(stripped_duration_sec)
        self.voice_ratio: float = float(voice_ratio)
        self.min_duration_sec: float = float(min_duration_sec)
        self.min_voice_ratio: float = float(min_voice_ratio)
        self.stoi_score: float = float(stoi_score)
        self.stoi_threshold: float = float(stoi_threshold)
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        if self.reason == REJECT_EMPTY_INPUT:
            return "Empty audio input (0 samples)."
        if self.reason == REJECT_VAD_REMOVED_ALL:
            return (
                "VAD detected no speech "
                f"(input_duration={self.input_duration_sec:.2f}s)."
            )
        if self.reason == REJECT_TOO_SHORT:
            return (
                f"Stripped audio is {self.stripped_duration_sec:.2f}s, "
                f"below minimum {self.min_duration_sec:.2f}s "
                f"(input_duration={self.input_duration_sec:.2f}s, "
                f"voice_ratio={self.voice_ratio:.2f})."
            )
        if self.reason == REJECT_LOW_VOICE_RATIO:
            return (
                f"voice_ratio={self.voice_ratio:.2f} below minimum "
                f"{self.min_voice_ratio:.2f} "
                f"(stripped_duration={self.stripped_duration_sec:.2f}s, "
                f"input_duration={self.input_duration_sec:.2f}s)."
            )
        if self.reason == REJECT_LOW_INTELLIGIBILITY:
            return (
                f"mean STOI={self.stoi_score:.3f} below minimum "
                f"{self.stoi_threshold:.3f} "
                f"(input_duration={self.input_duration_sec:.2f}s) — "
                f"noisy or unintelligible audio."
            )
        return f"Preprocess rejected: {self.reason}."

    def to_dict(self) -> Dict[str, Any]:
        """Serializable payload for error responses / logs."""
        return {
            "reason": self.reason,
            "message": self._format_message(),
            "input_duration_sec": round(self.input_duration_sec, 3),
            "stripped_duration_sec": round(self.stripped_duration_sec, 3),
            "voice_ratio": round(self.voice_ratio, 3),
            "min_duration_sec": round(self.min_duration_sec, 3),
            "min_voice_ratio": round(self.min_voice_ratio, 3),
            "stoi_score": _round_or_none(self.stoi_score),
            "stoi_threshold": _round_or_none(self.stoi_threshold),
        }
