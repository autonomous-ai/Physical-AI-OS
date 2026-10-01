"""Context compression setup must preserve thinking, tools and activity detection."""

import pytest
from google.genai import types
from google.genai._live_converters import _LiveConnectConfig_to_mldev
from pydantic import ValidationError

from hal.realtime.config import GeminiConfig
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


def build(model="gemini-3.8-live-extended-thinking", live=False, **settings):
    agent = object.__new__(GeminiLiveAgent)
    agent._config = GeminiConfig(
        api_key="test", model=model, instructions="Test instructions",
        google_search_enabled=False, session_resumption_enabled=False, **settings,
    )
    agent._vad_disabled = not live
    agent._resumption_handle = None
    agent._tools = [{"name": "delegate_to_main", "description": "Delegate actions"}]
    return agent._build_config()


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("model", ["gemini-3.8-live", "gemini-3.8-live-extended-thinking"])
def test_compression_setup_preserves_voice_contract(model, live):
    setup = build(model, live)
    compression = setup.context_window_compression
    assert compression.trigger_tokens == 32768
    assert compression.sliding_window.target_tokens == 24576
    assert setup.response_modalities == [types.Modality.AUDIO]
    assert setup.system_instruction == "Test instructions"
    assert setup.realtime_input_config.automatic_activity_detection.disabled == (not live)
    assert setup.tools[0].function_declarations[0].name == "delegate_to_main"
    if "extended-thinking" in model:
        assert setup.thinking_config.thinking_level == types.ThinkingLevel.LOW
    parent = {}
    _LiveConnectConfig_to_mldev(None, setup.model_dump(exclude_none=True), parent)
    assert parent["setup"]["contextWindowCompression"] == compression.model_dump(exclude_none=True)


@pytest.mark.parametrize("model", ["gemini-3.1-flash-live-preview", "gemini-2.5-flash-native-audio-preview-12-2025"])
def test_older_models_omit_compression(model):
    assert build(model).context_window_compression is None


def test_compression_can_be_disabled_or_tuned():
    assert build(context_trigger_tokens=0, context_target_tokens=0).context_window_compression is None
    compression = build(context_trigger_tokens=16000, context_target_tokens=12000).context_window_compression
    assert compression.trigger_tokens == 16000
    assert compression.sliding_window.target_tokens == 12000


@pytest.mark.parametrize("trigger,target", [(-1, 100), (100, -1), (100, 0), (100, 100), (100, 101)])
def test_invalid_window_rejected_before_connect(trigger, target):
    with pytest.raises(ValidationError):
        GeminiConfig(context_trigger_tokens=trigger, context_target_tokens=target)
