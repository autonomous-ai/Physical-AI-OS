"""Configuration for realtime voice agent providers (values from hal.config)."""

from pydantic import BaseModel, Field, model_validator

import hal.config as app_config
from hal.presets import normalize_language
from hal.realtime.enums import (
    GeminiThinkingLevel,
    GeminiVoice,
    GPTLiveVoice,
    OpenAIReasoningEffort,
    OpenAITruncationType,
    OpenAITurnDetectionType,
    OpenAIVoice,
)


def _load_language() -> str | None:
    """Load language from the device's config.json (stt_language field)."""
    from hal.config import _os_cfg_get

    lang: str = normalize_language(_os_cfg_get("stt_language", ""))
    return lang if lang else None


def gemini_needs_idle_workaround(model: str | None = None) -> bool:
    """Whether a Gemini Live model needs the idle-resume WS-1011 workarounds (2.5 native-audio only).

    Defaults to the configured model when `model` is omitted.
    """
    m = (model if model is not None else app_config.REALTIME_GEMINI_MODEL) or ""
    return "native-audio" in m.lower()


def _parse_turn_detection(value: str) -> OpenAITurnDetectionType | None:
    """Parse HAL_REALTIME_TURN_DETECTION into an OpenAITurnDetectionType or None (off)."""
    v = value.strip().lower()
    if v in ("off", "none", ""):
        return None
    try:
        return OpenAITurnDetectionType(v)
    except ValueError:
        return OpenAITurnDetectionType.SERVER_VAD


# max_retries counts the first attempt: 1 = try once, 0 disables send/recv entirely.


class OpenAIConfig(BaseModel):
    api_key: str = app_config.REALTIME_OPENAI_API_KEY
    base_url: str | None = app_config.REALTIME_OPENAI_BASE_URL or None
    model: str = app_config.REALTIME_OPENAI_MODEL
    voice: OpenAIVoice = OpenAIVoice(app_config.REALTIME_OPENAI_VOICE)
    instructions: str = ""
    sample_rate: int = app_config.REALTIME_OPENAI_SAMPLE_RATE
    language: str | None = _load_language()
    turn_detection_type: OpenAITurnDetectionType | None = Field(
        default_factory=lambda: _parse_turn_detection(app_config.REALTIME_TURN_DETECTION)
    )
    reasoning_effort: OpenAIReasoningEffort = OpenAIReasoningEffort(
        app_config.REALTIME_OPENAI_REASONING_EFFORT
    )
    truncation_type: OpenAITruncationType = OpenAITruncationType.RETENTION_RATIO
    truncation_retention_ratio: float = 0.5
    transcribe_model: str = app_config.REALTIME_OPENAI_TRANSCRIBE_MODEL
    noise_reduction: str = app_config.REALTIME_OPENAI_NOISE_REDUCTION
    vad_threshold: float = app_config.REALTIME_OPENAI_VAD_THRESHOLD
    vad_start_sensitivity: str = app_config.LIVE_VAD_START_SENSITIVITY
    vad_end_sensitivity: str = app_config.LIVE_VAD_END_SENSITIVITY
    vad_prefix_padding_ms: int = app_config.LIVE_VAD_PREFIX_PADDING_MS
    vad_silence_ms: int = app_config.LIVE_VAD_SILENCE_MS
    max_retries: int = 1
    reconnect_delay_s: float = 2.0
    queue_poll_s: float = 1.0
    # Wait for the active response before forcing response.create.
    response_wait_s: float = 10.0


class GPTLiveConfig(BaseModel):
    api_key: str = app_config.REALTIME_GPTLIVE_API_KEY
    # Shared with OpenAI Realtime by default (see REALTIME_GPTLIVE_BASE_URL);
    # the SDK derives wss://…/live/sessions from it.
    base_url: str | None = app_config.REALTIME_GPTLIVE_BASE_URL or None
    model: str = app_config.REALTIME_GPTLIVE_MODEL
    voice: GPTLiveVoice = GPTLiveVoice(app_config.REALTIME_GPTLIVE_VOICE)
    instructions: str = ""
    sample_rate: int = app_config.REALTIME_GPTLIVE_SAMPLE_RATE
    language: str | None = _load_language()
    turn_gap_ms: int = app_config.REALTIME_GPTLIVE_TURN_GAP_MS
    interrupt_gap_ms: int = app_config.REALTIME_GPTLIVE_INTERRUPT_GAP_MS
    input_gap_ms: int = app_config.REALTIME_GPTLIVE_INPUT_GAP_MS
    commit_silence_ms: int = app_config.REALTIME_GPTLIVE_COMMIT_SILENCE_MS
    delegation_wait_ms: int = app_config.REALTIME_GPTLIVE_DELEGATION_WAIT_MS
    output_silence_dbfs: float = app_config.REALTIME_GPTLIVE_OUTPUT_SILENCE_DBFS
    delegation: str = app_config.REALTIME_GPTLIVE_DELEGATION  # client | responses | auto
    web_search: bool = app_config.REALTIME_GPTLIVE_WEB_SEARCH
    backend_model: str = app_config.REALTIME_GPTLIVE_BACKEND_MODEL

    @property
    def responses_mode(self) -> bool:
        """Effective delegation owner: the Responses backend or this process."""
        if self.delegation == "responses":
            return True
        if self.delegation == "client":
            return False
        return self.web_search
    max_retries: int = 1
    reconnect_delay_s: float = 2.0
    queue_poll_s: float = 1.0
    # How long a send waits for `session.started` before giving up on the
    # command (audio appended before the session is up is rejected).
    start_timeout_s: float = 10.0
    # Graceful close: how long to keep reading for `session.closed` after
    # sending `session.close`, so the relay can confirm the final usage.
    close_timeout_s: float = 5.0
    join_timeout_s: float = 5.0


