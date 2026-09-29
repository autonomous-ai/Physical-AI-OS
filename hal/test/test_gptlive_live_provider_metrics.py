"""GPT-Live emits the same live-mode contract as Gemini Live — synthesized."""

import base64
import json
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
    FunctionCallResultInput,
    InterruptedOutput,
    OutputEvent,
    TextInput,
    TextOutput,
    TurnDoneEvent,
    UserSpeechOutput,
)
from hal.realtime.voice_agent import gpt_live
from hal.realtime.voice_agent.gpt_live import GPTLiveAgent


@pytest.fixture(autouse=True)
def _live_mode(monkeypatch):
    monkeypatch.setattr(config, "LIVE_MODE", True)


def _pcm(ms: int, rate: int = 24000, level: float = 0.1) -> str:
    """Speech-level tone by default (~-20 dBFS); level=0 for a silent delta."""
    n = rate * ms // 1000
    x = level * np.sin(2 * np.pi * 440 * np.arange(n) / rate)
    return base64.b64encode((x * 32767).astype(np.int16).tobytes()).decode()


def ev(type_: str, **fields):
    return NS(type=type_, **fields)


def started(sid="sess_1"):
    return ev("session.started", session=NS(id=sid, expires_at=None))


def in_tx(text, start_ms, end_ms):
    return ev("session.input_transcript.delta", delta=text, start_ms=start_ms, end_ms=end_ms)


def out_audio(ms=100):
    return ev("session.output_audio.delta", delta=_pcm(ms))


def silent_audio(ms=100):
    return ev("session.output_audio.delta", delta=_pcm(ms, level=0.0))


def backend(nested, did="dlg_b"):
    return ev("response.event", delegation_id=did, event=nested)


def out_tx(text):
    return ev("session.output_transcript.delta", delta=text, start_ms=0, end_ms=0)


def delegation(did="dlg_1", target="client"):
    return ev("session.delegation.created", delegation=NS(id=did, target=target, type="delegation"), offset_ms=1000)


def usage(seconds):
    return ev("session.usage.updated", usage=NS(seconds=seconds))


def _agent(**cfg):
    """Client-delegation agent (the mode most of these tests exercise)."""
    from hal.realtime.config import GPTLiveConfig
    from hal.realtime.orchestrator import DELEGATE_TOOL
    cfg.setdefault("delegation", "client")
    agent = GPTLiveAgent(GPTLiveConfig(instructions="be brief", api_key="test", **cfg),
                         tools=[DELEGATE_TOOL, {"name": "express_emotion"}])
    return agent


def _drain(agent):
    events = []
    while not agent._recv_queue.empty():
        event = agent._recv_queue.get_nowait()
        events.append(event.output if isinstance(event, OutputEvent) else event)
    return events


def _raw(agent):
    items = []
    while not agent._recv_queue.empty():
        items.append(agent._recv_queue.get_nowait())
    return items


def test_reply_owned_by_the_input_it_answers_and_boundary_completes_it():
    agent = _agent()
    assert agent._pump_events(iter([
        started(), in_tx("Find ", 0, 400), in_tx("a flight", 400, 900),
        out_tx("Looking now"), out_audio(), out_audio(),
    ])) is False
    assert agent._session_started.is_set()
    assert agent._fire_boundary()
    events = _drain(agent)
    speech = [e for e in events if isinstance(e, UserSpeechOutput)]
    key = speech[0].turn_id
    assert key.startswith("gptlive-")
    assert [s.transcript for s in speech] == ["Find ", "a flight", ""]
    assert speech[-1].transcript_finished is True
    assert all(s.endpoint_at is None and s.method == "provider_transcript" for s in speech)
    assert all(e.user_turn_id == key for e in events if not isinstance(e, UserSpeechOutput))
    reset = next(e for e in events if isinstance(e, InterruptedOutput))
    assert reset.reason == "output_reset"
    assert events.index(reset) < events.index(next(e for e in events if isinstance(e, TextOutput)))
    assert isinstance(events[-1], TurnDoneEvent) and events[-1].execution_completed
    assert events[-1].user_turn_id == key
    assert agent._live_user_turn_id == ""


