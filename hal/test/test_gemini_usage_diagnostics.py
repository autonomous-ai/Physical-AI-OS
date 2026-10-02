"""Usage diagnostics preserve provider evidence without logging content payloads."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

from google.genai import types

from hal.realtime.voice_agent import gemini_live
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


def _agent():
    agent = object.__new__(GeminiLiveAgent)
    agent._config = SimpleNamespace(
        model="gemini-3.8-live-extended-thinking",
        base_url="https://example.invalid/ws/gemini",
        voice="Kore",
        instructions="PRIVATE_SYSTEM_INSTRUCTIONS",
    )
    agent._session = SimpleNamespace(_ws=SimpleNamespace(_trace_id="session-a"))
    agent._turn_gen = 3
    agent._live_user_turn_id = "user-turn-a"
    return agent


def _usage(**kwargs):
    return types.UsageMetadata(**kwargs)


def _usage_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "hal.realtime.usage"]


def test_raw_counts_keep_absent_distinct_from_zero_and_preserve_details():
    usage = _usage(
        prompt_token_count=82039,
        response_token_count=0,
        thoughts_token_count=5383,
        cached_content_token_count=32768,
        cache_tokens_details=[types.ModalityTokenCount(modality="TEXT", token_count=32768)],
        tool_use_prompt_tokens_details=[types.ModalityTokenCount(modality="IMAGE", token_count=0)],
    )
    raw = json.loads(GeminiLiveAgent._usage_raw_counts(usage))
    assert raw == {
        "prompt_token_count": 82039,
        "response_token_count": 0,
        "total_token_count": None,
        "thoughts_token_count": 5383,
        "cached_content_token_count": 32768,
        "tool_use_prompt_token_count": None,
        "cache_tokens_details": [{"modality": "TEXT", "token_count": 32768}],
        "tool_use_prompt_tokens_details": [{"modality": "IMAGE", "token_count": 0}],
    }


def test_raw_counts_allowlist_drops_payloads_from_unknown_metadata():
    usage = SimpleNamespace(
        prompt_token_count=7,
        private_text="PRIVATE_USER_TRANSCRIPT",
        cache_tokens_details=[{
            "modality": "TEXT", "token_count": 3, "payload": "PRIVATE_IMAGE_PAYLOAD",
        }],
    )
    encoded = GeminiLiveAgent._usage_raw_counts(usage)
    assert "PRIVATE_" not in encoded
    assert json.loads(encoded)["cache_tokens_details"] == [{"modality": "TEXT", "token_count": 3}]


def test_usage_sequence_counts_events_not_turns_and_preserves_message_status(caplog):
    caplog.set_level(logging.INFO)
    agent = _agent()
    first = SimpleNamespace(
        usage_metadata=_usage(prompt_token_count=19, total_token_count=23),
        server_content=SimpleNamespace(
            interaction_status="IN_PROGRESS", generation_complete=False, turn_complete=True,
            text="PRIVATE_USER_TRANSCRIPT",
        ),
    )
    agent._log_usage(SimpleNamespace(usage_metadata=None))
    agent._log_usage(first, user_turn_id="explicit-turn")
    agent._log_usage(SimpleNamespace(usage_metadata=_usage(prompt_token_count=82)))
    lines = _usage_lines(caplog)
    assert len(lines) == 2
    assert "session=session-a usage_event_seq=1 gen=3 user_turn_id=explicit-turn" in lines[0]
    assert "interaction_status=IN_PROGRESS generation_complete=False turn_complete=True" in lines[0]
    assert "session=session-a usage_event_seq=2 gen=3 user_turn_id=user-turn-a" in lines[1]
    assert "interaction_status=None generation_complete=None turn_complete=None" in lines[1]
    assert json.loads(lines[1].split("usage_raw=", 1)[1])["total_token_count"] is None
    assert "PRIVATE_" not in caplog.text


def test_tool_ack_diagnostics_correlate_usage_without_old_session_contamination(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    agent = _agent()
    monkeypatch.setattr(gemini_live.time, "monotonic", lambda: 100.0)
    agent._record_tool_ack("look", "look-1", agent._session)
    old_session = SimpleNamespace(_ws=SimpleNamespace(_trace_id="old-session"))
    agent._record_tool_ack("express_emotion", "old-call", old_session)
    monkeypatch.setattr(gemini_live.time, "monotonic", lambda: 101.25)
    agent._log_usage(SimpleNamespace(usage_metadata=_usage(thoughts_token_count=5383)))
    line, = _usage_lines(caplog)
    assert "last_tool_ack_name=look last_tool_ack_id=look-1 last_tool_ack_monotonic=100.0" in line
    assert "last_tool_ack_elapsed_ms=1250.0" in line
    assert "thought_count=5383" in line
    assert "Tool ACK sent: session=old-session" in caplog.text
    assert "PRIVATE_" not in caplog.text


def test_new_connection_resets_usage_sequence_and_ack_metadata(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    agent = _agent()
    agent._usage_event_seq = 9
    agent._last_tool_ack = ("look", "old-call", 100.0)
    new_session = SimpleNamespace(_ws=SimpleNamespace(_trace_id="session-b"))

    @asynccontextmanager
    async def connect(**kwargs):
        yield new_session

    agent._client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=connect)))
    monkeypatch.setattr(agent, "_build_config", lambda: None)
    monkeypatch.setattr(gemini_live, "install_interaction_status", lambda session: None)

    async def run():
        await agent._async_connect()
        agent._log_usage(SimpleNamespace(usage_metadata=_usage(prompt_token_count=7)))
        await agent._async_disconnect()

    asyncio.run(run())
    line, = _usage_lines(caplog)
    assert "session=session-b usage_event_seq=1" in line
    assert "last_tool_ack_name=None last_tool_ack_id=None" in line
    assert "instruction_chars=27" in caplog.text
    assert "PRIVATE_SYSTEM_INSTRUCTIONS" not in caplog.text
