"""OpenAI Realtime emits the same live-mode contract as Gemini Live."""

import base64
import logging
import queue
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest

from hal import config
from hal.realtime.models import (
    AudioOutput,
    ExecutionOutput,
    FunctionCallOutput,
    InterruptedOutput,
    OutputEvent,
    TextOutput,
    TurnDoneEvent,
    UserSpeechOutput,
)
from hal.realtime.voice_agent import openai_realtime
from hal.realtime.voice_agent.openai_realtime import OpenAIRealtimeAgent


@pytest.fixture(autouse=True)
def _live_mode(monkeypatch):
    monkeypatch.setattr(config, "LIVE_MODE", True)


def _pcm(ms: int, rate: int = 24000) -> str:
    return base64.b64encode(np.zeros(rate * ms // 1000, dtype=np.int16).tobytes()).decode()


def ev(type_: str, **fields):
    return NS(type=type_, **fields)


def speech_started(item="item_u1"):
    return ev("input_audio_buffer.speech_started", item_id=item, audio_start_ms=0)


def speech_stopped(item="item_u1"):
    return ev("input_audio_buffer.speech_stopped", item_id=item, audio_end_ms=900)


def committed(item="item_u1"):
    return ev("input_audio_buffer.committed", item_id=item, previous_item_id=None)


def in_tx_delta(text, item="item_u1"):
    return ev("conversation.item.input_audio_transcription.delta", item_id=item, delta=text)


def in_tx_done(text, item="item_u1"):
    return ev("conversation.item.input_audio_transcription.completed", item_id=item, transcript=text)


def created(rid="resp_1"):
    return ev("response.created", response=NS(id=rid))


def audio(ms=100, rid="resp_1", item="item_a1"):
    return ev("response.output_audio.delta", response_id=rid, item_id=item, content_index=0, delta=_pcm(ms))


def transcript(text, rid="resp_1", item="item_a1"):
    return ev("response.output_audio_transcript.delta", response_id=rid, item_id=item, content_index=0, delta=text)


def fn_call(name="delegate_to_main", args='{"message": "x"}', rid="resp_1"):
    return ev("response.function_call_arguments.done", response_id=rid, item_id="item_f1",
              name=name, arguments=args, call_id="call_1")


def done(status="completed", rid="resp_1", usage=None):
    return ev("response.done", response=NS(id=rid, status=status, usage=usage))


def _agent(*, conn=None):
    agent = object.__new__(OpenAIRealtimeAgent)
    agent._recv_queue = queue.Queue()
    agent._turn_done = threading.Event()
    agent._conn_lock = threading.RLock()
    agent._connection = conn
    agent._config = NS(sample_rate=24000, model="gpt-realtime-2")
    return agent


def _run(events, *, agent=None, conn=None):
    agent = agent or _agent(conn=conn)
    completed = agent._sync_receive_turn(iter(events) if conn is None else _Conn(conn, events))
    return agent, completed, _drain(agent)


class _Conn:
    """Iterable connection snapshot that also exposes the truncate resource."""

    def __init__(self, mock, events):
        self._mock, self._events = mock, list(events)
        self.conversation = mock.conversation

    def __iter__(self):
        return iter(self._events)


def _drain(agent):
    events = []
    while not agent._recv_queue.empty():
        event = agent._recv_queue.get_nowait()
        events.append(event.output if isinstance(event, OutputEvent) else event)
    return events


def _raw(agent_queue):
    items = []
    while not agent_queue.empty():
        items.append(agent_queue.get_nowait())
    return items


def test_server_vad_and_transcript_share_one_input_key(monkeypatch):
    monkeypatch.setattr("hal.realtime.voice_agent.openai_realtime.time.monotonic", lambda: 42.0)
    _, completed, events = _run([
        speech_started(), in_tx_delta("Find "), speech_stopped(),
        created(), transcript("Looking now"), in_tx_done("Find a flight"), audio(), done(),
    ])
    assert completed
    speech = [e for e in events if isinstance(e, UserSpeechOutput)]
    assert len(speech) == 4
    key = speech[0].turn_id
    assert key.startswith("openai-")
    assert all(e.turn_id == key for e in speech)
    assert speech[0].transcript == "" and speech[0].endpoint_at is None
    assert speech[1].transcript == "Find " and speech[1].method == "provider_transcript"
    assert speech[2].endpoint_at == 42.0 and speech[2].method == "server_vad"
    assert speech[3].transcript == "a flight" and speech[3].transcript_finished is True
    assert "".join(e.transcript for e in speech) == "Find a flight"
    assert all(e.user_turn_id == key for e in events if not isinstance(e, UserSpeechOutput))
    assert isinstance(events[-1], TurnDoneEvent) and events[-1].execution_completed
    reset = next(e for e in events if isinstance(e, InterruptedOutput))
    assert reset.reason == "output_reset" and reset.at is None
    assert events.index(reset) < events.index(next(e for e in events if isinstance(e, TextOutput)))


def test_unsolicited_response_has_no_user_task():
    _, _, events = _run([created(), transcript("Welcome"), done()])
    assert not any(isinstance(e, UserSpeechOutput) for e in events)
    assert all(e.user_turn_id == "" for e in events)


def test_late_transcription_is_attributed_to_its_own_item_not_the_new_input():
    agent, _, first = _run([speech_started("item_u1"), speech_stopped("item_u1"), created(), transcript("Hi"), done()])
    key1 = next(e for e in first if isinstance(e, UserSpeechOutput)).turn_id
    _, _, second = _run([speech_started("item_u2"), in_tx_done("first words", "item_u1"),
                         in_tx_delta("second", "item_u2"), created("resp_2"), transcript("Ok", "resp_2"), done(rid="resp_2")],
                        agent=agent)
    speech = [e for e in second if isinstance(e, UserSpeechOutput)]
    late = next(e for e in speech if e.transcript == "first words")
    assert late.turn_id == key1
    key2 = speech[0].turn_id
    assert key2 != key1
    assert next(e for e in speech if e.transcript == "second").turn_id == key2
    assert next(e for e in second if isinstance(e, TextOutput)).user_turn_id == key2
    assert second[-1].user_turn_id == key2


def test_completion_only_transcription_cannot_create_a_task():
    _, _, events = _run([in_tx_done("", "item_zz"), created(), done()])
    assert not any(isinstance(e, UserSpeechOutput) for e in events)


def test_turn_mode_does_not_emit_live_user_metadata(monkeypatch):
    monkeypatch.setattr(config, "LIVE_MODE", False)
    agent, _, events = _run([committed(), in_tx_done("Hello"), created(), fn_call(), done()])
    assert not any(isinstance(e, UserSpeechOutput) for e in events)
    assert not any(isinstance(e, InterruptedOutput) for e in events)
    call = next(e for e in events if isinstance(e, FunctionCallOutput))
    assert call.user_transcript == "Hello"


def test_barge_in_drains_queue_announces_interrupt_bumps_gen_and_truncates():
    conn = Mock()
    agent = _agent(conn=None)
    agent._connection = None  # set after _Conn exists so identity matches
    conn_snapshot = _Conn(conn, [
        speech_started("item_u1"), speech_stopped("item_u1"),
        created(), transcript("Long reply "), audio(500), audio(500), audio(500),
        speech_started("item_u2"),
        audio(500),
        done(status="cancelled"),
    ])
    agent._connection = conn_snapshot
    raw_before = None
    completed = agent._sync_receive_turn(conn_snapshot)
    assert completed
    raw = _raw(agent._recv_queue)
    outputs = [r.output if isinstance(r, OutputEvent) else r for r in raw]
    speeches = [e for e in outputs if isinstance(e, UserSpeechOutput)]
    key1, key2 = speeches[0].turn_id, speeches[-1].turn_id
    assert key1 != key2
    assert not any(isinstance(e, (AudioOutput, TextOutput)) for e in outputs)
    interrupt = next(e for e in outputs if isinstance(e, InterruptedOutput) and e.reason == "server_interrupt")
    assert interrupt.at is not None and interrupt.user_turn_id == key1
    gen_of = {id(r.output): r.gen for r in raw if isinstance(r, OutputEvent)}
    assert gen_of[id(interrupt)] == agent._turn_gen == 2
    assert isinstance(outputs[-1], TurnDoneEvent)
    assert outputs[-1].user_turn_id == key1 and not outputs[-1].execution_completed
    # Server-side truncate at (received - still queued) = 1500ms - 1500ms → 0.
    conn.conversation.item.truncate.assert_called_once()
    kw = conn.conversation.item.truncate.call_args.kwargs
    assert kw["item_id"] == "item_a1" and kw["content_index"] == 0
    assert kw["audio_end_ms"] == 0 and kw["event_id"] == openai_realtime._TRUNCATE_EVENT_ID
    assert agent._live_user_turn_id == key2


def test_truncate_accounts_for_audio_already_handed_to_the_consumer():
    conn = Mock()
    agent = _agent()

    class _LiveConn(_Conn):
        """The consumer drains chunks while the response streams."""

        def __iter__(self):
            for event in self._events:
                yield event
                if getattr(event, "type", "") == "response.output_audio.delta" and not getattr(self, "_taken", False):
                    self._taken = True
                    agent._recv_queue.get_nowait()
                    agent._recv_queue.get_nowait()

    snapshot = _LiveConn(conn, [
        speech_started(), created(), audio(400), audio(400), speech_started("item_u2"), done(status="cancelled"),
    ])
    agent._connection = snapshot
    agent._sync_receive_turn(snapshot)
    # received 800 ms, 400 ms still queued (dropped) → the user heard ~400 ms.
    assert conn.conversation.item.truncate.call_args.kwargs["audio_end_ms"] == 400


def test_cancelled_done_without_speech_started_still_interrupts():
    conn = Mock()
    agent = _agent()
    snapshot = _Conn(conn, [created(), transcript("Half a"), audio(200), done(status="cancelled")])
    agent._connection = snapshot
    agent._sync_receive_turn(snapshot)
    events = _drain(agent)
    assert not any(isinstance(e, (AudioOutput, TextOutput)) for e in events)
    assert any(isinstance(e, InterruptedOutput) and e.reason == "server_interrupt" for e in events)
    assert isinstance(events[-1], TurnDoneEvent) and not events[-1].execution_completed
    conn.conversation.item.truncate.assert_called_once()


def test_dropped_terminal_is_kept_as_execution_evidence():
    conn = Mock()
    agent = _agent()
    agent._recv_queue.put(TurnDoneEvent(execution_completed=True, user_turn_id="previous"))
    snapshot = _Conn(conn, [created(), audio(), speech_started("item_u9"), done(status="cancelled")])
    agent._connection = snapshot
    agent._sync_receive_turn(snapshot)
    events = _drain(agent)
    preserved = next(e for e in events if isinstance(e, ExecutionOutput))
    assert preserved.user_turn_id == "previous" and preserved.execution_completed
    assert sum(isinstance(e, TurnDoneEvent) for e in events) == 1
    assert isinstance(events[-1], TurnDoneEvent)


def test_speech_started_without_active_response_is_just_a_new_input():
    conn = Mock()
    agent = _agent()
    snapshot = _Conn(conn, [speech_started(), speech_stopped(), created(), transcript("Hi"), done()])
    agent._connection = snapshot
    agent._sync_receive_turn(snapshot)
    events = _drain(agent)
    assert not any(isinstance(e, InterruptedOutput) and e.reason == "server_interrupt" for e in events)
    conn.conversation.item.truncate.assert_not_called()
    assert events[-1].execution_completed


def test_benign_errors_do_not_end_the_session():
    err_commit = ev("error", error=NS(code="input_audio_buffer_commit_empty", message="empty", event_id=None))
    err_trunc = ev("error", error=NS(code="invalid_value", message="audio_end_ms too large",
                                     event_id=openai_realtime._TRUNCATE_EVENT_ID))
    _, completed, events = _run([err_commit, err_trunc, created(), transcript("ok"), done()])
    assert completed and events[-1].execution_completed


def test_other_errors_still_fail_fast():
    from hal.realtime.exceptions import OpenAIRealtimeError
    with pytest.raises(OpenAIRealtimeError):
        _run([ev("error", error=NS(code="server_error", message="boom", event_id=None))])


def test_every_inbound_event_feeds_the_silent_turn_watchdog(monkeypatch):
    agent = _agent()
    seen = []
    monkeypatch.setattr(agent, "note_server_activity", lambda: seen.append(1))
    agent._sync_receive_turn(iter([ev("rate_limits.updated"), ev("session.updated"), created(), done()]))
    assert len(seen) == 4


def test_usage_line_prices_per_modality_with_cache_discount(caplog):
    usage = NS(
        input_tokens=1200, output_tokens=300, total_tokens=1500,
        input_token_details=NS(text_tokens=1000, audio_tokens=200, cached_tokens=800,
                               cached_tokens_details=NS(text_tokens=800, audio_tokens=0)),
        output_token_details=NS(text_tokens=100, audio_tokens=200),
    )
    with caplog.at_level(logging.INFO, logger="hal.realtime.usage.openai"):
        _run([created(), done(usage=usage)])
    line = next(r.message for r in caplog.records if "OpenAI usage" in r.message)
    assert "model=gpt-realtime-2" in line
    # gpt-realtime-2: text in $4, audio in $32, text out $24, audio out $64 per 1M
    full = (1000 * 4 + 200 * 32 + 100 * 24 + 200 * 64) / 1e6
    cached = full - 800 * (4 - 0.40) / 1e6
    assert f"est_full>=${full:.5f}" in line and f"est_cached>=${cached:.5f}" in line
    assert "cached=800tok total=1500tok" in line


def test_generation_advances_per_receive_turn():
    agent = _agent()
    agent._sync_receive_turn(iter([created(), audio(), done()]))
    first = _raw(agent._recv_queue)
    agent._sync_receive_turn(iter([created("resp_2"), audio(rid="resp_2"), done(rid="resp_2")]))
    second = _raw(agent._recv_queue)
    assert first[0].gen == 1 and second[0].gen == 2


def test_function_call_carries_owner_and_transcript_then_clears_it():
    agent, _, events = _run([speech_started(), in_tx_done("Turn on the light"), created(), fn_call(), done()])
    call = next(e for e in events if isinstance(e, FunctionCallOutput))
    key = next(e for e in events if isinstance(e, UserSpeechOutput)).turn_id
    assert call.user_turn_id == key and call.user_transcript == "Turn on the light"
    assert agent._user_transcript == ""


def _config_agent(**overrides):
    from hal.realtime.config import OpenAIConfig
    agent = object.__new__(OpenAIRealtimeAgent)
    agent._config = OpenAIConfig(instructions="be brief", **overrides)
    agent._tools = [{"type": "function", "name": "delegate_to_main", "description": "d",
                     "parameters": {"type": "object", "properties": {}}}]
    return agent


@pytest.mark.parametrize("td,end", [(None, ""), ("server_vad", ""), ("semantic_vad", "high")])
def test_session_payload_matches_the_ga_schema(td, end):
    from openai.types.realtime.realtime_session_create_request import RealtimeSessionCreateRequest
    from hal.realtime.enums import OpenAITurnDetectionType
    agent = _config_agent(
        turn_detection_type=OpenAITurnDetectionType(td) if td else None,
        vad_start_sensitivity="low", vad_prefix_padding_ms=300, vad_silence_ms=0,
        vad_end_sensitivity=end, language="vi-VN", noise_reduction="far_field",
    )
    session = agent._build_session()
    RealtimeSessionCreateRequest.model_validate(session)
    audio_in = session["audio"]["input"]
    assert session["output_modalities"] == ["audio"]
    assert audio_in["transcription"] == {"model": agent._config.transcribe_model, "language": "vi"}
    assert audio_in["noise_reduction"] == {"type": "far_field"}
    if td is None:
        assert audio_in["turn_detection"] is None
    elif td == "server_vad":
        assert audio_in["turn_detection"] == {"type": "server_vad", "threshold": 0.7, "prefix_padding_ms": 300}
    else:
        assert audio_in["turn_detection"] == {"type": "semantic_vad", "eagerness": "high"}


def test_explicit_threshold_wins_and_off_disables_noise_reduction():
    from hal.realtime.enums import OpenAITurnDetectionType
    agent = _config_agent(turn_detection_type=OpenAITurnDetectionType.SERVER_VAD,
                          vad_threshold=0.55, vad_start_sensitivity="low", noise_reduction="off")
    audio_in = agent._build_session()["audio"]["input"]
    assert audio_in["turn_detection"]["threshold"] == 0.55
    assert "noise_reduction" not in audio_in


def test_commit_is_skipped_when_server_vad_brackets_the_turn():
    from hal.realtime.enums import OpenAITurnDetectionType
    agent = _config_agent(turn_detection_type=OpenAITurnDetectionType.SERVER_VAD)
    agent._conn_lock = threading.RLock()
    agent._connection = Mock()
    agent._turn_done = threading.Event(); agent._turn_done.set()
    agent._sync_commit(None)
    agent._connection.input_audio_buffer.commit.assert_not_called()
    agent._connection.response.create.assert_not_called()
    agent._config = _config_agent(turn_detection_type=None)._config
    agent._response_wait_s = 1.0
    agent._sync_commit(None)
    agent._connection.input_audio_buffer.commit.assert_called_once()
    agent._connection.response.create.assert_called_once()
    assert not agent._turn_done.is_set()