def test_unsolicited_remark_has_no_owner_and_second_reply_is_new_generation():
    agent = _agent()
    agent._pump_events(iter([started(), out_tx("Welcome"), out_audio()]))
    agent._fire_boundary()
    first = _raw(agent)
    outputs = [r.output if isinstance(r, OutputEvent) else r for r in first]
    assert not any(isinstance(e, UserSpeechOutput) for e in outputs)
    assert all(e.user_turn_id == "" for e in outputs)
    gen1 = first[0].gen
    agent._pump_events(iter([out_audio()]))
    agent._fire_boundary()
    second = _raw(agent)
    assert second[0].gen == gen1 + 1


def test_input_gap_opens_a_new_turn_even_without_a_reply():
    agent = _agent(input_gap_ms=1500)
    agent._pump_events(iter([started(), in_tx("first", 0, 500), in_tx("second", 3000, 3500)]))
    speech = [e for e in _drain(agent) if isinstance(e, UserSpeechOutput)]
    assert speech[0].turn_id != speech[1].turn_id
    assert agent._user_transcript == "second"


def test_turn_mode_emits_no_live_metadata_but_keeps_the_transcript(monkeypatch):
    monkeypatch.setattr(config, "LIVE_MODE", False)
    agent = _agent()
    agent._pump_events(iter([started(), in_tx("Turn on the light", 0, 800), delegation()]))
    agent._flush_deferred_delegation(force=True)
    events = _drain(agent)
    assert not any(isinstance(e, (UserSpeechOutput, InterruptedOutput)) for e in events)
    call = next(e for e in events if isinstance(e, FunctionCallOutput))
    assert call.user_transcript == "Turn on the light"
    assert call.user_turn_id == ""
    agent._pump_events(iter([out_audio()]))
    agent._fire_boundary()
    assert all(e.user_turn_id == "" for e in _drain(agent))


def test_user_talking_over_then_model_falling_silent_is_an_interruption():
    agent = _agent()
    agent._pump_events(iter([
        started(), in_tx("Tell me a story", 0, 900),
        out_tx("Once upon "), out_audio(300), out_audio(300),
        in_tx("stop", 2000, 2300),
    ]))
    assert agent._overlap_pending
    assert agent._fire_boundary()
    raw = _raw(agent)
    events = [r.output if isinstance(r, OutputEvent) else r for r in raw]
    speeches = [e for e in events if isinstance(e, UserSpeechOutput)]
    key1, key2 = speeches[0].turn_id, speeches[-1].turn_id
    assert key1 != key2
    assert not any(isinstance(e, (AudioOutput, TextOutput)) for e in events)
    interrupt = next(e for e in events if isinstance(e, InterruptedOutput) and e.reason == "server_interrupt")
    assert interrupt.user_turn_id == key1 and interrupt.at is not None
    gens = {id(r.output): r.gen for r in raw if isinstance(r, OutputEvent)}
    assert gens[id(interrupt)] == agent._turn_gen
    assert isinstance(events[-1], TurnDoneEvent)
    assert events[-1].user_turn_id == key1 and not events[-1].execution_completed
    assert agent._live_user_turn_id == key2 and not agent._input_answered


