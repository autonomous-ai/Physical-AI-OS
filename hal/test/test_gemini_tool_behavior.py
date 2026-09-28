"""Regression: extended-thinking needs NON_BLOCKING tool declarations.

`gemini-3.8-live-extended-thinking` accepts ONLY NON_BLOCKING tool declarations;
a BLOCKING one makes it error mid-turn and speak a canned "I'm sorry, an error
occurred." after every tool call (device-observed 2026-09-17). Every other live
model keeps the BLOCKING default. The behavior is gated on the model string in
GeminiLiveAgent._build_config.
"""

from types import SimpleNamespace

from google.genai import types

from hal.realtime.enums.gemini import GeminiThinkingLevel
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


def _build(model: str, language=None, use_language_codes=False) -> types.LiveConnectConfig:
    agent = object.__new__(GeminiLiveAgent)
    agent._vad_disabled = True  # short-circuits _activity_detection setup
    agent._resumption_handle = None
    agent._tools = [{"name": "delegate_to_main", "description": "", "parameters": None}]
    agent._config = SimpleNamespace(
        language=language,
        use_language_codes=use_language_codes,
        model=model,
        thinking_level=GeminiThinkingLevel.LOW,
        voice=SimpleNamespace(value="Kore"),
        instructions="sys",
        google_search_enabled=False,
        session_resumption_enabled=False,
    )
    return agent._build_config()


def _tool_behavior(cfg: types.LiveConnectConfig):
    return cfg.tools[0].function_declarations[0].behavior


def test_extended_thinking_declares_non_blocking() -> None:
    cfg = _build("gemini-3.8-live-extended-thinking")
    declarations = cfg.tools[0].function_declarations
    assert {tool.name for tool in declarations} == {"delegate_to_main", "complete_response"}
    assert all(tool.behavior == types.Behavior.NON_BLOCKING for tool in declarations)
    completion, = [tool for tool in declarations if tool.name == "complete_response"]
    assert completion.parameters.required in (None, [])
    assert "Never use for an action" in completion.description


def test_plain_live_leaves_tool_blocking() -> None:
    # Unset behavior == the provider's BLOCKING default; must NOT be NON_BLOCKING.
    for model in ("gemini-3.8-live", "gemini-3.1-flash-live-preview"):
        cfg = _build(model)
        assert _tool_behavior(cfg) != types.Behavior.NON_BLOCKING
        assert [tool.name for tool in cfg.tools[0].function_declarations] == ["delegate_to_main"]


if __name__ == "__main__":
    test_extended_thinking_declares_non_blocking()
    test_plain_live_leaves_tool_blocking()
    print("ok")


def test_input_language_hint_serializes_for_developer_api():
    from google.genai._live_converters import _AudioTranscriptionConfig_to_mldev

    cfg = _build("gemini-3.8-live-extended-thinking", "vi", True)
    data = cfg.input_audio_transcription.model_dump(exclude_none=True)
    wire = _AudioTranscriptionConfig_to_mldev(data)
    assert wire == {"languageHints": {"language_codes": ["vi-VN"]}}
    assert cfg.output_audio_transcription.model_dump(exclude_none=True) == {}


def test_input_language_hint_opt_in_and_preserves_locale():
    for language, enabled, expected in [
        ("vi", False, None), (None, True, None), ("", True, None),
        ("fr-FR", True, ["fr-FR"]), ("vi-VN", True, ["vi-VN"]),
    ]:
        cfg = _build("gemini-3.8-live-extended-thinking", language, enabled)
        hints = cfg.input_audio_transcription.language_hints
        assert (hints.language_codes if hints else None) == expected
