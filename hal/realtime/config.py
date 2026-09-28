"""Configuration for realtime voice agent providers.

All values are read from hal.config (environment variables).
"""

from pydantic import BaseModel

import hal.config as app_config
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

    lang: str = _os_cfg_get("stt_language", "").strip()
    return lang if lang else None


def gemini_needs_idle_workaround(model: str | None = None) -> bool:
    """Whether a Gemini Live model needs the idle-resume WS-1011 workarounds.

    True ONLY for the 2.5 native-audio family: through the campaign-api proxy it
    returns WS 1011 on a turn that follows an idle pause — a backend limitation
    (the browser raw-WS client fails the same way), not fixable client-side. The
    workarounds (keepalive ping, pre-turn rebuild, replay, suppressed
    mid-activity [TURN CONTEXT]) only mitigate it and add churn/latency, so they
    must stay OFF for models that DON'T have the bug.

    `gemini-3.1-flash-live` handles idle→resume fine → returns False → all
    workarounds disabled (clean, fast, per-turn context restored). Switch the
    model back to native-audio in config.json and they re-enable automatically.
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


# max_retries bounds the send/recv attempt loop in every provider — it counts the
# FIRST attempt, so 1 means "try once", and 0 would disable sending and receiving
# outright rather than disabling retries (use realtime.enabled for that).
#
# 1 is not a behaviour change from the old 3: a failed attempt clears _connected
# and drops the session, and the next iteration's _ensure_connected is throttled
# by the reconnect backoff, so attempts 2 and 3 could never do work — they logged
# "Not connected, skipping attempt" and fell through in microseconds. Recovery
# lives elsewhere and is unaffected: _fail_fast_turn ends the turn immediately so
# it falls back to the main agent, and the background reconnect keeps retrying on
# a 2s → 60s backoff so a session dead from a quota close heals once the limit
# lifts.


class OpenAIConfig(BaseModel):
    api_key: str = app_config.REALTIME_OPENAI_API_KEY
    base_url: str | None = app_config.REALTIME_OPENAI_BASE_URL or None
    model: str = app_config.REALTIME_OPENAI_MODEL
    voice: OpenAIVoice = OpenAIVoice(app_config.REALTIME_OPENAI_VOICE)
    instructions: str = ""
    sample_rate: int = app_config.REALTIME_OPENAI_SAMPLE_RATE
    language: str | None = _load_language()
    turn_detection_type: OpenAITurnDetectionType | None = _parse_turn_detection(
        app_config.REALTIME_TURN_DETECTION
    )
    reasoning_effort: OpenAIReasoningEffort = OpenAIReasoningEffort(
        app_config.REALTIME_OPENAI_REASONING_EFFORT
    )
    truncation_type: OpenAITruncationType = OpenAITruncationType.RETENTION_RATIO
    truncation_retention_ratio: float = 0.5
    transcribe_model: str = app_config.REALTIME_OPENAI_TRANSCRIBE_MODEL
    noise_reduction: str = app_config.REALTIME_OPENAI_NOISE_REDUCTION
    # Server VAD knobs — the same HAL_LIVE_VAD_* settings Gemini reads, mapped
    # onto server_vad threshold / prefix_padding_ms / silence_duration_ms and
    # semantic_vad eagerness (see OpenAIRealtimeAgent._turn_detection).
    vad_threshold: float = app_config.REALTIME_OPENAI_VAD_THRESHOLD
    vad_start_sensitivity: str = app_config.LIVE_VAD_START_SENSITIVITY
    vad_end_sensitivity: str = app_config.LIVE_VAD_END_SENSITIVITY
    vad_prefix_padding_ms: int = app_config.LIVE_VAD_PREFIX_PADDING_MS
    vad_silence_ms: int = app_config.LIVE_VAD_SILENCE_MS
    max_retries: int = 1
    reconnect_delay_s: float = 2.0
    queue_poll_s: float = 1.0
    # How long a commit / tool result waits for the active response to finish
    # before forcing response.create anyway.
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
    """On-device Pipecat pipeline (voice_agent/pipecat_v1.py). Text out only —
    no voice field: HAL's TTS speaks the reply."""

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
    # Pipeline build + StartFrame must land within this; else "unavailable".
    # Loading Silero + Smart Turn ONNX took ~12 s on lamp-ee17 (A55) cold.
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
    vad_enabled: bool = app_config.REALTIME_TURN_DETECTION.strip().lower() not in (
        "off",
        "none",
        "",
    )

    vad_start_sensitivity: str = app_config.LIVE_VAD_START_SENSITIVITY
    vad_end_sensitivity: str = app_config.LIVE_VAD_END_SENSITIVITY
    vad_prefix_padding_ms: int = app_config.LIVE_VAD_PREFIX_PADDING_MS
    vad_silence_ms: int = app_config.LIVE_VAD_SILENCE_MS
    # NO text_only / TEXT-modality field. Opening a TEXT-only Live session to
    # avoid paying for audio we discard was tried on device 2026-09-08 and the
    # model refuses it outright: WS 1007 "The requested combination of response
    # modalities (TEXT) is not supported by the model". Live is audio-out only,
    # so with REALTIME_NATIVE_AUDIO=false the audio is received and dropped.
    max_retries: int = 1
    reconnect_delay_s: float = 2.0
    send_timeout_s: float = 10.0
    recv_timeout_s: float = 300.0
    queue_poll_s: float = 1.0
    join_timeout_s: float = 5.0
