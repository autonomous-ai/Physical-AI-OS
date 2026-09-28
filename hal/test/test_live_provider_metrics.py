"""Live metadata preserves user ownership without inventing speech timestamps."""

import asyncio
import queue
import threading
from types import SimpleNamespace as NS

import pytest

from hal import config

from hal.realtime.models import (
    AudioOutput, ExecutionOutput, InterruptedOutput, OutputEvent, TextOutput, TurnDoneEvent, UserSpeechOutput,
)
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


@pytest.fixture(autouse=True)
def _live_mode(monkeypatch):
    monkeypatch.setattr(config, "LIVE_MODE", True)


def _gemini_message(*, transcript=None, reply=None, interrupted=False,
                    done=False, activity=None, generation_complete=False):
    return NS(
        usage_metadata=None, tool_call=None, session_resumption_update=None,
        go_away=None,
        voice_activity=NS(voice_activity_type=activity) if activity else None,
        server_content=NS(
            grounding_metadata=None, model_turn=None,
            input_transcription=NS(text=transcript, finished=True) if transcript else None,
            output_transcription=NS(text=reply) if reply else None,
            interrupted=interrupted, turn_complete=done, generation_complete=generation_complete,
        ),
    )


def _gemini(messages):
    agent = object.__new__(GeminiLiveAgent)
    agent._recv_queue = queue.Queue()
    agent._turn_done = threading.Event()

    async def receive():
        for message in messages:
            yield message

    agent._session = NS(receive=receive)
    agent._pending_tool_calls = set()
    agent._last_audio_sent_at = None
    agent._activity_end_sent_at = None
    asyncio.run(agent._async_receive_turn())
    return _drain(agent)


def _drain(agent):
    events = []
    while not agent._recv_queue.empty():
        event = agent._recv_queue.get_nowait()
        events.append(event.output if isinstance(event, OutputEvent) else event)
    return events


def test_gemini_transcript_chunks_create_one_input_without_fake_endpoint():
    events = _gemini([
        _gemini_message(transcript="Find "),
        _gemini_message(transcript="a flight", reply="Looking now", done=True),
    ])
    speech = [e for e in events if isinstance(e, UserSpeechOutput)]
    assert len(speech) == 2
    assert "".join(e.transcript for e in speech) == "Find a flight"
    assert speech[0].transcript_finished is True
    assert speech[0].endpoint_at is None
    assert speech[0].method == "provider_transcript"
    assert all(e.user_turn_id == speech[0].turn_id for e in events)
    assert events[-1].execution_completed
    reset = next(e for e in events if isinstance(e, InterruptedOutput))
    assert reset.reason == "output_reset"
    assert reset.at is None


def test_gemini_unsolicited_response_has_no_user_task():
    events = _gemini([_gemini_message(reply="Welcome", done=True)])
    assert not any(isinstance(e, UserSpeechOutput) for e in events)
    assert all(e.user_turn_id == "" for e in events)


def test_gemini_late_transcription_does_not_reassign_response_completion():
    events = _gemini([
        _gemini_message(reply="Previous response"),
        _gemini_message(transcript="New request", done=True),
    ])
    speech = next(e for e in events if isinstance(e, UserSpeechOutput))
    assert speech.turn_id
    assert next(e for e in events if isinstance(e, TextOutput)).user_turn_id == ""
    assert events[-1].user_turn_id == ""


def test_gemini_interrupt_preserves_input_and_does_not_complete_new_request():
    events = _gemini([
        _gemini_message(transcript="First", reply="First response"),
        _gemini_message(interrupted=True, transcript="Second", reply="Must be discarded", done=True),
    ])
    speeches = [e for e in events if isinstance(e, UserSpeechOutput)]
    assert len(speeches) == 2
    interrupt = next(e for e in events if isinstance(e, InterruptedOutput))
    assert interrupt.reason == "server_interrupt"
    assert interrupt.at is not None
    assert interrupt.user_turn_id == speeches[0].turn_id
    assert not any(isinstance(e, TextOutput) for e in events)
    assert events[-1].user_turn_id == speeches[0].turn_id
    assert not events[-1].execution_completed
    assert speeches[0].turn_id != speeches[1].turn_id


def test_gemini_vad_enriches_same_input_key(monkeypatch):
    monkeypatch.setattr("hal.realtime.voice_agent.gemini_live.time.monotonic", lambda: 42.0)
    events = _gemini([
        _gemini_message(activity="ACTIVITY_START"),
        _gemini_message(transcript="Hello"),
        _gemini_message(activity="ACTIVITY_END"),
        _gemini_message(reply="Hi", done=True),
    ])
    speeches = [e for e in events if isinstance(e, UserSpeechOutput)]
    assert len(speeches) == 2
    assert speeches[0].turn_id == speeches[1].turn_id
    assert speeches[0].endpoint_at is None
    assert speeches[1].endpoint_at == 42.0
    assert speeches[1].method == "server_vad_receive"
    assert events[-1].user_turn_id == speeches[0].turn_id


def _base_agent():
    agent = object.__new__(GeminiLiveAgent)
    agent._recv_queue = queue.Queue()
    agent._turn_done = threading.Event()
    return agent


def test_base_preserves_input_metadata_and_copies_terminal_ownership():
    agent = _base_agent()
    agent._newest_output_gen = 8
    agent._skip_stale_turn_done = False
    agent._recv_timeout_override_s = None
    agent._recv_queue.put(OutputEvent(gen=1, output=UserSpeechOutput(turn_id="old-input")))
    agent._recv_queue.put(TurnDoneEvent(execution_completed=True, user_turn_id="old-input"))
    assert list(agent.receive())[0].turn_id == "old-input"
    assert agent.execution_completed
    assert agent.execution_turn_id == "old-input"
    agent._recv_queue.put(TurnDoneEvent())
    assert list(agent.receive()) == []
    assert not agent.execution_completed
    assert agent.execution_turn_id == ""