def test_model_keeps_talking_after_overlap_is_a_backchannel(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("hal.realtime.voice_agent.gpt_live.time.monotonic", lambda: clock[0])
    agent = _agent(interrupt_gap_ms=400)
    agent._pump_events(iter([started(), in_tx("Tell me", 0, 500), out_tx("Once upon"), out_audio()]))
    agent._pump_events(iter([in_tx("mhm", 900, 1000)]))
    assert agent._overlap_pending
    clock[0] += 0.2
    agent._pump_events(iter([out_audio()]))
    assert agent._overlap_pending
    assert agent._boundary_deadline == pytest.approx(clock[0] + 0.4)
    clock[0] += 0.5
    agent._pump_events(iter([out_audio()]))
    assert not agent._overlap_pending
    agent._fire_boundary()
    events = _drain(agent)
    assert not any(isinstance(e, InterruptedOutput) and e.reason == "server_interrupt" for e in events)
    assert events[-1].execution_completed


def test_dropped_terminal_is_kept_as_execution_evidence():
    agent = _agent()
    agent._recv_queue.put(TurnDoneEvent(execution_completed=True, user_turn_id="previous"))
    agent._pump_events(iter([started(), in_tx("Go on", 0, 400), out_audio(), in_tx("wait", 800, 900)]))
    agent._fire_boundary()
    events = _drain(agent)
    preserved = next(e for e in events if isinstance(e, ExecutionOutput))
    assert preserved.user_turn_id == "previous" and preserved.execution_completed
    assert sum(isinstance(e, TurnDoneEvent) for e in events) == 1
    assert isinstance(events[-1], TurnDoneEvent)


def test_client_delegation_becomes_delegate_to_main_with_the_transcript(monkeypatch):
    clock = [50.0]
    monkeypatch.setattr("hal.realtime.voice_agent.gpt_live.time.monotonic", lambda: clock[0])
    agent = _agent()
    agent._pump_events(iter([started(), in_tx("Play some ", 0, 500), in_tx("music", 500, 800), delegation("dlg_9")]))
    early = _drain(agent)
    assert not any(isinstance(e, FunctionCallOutput) for e in early)
    key = next(e for e in early if isinstance(e, UserSpeechOutput)).turn_id
    assert agent._flush_deferred_delegation() is False
    clock[0] += 0.3
    assert agent._flush_deferred_delegation() is True
    events = _drain(agent)
    call = next(e for e in events if isinstance(e, FunctionCallOutput))
    assert call.name == "delegate_to_main" and call.call_id == "dlg_9"
    assert json.loads(call.arguments) == {"message": "Play some music"}
    assert call.user_transcript == "Play some music" and call.user_turn_id == key
    assert agent._pending_delegations == {"dlg_9": key}


def test_delegation_waits_for_the_rest_of_the_sentence(monkeypatch):
    """BFF doc §6: a delegation can arrive before the transcript is complete."""
    clock = [50.0]
    monkeypatch.setattr("hal.realtime.voice_agent.gpt_live.time.monotonic", lambda: clock[0])
    agent = _agent(delegation_wait_ms=800)
    agent._pump_events(iter([started(), in_tx("turn off", 0, 400), delegation("dlg_p")]))
    clock[0] += 0.1
    agent._pump_events(iter([in_tx(" the light", 400, 800)]))
    clock[0] += 0.1
    assert agent._flush_deferred_delegation() is False
    clock[0] += 0.3
    assert agent._flush_deferred_delegation() is True
    call = next(e for e in _drain(agent) if isinstance(e, FunctionCallOutput))
    assert json.loads(call.arguments)["message"] == "turn off the light"


def test_delegation_hard_deadline_forwards_whatever_was_heard(monkeypatch):
    clock = [50.0]
    monkeypatch.setattr("hal.realtime.voice_agent.gpt_live.time.monotonic", lambda: clock[0])
    agent = _agent(delegation_wait_ms=500)
    agent._pump_events(iter([started(), in_tx("find my", 0, 300), delegation("dlg_h")]))
    for _ in range(4):
        clock[0] += 0.15
        agent._pump_events(iter([in_tx(" x", 300, 400)]))
        assert agent._flush_deferred_delegation() is (clock[0] - 50.0 >= 0.5)


def test_delegation_before_transcript_waits_for_it(monkeypatch):
    clock = [50.0]
    monkeypatch.setattr("hal.realtime.voice_agent.gpt_live.time.monotonic", lambda: clock[0])
    agent = _agent(delegation_wait_ms=500)
    agent._pump_events(iter([started(), delegation("dlg_2")]))
    assert not any(isinstance(e, FunctionCallOutput) for e in _drain(agent))
    assert agent._flush_deferred_delegation() is False
    agent._pump_events(iter([in_tx("Remind me at seven", 0, 900)]))
    clock[0] += 0.3
    assert agent._flush_deferred_delegation() is True
    call = next(e for e in _drain(agent) if isinstance(e, FunctionCallOutput))
    assert json.loads(call.arguments)["message"] == "Remind me at seven"


def test_delegation_with_no_transcript_at_all_is_forwarded_empty(monkeypatch):
    clock = [50.0]
    monkeypatch.setattr("hal.realtime.voice_agent.gpt_live.time.monotonic", lambda: clock[0])
    agent = _agent(delegation_wait_ms=500)
    agent._pump_events(iter([started(), delegation("dlg_3")]))
    clock[0] += 1.0
    assert agent._flush_deferred_delegation() is True
    call = next(e for e in _drain(agent) if isinstance(e, FunctionCallOutput))
    assert json.loads(call.arguments)["message"] == ""


def test_responses_delegation_is_ignored():
    agent = _agent()
    agent._pump_events(iter([started(), in_tx("hi", 0, 100), delegation("dlg_r", target="responses")]))
    assert not any(isinstance(e, FunctionCallOutput) for e in _drain(agent))


def test_delegate_ack_and_main_reply_flow_back_as_thinking_context():
    agent = _agent()
    agent._pump_events(iter([started(), in_tx("Play music", 0, 500), delegation("dlg_5")]))
    agent._flush_deferred_delegation(force=True)
    _drain(agent)
    conn = Mock()
    agent._connection = conn
    agent._sync_send_input(FunctionCallResultInput(call_id="dlg_5", output='{"result": "delegated"}'))
    kw = conn.session.thinking.append.call_args.kwargs
    assert kw["delegation_id"] == "dlg_5" and "main agent" in kw["content"]
    assert "dlg_5" in agent._pending_delegations
    agent._sync_send_input(TextInput(text="[TTS HISTORY] Playing jazz for you."))
    kw = conn.session.thinking.append.call_args.kwargs
    assert kw["delegation_id"] == "dlg_5" and kw["content"].startswith("[TTS HISTORY]")
    assert agent._pending_delegations == {}
    agent._sync_send_input(TextInput(text="[TURN CONTEXT] speaker=Leo"))
    assert conn.session.thinking.append.call_args.kwargs["delegation_id"] is None
    agent._pump_events(iter([in_tx("Find my pen", 3000, 3800), delegation("dlg_6")]))
    agent._flush_deferred_delegation(force=True)
    _drain(agent)
    agent._sync_send_input(FunctionCallResultInput(call_id="dlg_6", output='{"error": "message must not be empty"}'))
    assert "failed" in conn.session.thinking.append.call_args.kwargs["content"]
    assert "dlg_6" not in agent._pending_delegations
    n = conn.session.thinking.append.call_count
    agent._sync_send_input(FunctionCallResultInput(call_id="call_emotion", output='{"result": "expressed"}'))
    assert conn.session.thinking.append.call_count == n


def test_context_is_clipped_to_the_append_limit():
    agent = _agent()
    agent._session_started.set()
    conn = Mock(); agent._connection = conn
    agent._sync_send_input(TextInput(text="x" * 5000))
    assert len(conn.session.thinking.append.call_args.kwargs["content"]) <= gpt_live._APPEND_MAX_CHARS


def test_sends_wait_for_session_started_and_commit_pads_silence_in_turn_mode(monkeypatch):
    from hal.realtime.models import AudioInput
    agent = _agent(start_timeout_s=0.05, commit_silence_ms=600, sample_rate=16000)
    conn = Mock(); agent._connection = conn
    agent._sync_send_input(AudioInput(audio=np.zeros(160, dtype=np.float32)))
    conn.session.input_audio.append.assert_not_called()
    agent._session_started.set()
    agent._sync_send_input(AudioInput(audio=np.zeros(160, dtype=np.float32)))
    kw = conn.session.input_audio.append.call_args.kwargs
    assert len(base64.b64decode(kw["audio"])) == 320 and kw["event_id"] == gpt_live._EVT_AUDIO
    monkeypatch.setattr(config, "LIVE_MODE", False)
    agent._sync_commit()
    kw = conn.session.input_audio.append.call_args.kwargs
    assert len(base64.b64decode(kw["audio"])) == 16000 * 600 // 1000 * 2 and kw["event_id"] == gpt_live._EVT_SILENCE
    assert agent._awaiting_reply
    monkeypatch.setattr(config, "LIVE_MODE", True)
    n = conn.session.input_audio.append.call_count
    agent._sync_commit()
    assert conn.session.input_audio.append.call_count == n


def test_rejected_own_command_is_a_warning_and_other_errors_are_fatal():
    from hal.realtime.exceptions import GPTLiveError
    agent = _agent()
    agent._pump_events(iter([
        started(),
        ev("error", error=NS(code="invalid_value", message="too long", client_event_id=None), client_event_id=gpt_live._EVT_CONTEXT),
        out_audio(),
    ]))
    assert any(isinstance(e, AudioOutput) for e in _drain(agent))
    with pytest.raises(GPTLiveError):
        agent._pump_events(iter([ev("error", error=NS(code="server_error", message="boom", client_event_id=None), client_event_id=None)]))
    with pytest.raises(GPTLiveError):
        agent._pump_events(iter([ev("error", error=NS(code="invalid_request", message="bad model", client_event_id=None), client_event_id=gpt_live._EVT_START)]))


def test_fail_fast_ends_a_reply_in_flight_or_a_pending_question():
    agent = _agent()
    agent._pump_events(iter([started(), in_tx("Hello", 0, 300), out_audio()]))
    agent._fail_fast_turn("session closed")
    events = _drain(agent)
    assert isinstance(events[-1], TurnDoneEvent) and not events[-1].execution_completed
    agent._pump_events(iter([in_tx("Anyone there?", 5000, 5600)]))
    _drain(agent)
    agent._fail_fast_turn("session closed")
    assert isinstance(_drain(agent)[-1], TurnDoneEvent)
    agent._fail_fast_turn("idle")
    assert _drain(agent) == []


def test_usage_line_prices_session_minutes(caplog):
    agent = _agent()
    agent._request_id = "hal-abc"
    with caplog.at_level(logging.INFO, logger="hal.realtime.usage.gptlive"):
        agent._pump_events(iter([started("sess_7"), usage(90.0)]))
    line = next(r.message for r in caplog.records if "GPT-Live usage" in r.message)
    assert "session=sess_7 request=hal-" in line and "seconds=90.0 est>=$0.0750 context=-" in line


def test_liveness_is_noted_for_content_not_usage_ticks(monkeypatch):
    agent = _agent()
    seen = []
    monkeypatch.setattr(agent, "note_server_activity", lambda: seen.append(1))
    agent._pump_events(iter([started(), usage(1.0), ev("session.thinking.appended"), out_audio(), in_tx("x", 0, 1)]))
    assert len(seen) == 3


def test_session_payload_matches_the_sdk_schema_and_uses_client_delegation():
    from openai.types.live import SessionConfig
    agent = _agent(sample_rate=16000)
    session = agent._build_session()
    SessionConfig.model_validate(session)
    assert session["delegation"] == {"type": "client"}
    assert session["audio"]["format"] == {"type": "audio/pcm", "rate": 16000}
    assert session["audio"]["output"]["voice"] == "marin"
    assert "tools" not in session and "turn_detection" not in json.dumps(session)


def test_web_search_selects_responses_delegation_with_our_delegate_function():
    from openai.types.live import SessionConfig
    agent = _agent(delegation="auto", web_search=True, backend_model="gpt-5.6-luna", language="vi")
    assert agent._config.responses_mode
    session = agent._build_session()
    SessionConfig.model_validate(session)
    d = session["delegation"]
    assert d["type"] == "responses" and d["responses"]["model"] == "gpt-5.6-luna"
    kinds = [(t["type"], t.get("name")) for t in d["responses"]["tools"]]
    assert kinds == [("web_search", None), ("function", "delegate_to_main")]
    assert "Vietnamese" in d["responses"]["instructions"]
    assert d["responses"]["tool_choice"] == "auto" and d["responses"]["parallel_tool_calls"] is False
    assert not _agent(delegation="client", web_search=True)._config.responses_mode
    only_fn = _agent(delegation="responses", web_search=False)._build_session()["delegation"]["responses"]["tools"]
    assert [t["type"] for t in only_fn] == ["function"]


def test_reconnect_clears_ownership_and_delegations():
    agent = _agent()
    agent._pump_events(iter([started(), in_tx("Play music", 0, 500), delegation("dlg_1"), out_audio()]))
    mgr = Mock(); conn = Mock(); mgr.enter.return_value = conn
    agent._client = Mock(); agent._client.live.connect.return_value = mgr
    agent._sync_connect()
    assert agent._live_user_turn_id == "" and agent._pending_delegations == {} and not agent._output_active
    assert not agent._session_started.is_set()
    conn.session.start.assert_called_once()
    assert conn.session.start.call_args.kwargs["event_id"] == gpt_live._EVT_START


def test_delegation_reaches_the_orchestrator_as_a_delegate_signal(monkeypatch):
    from hal.realtime.models import InputEvent
    from hal.realtime.models.signal import DelegateSignal
    from hal.realtime.orchestrator import RealtimeOrchestrator

    agent = _agent()
    agent._connected.set()
    agent._pump_events(iter([started(), in_tx("Play some jazz", 0, 900), delegation("dlg_42")]))
    agent._flush_deferred_delegation(force=True)
    agent._fire_boundary()
    agent._recv_queue.put(TurnDoneEvent())

    orch = object.__new__(RealtimeOrchestrator)
    orch._agent = agent
    orch._vision_enabled = False
    orch._expression_enabled = False
    # post-turn bookkeeping the bare instance lacks (see __init__)
    orch._skip_post_idle_recycle = False
    orch._consecutive_silent = 0
    orch._idle_reset_pending = False
    orch._last_activity_monotonic = 0.0
    orch._last_turn_monotonic = 0.0
    orch._turn_in_flight = False
    orch._turns_since_recycle = 0
    orch._force_rebuild = lambda: None
    outputs = list(orch.stream_output())
    signal = next(o for o in outputs if isinstance(o, DelegateSignal))
    key = next(o for o in outputs if isinstance(o, UserSpeechOutput)).turn_id
    assert signal.message == "Play some jazz" and signal.transcript == "Play some jazz"
    assert signal.user_turn_id == key
    queued = agent._send_queue.get_nowait()
    assert isinstance(queued, InputEvent) and isinstance(queued.input, FunctionCallResultInput)
    assert queued.input.call_id == "dlg_42" and json.loads(queued.input.output) == {"result": "delegated"}


def test_adapter_reads_sdk_parsed_events_not_just_fakes(caplog):
    """Parse wire-shaped JSON through the SDK parser, as LiveConnection.recv does."""
    from openai.resources.live.live import LiveConnection

    conn = object.__new__(LiveConnection)
    raw = [
        {"type": "session.started", "event_id": "e1", "session": {"id": "sess_x", "model": "gpt-live-1",
         "status": "active", "expires_at": 1789600000}},
        {"type": "session.input_transcript.delta", "event_id": "e2", "delta": "Play jazz", "start_ms": 0, "end_ms": 700},
        {"type": "session.delegation.created", "event_id": "e3", "offset_ms": 900,
         "delegation": {"id": "item_d1", "type": "delegation", "target": "client"}},
        {"type": "session.output_transcript.delta", "event_id": "e4", "delta": "On it", "start_ms": 1000, "end_ms": 1400},
        {"type": "session.output_audio.delta", "event_id": "e5", "delta": _pcm(50)},
        {"type": "session.usage.updated", "event_id": "e6", "usage": {"seconds": 12.5}, "usage_ratio": 0.01},
        {"type": "error", "event_id": "e7", "client_event_id": gpt_live._EVT_CONTEXT,
         "error": {"type": "invalid_request_error", "code": "content_too_long", "message": "over 500 tokens"}},
        {"type": "session.closed", "event_id": "e8", "reason": "expired",
         "session": {"id": "sess_x", "model": "gpt-live-1", "status": "active"}, "usage": {"seconds": 13.0}},
    ]
    events = [conn.parse_event(json.dumps(r)) for r in raw]
    agent = _agent()
    with caplog.at_level(logging.INFO):
        assert agent._pump_events(iter(events)) is False
    agent._flush_deferred_delegation(force=True)
    outputs = _drain(agent)
    assert agent._session_closed.is_set() and agent._usage_ratio == 0.01
    assert agent._session_id == "sess_x" and not agent._session_started.is_set()
    assert agent._session_started_once
    assert next(e for e in outputs if isinstance(e, UserSpeechOutput)).transcript == "Play jazz"
    call = next(e for e in outputs if isinstance(e, FunctionCallOutput))
    assert call.call_id == "item_d1" and json.loads(call.arguments)["message"] == "Play jazz"
    assert next(e for e in outputs if isinstance(e, TextOutput)).text == "On it"
    assert any(isinstance(e, AudioOutput) for e in outputs)
    assert agent._usage_seconds == 13.0
    assert any("GPT-Live rejected hal-context" in r.message for r in caplog.records)


@pytest.mark.parametrize("code", [4001, 4002, 4029])
def test_no_retry_close_codes_jump_to_max_backoff(code, caplog):
    agent = _agent()
    assert agent._reconnect_backoff == 2.0
    with caplog.at_level(logging.WARNING):
        agent._note_close_code(code, "GPT-Live usage limit reached")
    assert agent._reconnect_backoff == agent._reconnect_backoff_max
    assert any(str(code) in r.message and "next retry in ~60s" in r.message for r in caplog.records)
    agent._note_close_code(1011, "upstream")
    assert agent._reconnect_backoff == agent._reconnect_backoff_max


def test_graceful_close_waits_for_session_closed_only_on_owner_teardown():
    agent = _agent(close_timeout_s=0.2)
    conn = Mock(); agent._connection = conn
    agent._session_started_once = True
    agent._session_closed.set()
    agent._do_disconnect()
    conn.session.close.assert_called_once()
    conn.close.assert_called_once()
    agent._connection = Mock(); agent._session_closed.clear()
    import time as _t
    t0 = _t.monotonic(); agent._sync_disconnect(); assert _t.monotonic() - t0 < 0.1


def test_connect_sends_the_correlation_header():
    agent = _agent()
    mgr = Mock(); conn = Mock(); mgr.enter.return_value = conn
    agent._client = Mock(); agent._client.live.connect.return_value = mgr
    agent._sync_connect()
    headers = agent._client.live.connect.call_args.kwargs["extra_headers"]
    assert headers["x-request-id"].startswith("hal-") and headers["x-request-id"] == agent._request_id


def test_silent_output_deltas_are_dropped_and_do_not_start_a_reply():
    agent = _agent()
    agent._pump_events(iter([started(), in_tx("Hi", 0, 300)] + [silent_audio()] * 30))
    assert not agent._output_active and agent._silent_deltas_dropped == 30
    assert not any(isinstance(e, AudioOutput) for e in _drain(agent))
    assert agent._fire_boundary() is False
    assert agent._live_user_turn_id
    agent._pump_events(iter([out_tx("Hello"), out_audio()] + [silent_audio()] * 5))
    assert agent._output_active and agent._fire_boundary()
    events = _drain(agent)
    assert sum(isinstance(e, AudioOutput) for e in events) == 1
    assert events[-1].execution_completed


def test_audio_without_words_is_a_backchannel_not_an_answer():
    agent = _agent()
    agent._pump_events(iter([started(), in_tx("Tell me a joke", 0, 800), out_audio(), out_audio()]))
    key = agent._live_user_turn_id
    assert agent._fire_boundary()
    events = _drain(agent)
    assert not any(isinstance(e, UserSpeechOutput) and e.transcript_finished for e in events)
    assert isinstance(events[-1], TurnDoneEvent) and not events[-1].execution_completed
    assert agent._live_user_turn_id == key and not agent._input_answered
    agent._pump_events(iter([out_tx("Why did"), out_audio(), out_tx(" the lamp…")]))
    assert agent._fire_boundary()
    events = _drain(agent)
    assert next(e for e in events if isinstance(e, UserSpeechOutput)).transcript_finished is True
    assert next(e for e in events if isinstance(e, TextOutput)).user_turn_id == key
    assert events[-1].execution_completed and events[-1].user_turn_id == key
    assert agent._live_user_turn_id == ""


def test_backend_function_call_round_trips_through_the_orchestrator_contract(caplog):
    agent = _agent(delegation="responses", web_search=True)
    conn = Mock(); agent._connection = conn; agent._session_started.set()
    with caplog.at_level(logging.INFO):
        agent._pump_events(iter([
            started(), in_tx("Play some jazz", 0, 900),
            ev("session.delegation.created", delegation=NS(id="dlg_b", target="responses", type="delegation"), offset_ms=900),
            backend({"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc_1"}}),
            backend({"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "call_9",
                                                                     "name": "delegate_to_main", "arguments": '{"message": "Play some jazz"}'}}),
        ]))
    events = _drain(agent)
    assert not any(isinstance(e, FunctionCallOutput) and e.call_id == "dlg_b" for e in events)
    call = next(e for e in events if isinstance(e, FunctionCallOutput))
    key = next(e for e in events if isinstance(e, UserSpeechOutput)).turn_id
    assert call.call_id == "call_9" and json.loads(call.arguments) == {"message": "Play some jazz"}
    assert call.user_turn_id == key and call.user_transcript == "Play some jazz"
    assert agent._pending_function_calls == {"call_9": "dlg_b"}
    agent._sync_send_input(FunctionCallResultInput(call_id="call_9", output='{"result": "delegated"}'))
    item = conn.response.item.create.call_args.kwargs["item"]
    assert item["type"] == "function_call_output" and item["call_id"] == "call_9"
    assert json.loads(item["output"])["result"] == "delegated" and "two-word" in item["output"]
    conn.response.create.assert_called_once()
    conn.session.thinking.append.assert_not_called()
    assert agent._pending_function_calls == {}
    agent._sync_send_input(TextInput(text="[TTS HISTORY] Playing jazz."))
    assert conn.session.thinking.append.call_args.kwargs["delegation_id"] is None


