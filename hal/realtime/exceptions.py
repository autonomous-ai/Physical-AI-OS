"""Exceptions for realtime voice agent providers."""


class OpenAIRealtimeError(Exception):
    """Raised on OpenAI Realtime API errors."""


class GeminiLiveError(Exception):
    """Raised on Gemini Live API errors (e.g. go_away)."""


class GPTLiveError(Exception):
    """Raised on GPT-Live (OpenAI /v1/live) server `error` events."""


class PipecatV1Error(Exception):
    """Raised when the on-device Pipecat pipeline cannot be built or started."""