class PipecatV1Config(BaseModel):
    """On-device Pipecat pipeline config; text out only (HAL's TTS speaks the reply)."""

    api_key: str = app_config.REALTIME_PIPECAT_API_KEY
    base_url: str | None = app_config.REALTIME_PIPECAT_BASE_URL or None
    model: str = app_config.REALTIME_PIPECAT_MODEL
    instructions: str = ""
    sample_rate: int = app_config.REALTIME_PIPECAT_SAMPLE_RATE
    language: str | None = _load_language()
    temperature: float = app_config.REALTIME_PIPECAT_TEMPERATURE
    max_tokens: int = app_config.REALTIME_PIPECAT_MAX_TOKENS
    disable_thinking: bool = app_config.REALTIME_PIPECAT_DISABLE_THINKING
    # STT fallback credentials (the injected VoiceService provider wins).
    stt_api_key: str = app_config.REALTIME_PIPECAT_STT_API_KEY
    stt_base_url: str = app_config.REALTIME_PIPECAT_STT_BASE_URL
    stt_model: str = app_config.REALTIME_PIPECAT_STT_MODEL
    # Live-mode turn detection (ignored on the turn-based path, where HAL's
    # own VAD brackets the utterance and commit ends it).
    smart_turn: bool = app_config.REALTIME_PIPECAT_SMART_TURN
    smart_turn_stop_secs: float = app_config.REALTIME_PIPECAT_SMART_TURN_STOP_SECS
    vad_confidence: float = app_config.REALTIME_PIPECAT_VAD_CONFIDENCE
    vad_start_secs: float = app_config.REALTIME_PIPECAT_VAD_START_SECS
    vad_stop_secs: float = app_config.REALTIME_PIPECAT_VAD_STOP_SECS
    vad_min_volume: float = app_config.REALTIME_PIPECAT_VAD_MIN_VOLUME
    silence_timeout_s: float = app_config.REALTIME_PIPECAT_SILENCE_TIMEOUT_S
    min_words: int = app_config.REALTIME_PIPECAT_MIN_WORDS
    turn_stop_timeout_s: float = app_config.REALTIME_PIPECAT_TURN_STOP_TIMEOUT_S
    tool_result_timeout_s: float = app_config.REALTIME_PIPECAT_TOOL_RESULT_TIMEOUT_S
    max_retries: int = 1
    reconnect_delay_s: float = 2.0
    queue_poll_s: float = 1.0
    # Loading Silero + Smart Turn ONNX took ~12 s cold on an A55.
    start_timeout_s: float = 60.0
    join_timeout_s: float = 5.0


class GeminiConfig(BaseModel):
    api_key: str = app_config.REALTIME_GEMINI_API_KEY
    base_url: str | None = app_config.REALTIME_GEMINI_BASE_URL or None
    model: str = app_config.REALTIME_GEMINI_MODEL
    voice: GeminiVoice = GeminiVoice(app_config.REALTIME_GEMINI_VOICE)
    instructions: str = ""
    sample_rate: int = app_config.REALTIME_GEMINI_SAMPLE_RATE
    language: str | None = _load_language()
    use_language_codes: bool = app_config.REALTIME_GEMINI_USE_LANGUAGE_CODES
    session_resumption_enabled: bool = app_config.REALTIME_GEMINI_SESSION_RESUMPTION
    google_search_enabled: bool = app_config.REALTIME_GEMINI_GOOGLE_SEARCH
    thinking_level: GeminiThinkingLevel = GeminiThinkingLevel(
        app_config.REALTIME_GEMINI_THINKING_LEVEL
    )
    context_trigger_tokens: int = Field(default=app_config.REALTIME_GEMINI_CONTEXT_TRIGGER_TOKENS, ge=0)
    context_target_tokens: int = Field(default=app_config.REALTIME_GEMINI_CONTEXT_TARGET_TOKENS, ge=0)

    @model_validator(mode="after")
    def validate_context_window(self):
        if self.context_trigger_tokens and not 0 < self.context_target_tokens < self.context_trigger_tokens:
            raise ValueError("Gemini context target must be positive and below the trigger; use trigger=0 to disable")
        return self

    vad_enabled: bool = Field(default_factory=lambda: (
        app_config.REALTIME_TURN_DETECTION.strip().lower() not in ("off", "none", "")
    ))

    vad_start_sensitivity: str = app_config.LIVE_VAD_START_SENSITIVITY
    vad_end_sensitivity: str = app_config.LIVE_VAD_END_SENSITIVITY
    vad_prefix_padding_ms: int = app_config.LIVE_VAD_PREFIX_PADDING_MS
    vad_silence_ms: int = app_config.LIVE_VAD_SILENCE_MS
    # No TEXT-only modality: Live rejects it with WS 1007, so audio is received and dropped.
    max_retries: int = 1
    reconnect_delay_s: float = 2.0
    send_timeout_s: float = 10.0
    recv_timeout_s: float = 300.0
    queue_poll_s: float = 1.0
    join_timeout_s: float = 5.0