def test_gemini_late_playback_terminal_cannot_complete_new_input():
    agent = object.__new__(GeminiLiveAgent)
    agent._recv_queue = queue.Queue()
    agent._turn_done = threading.Event()
    messages = iter([
        _gemini_message(transcript="First", reply="First answer", generation_complete=True),
        _gemini_message(transcript="Second", done=True),
        _gemini_message(reply="Second answer", done=True),
    ])

    async def receive():
        for message in messages:
            yield message

    agent._session = NS(receive=receive)
    agent._pending_tool_calls = set()
    asyncio.run(agent._async_receive_turn())
    first = _drain(agent)
    asyncio.run(agent._async_receive_turn())
    second = _drain(agent)
    speech = next(e for e in second if isinstance(e, UserSpeechOutput))
    assert speech.turn_id != first[-1].user_turn_id
    # Preserve the baseline receive boundary, but do not count the old playback
    # acknowledgement as completion of the newly observed user input.
    assert isinstance(second[-1], TurnDoneEvent)
    assert second[-1].user_turn_id == ""
    assert not second[-1].execution_completed
    asyncio.run(agent._async_receive_turn())
    third = _drain(agent)
    assert next(e for e in third if isinstance(e, TextOutput)).user_turn_id == speech.turn_id
    assert third[-1].user_turn_id == speech.turn_id


def test_turn_mode_does_not_emit_live_user_metadata(monkeypatch):
    monkeypatch.setattr(config, "LIVE_MODE", False)
    events = _gemini([_gemini_message(transcript="Hello", reply="Hi", done=True)])
    assert not any(isinstance(e, UserSpeechOutput) for e in events)


def test_base_preserves_genuine_boundary_from_old_generation():
    agent = _base_agent()
    agent._newest_output_gen = 8
    agent._skip_stale_turn_done = False
    agent._recv_timeout_override_s = None
    agent._recv_queue.put(OutputEvent(gen=1, output=InterruptedOutput(
        reason="server_interrupt", at=42.0, user_turn_id="old-input",
    )))
    agent._recv_queue.put(OutputEvent(gen=1, output=TextOutput(text="old reply")))
    agent._recv_queue.put(TurnDoneEvent(user_turn_id="old-input"))
    outputs = list(agent.receive())
    assert len(outputs) == 1
    assert outputs[0].reason == "server_interrupt"
    assert not agent.execution_completed
    agent._recv_queue.put(TurnDoneEvent())
    assert list(agent.receive()) == []
    assert not agent.execution_completed


def test_gemini_connect_attempt_clears_previous_session_ownership():
    from unittest.mock import Mock

    gemini = object.__new__(GeminiLiveAgent)
    gemini._live_user_turn_id = "old"
    gemini._live_speech_emitted = True
    gemini._awaiting_playback_turn_complete = True
    gemini._config = NS(base_url="test", model="test")
    gemini._build_config = lambda: {}
    gemini._client = Mock()
    gemini._client.aio.live.connect.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError, match="offline"):
        asyncio.run(gemini._async_connect())
    assert gemini._live_user_turn_id == ""
    assert not gemini._live_speech_emitted
    assert not gemini._awaiting_playback_turn_complete


def test_gemini_interrupt_discards_same_frame_audio_and_text_as_before():
    frame = _gemini_message(transcript="Question", reply="Discard this", interrupted=True, done=True)
    frame.server_content.model_turn = NS(parts=[NS(
        thought=False, text="", inline_data=NS(data=b"\x00\x00\x01\x00"),
    )])
    events = _gemini([frame])
    assert not any(isinstance(event, (AudioOutput, TextOutput)) for event in events)
    assert not any(isinstance(event, InterruptedOutput) and event.reason == "output_reset"
                   for event in events)
    assert sum(isinstance(event, TurnDoneEvent) for event in events) == 1
    assert isinstance(events[-1], TurnDoneEvent)


def test_gemini_dropped_terminal_is_metadata_not_an_extra_receive_boundary():
    agent = object.__new__(GeminiLiveAgent)
    agent._recv_queue = queue.Queue()
    agent._turn_done = threading.Event()
    agent._recv_queue.put(TurnDoneEvent(execution_completed=True, user_turn_id="previous"))

    async def receive():
        yield _gemini_message(interrupted=True, done=True)

    agent._session = NS(receive=receive)
    agent._pending_tool_calls = set()
    asyncio.run(agent._async_receive_turn())
    events = _drain(agent)
    preserved = next(event for event in events if isinstance(event, ExecutionOutput))
    assert preserved.user_turn_id == "previous"
    assert preserved.execution_completed
    assert sum(isinstance(event, TurnDoneEvent) for event in events) == 1
    assert isinstance(events[-1], TurnDoneEvent)


def test_finished_only_transcription_preserves_key_without_metric_endpoint():
    final = _gemini_message(done=True)
    final.server_content.input_transcription = NS(text=None, finished=True)
    events = _gemini([_gemini_message(transcript="hello"), final])
    speech = [e for e in events if isinstance(e, UserSpeechOutput)]
    assert len(speech) == 2
    assert speech[1].turn_id == speech[0].turn_id
    assert speech[1].transcript == ""
    assert speech[1].transcript_finished is True
    assert speech[1].endpoint_at is None


def test_finished_only_transcription_cannot_create_a_task():
    final = _gemini_message(done=True)
    final.server_content.input_transcription = NS(text=None, finished=True)
    assert not any(isinstance(e, UserSpeechOutput) for e in _gemini([final]))