def test_backend_web_search_and_usage_are_logged(caplog):
    agent = _agent(delegation="responses")
    with caplog.at_level(logging.INFO):
        agent._pump_events(iter([
            started(),
            backend({"type": "response.output_item.done", "item": {"type": "web_search_call", "status": "completed",
                                                                     "action": {"type": "search", "query": "weather hanoi"}}}),
            backend({"type": "response.completed", "response": {"model": "gpt-5.6-luna", "status": "completed",
                                                                  "usage": {"input_tokens": 120, "output_tokens": 40, "total_tokens": 160}}}),
        ]))
    assert any("backend searched: 'weather hanoi'" in r.message for r in caplog.records)
    assert any("GPT-Live backend usage: delegation=dlg_b model=gpt-5.6-luna status=completed in=120 out=40 total=160" in r.message
               for r in caplog.records)


def test_filler_while_the_backend_works_does_not_count_as_the_answer():
    """A filler during a web search must not take the answer's input key."""
    agent = _agent(delegation="responses")
    agent._pump_events(iter([
        started(), in_tx("What is the weather in Hanoi", 0, 1200),
        ev("session.delegation.created", delegation=NS(id="dlg_w", target="responses", type="delegation"), offset_ms=1300),
        out_tx(" Mmm."), out_audio(),
    ]))
    key = agent._live_user_turn_id
    assert agent._fire_boundary()
    events = _drain(agent)
    assert not events[-1].execution_completed and agent._live_user_turn_id == key
    agent._pump_events(iter([
        backend({"type": "response.completed", "response": {"status": "completed", "usage": {}}}, did="dlg_w"),
        out_tx(" It's rainy in Hanoi."), out_audio(),
    ]))
    assert agent._fire_boundary()
    events = _drain(agent)
    assert next(e for e in events if isinstance(e, TextOutput)).user_turn_id == key
    assert events[-1].execution_completed and events[-1].user_turn_id == key
    assert agent._live_user_turn_id == "" and agent._backend_busy == {}
