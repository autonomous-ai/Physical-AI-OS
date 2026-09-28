"""pipecat_v1 provider: the VoiceAgentBase contract it produces from pipeline events.

The Pipecat side (pipecat_pipeline.py) reports frames as plain `_ev_*` calls,
so the turn / generation / tool bookkeeping is tested here without a pipeline
and without pipecat installed. The one adapter test that needs the package
skips when it is absent.
"""

import asyncio
import queue
import threading
from types import SimpleNamespace as NS

import numpy as np
import pytest

from hal import config
from hal.realtime.config import PipecatV1Config
from hal.realtime.models import (
    AudioInput,
    FunctionCallOutput,
    InterruptedOutput,
    OutputEvent,
    TextOutput,
    TurnDoneEvent,
    UserSpeechOutput,
)
from hal.realtime.voice_agent.pipecat_v1 import PipecatV1Agent

DELEGATE = "delegate_to_main"
EMOTION = "express_emotion"


class FakeHandle:
    """Records what the send side asks the pipeline to do."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.alive = True

    def queue_audio(self, pcm16):
        self.calls.append(("audio", len(pcm16)))

    def propose_user_turn_start(self):
        self.calls.append(("start",))

    def propose_user_turn_stop(self):
        self.calls.append(("stop",))

    def append_context(self, text):
        self.calls.append(("context", text))

    def finalize_stt(self):
        self.calls.append(("finalize",))

    def remind_tools(self):
        self.calls.append(("remind",))

    def stop(self, timeout_s):
        self.alive = False


def make_agent(monkeypatch, *, live: bool) -> PipecatV1Agent:
    monkeypatch.setattr(config, "LIVE_MODE", live)
    agent = PipecatV1Agent(PipecatV1Config(instructions="x"), tools=[{"name": DELEGATE, "parameters": {}}])
    agent._handle = FakeHandle()
    agent._connected.set()
    return agent


def drain(agent) -> list:
    out = []
    while True:
        try:
            out.append(agent._recv_queue.get_nowait())
        except queue.Empty:
            return out


def reply(agent, *chunks: str):
    agent._ev_response_started()
    for c in chunks:
        agent._ev_text(c)
    agent._ev_response_ended()


# --- turn-based mode --------------------------------------------------------------


def test_text_reply_is_reset_then_text_then_turn_done(monkeypatch):
    agent = make_agent(monkeypatch, live=False)
    agent._ev_user_turn_started()
    reply(agent, "Four", ".")
    events = drain(agent)
    assert isinstance(events[0], OutputEvent) and isinstance(events[0].output, InterruptedOutput)
    assert events[0].output.reason == "output_reset"
    assert [e.output.text for e in events[1:3]] == ["Four", "."]
    assert isinstance(events[3], TurnDoneEvent) and events[3].execution_completed
    assert events[3].user_turn_id == agent._user_turn_id != ""
    # every output of one user turn carries the same generation
    assert {e.gen for e in events[:3]} == {agent._gen}


def test_output_reset_only_once_per_user_turn(monkeypatch):
    agent = make_agent(monkeypatch, live=False)
    agent._ev_user_turn_started()
    reply(agent, "a")
    agent._ev_response_started()  # a tool-result follow-up in the same turn
    agent._ev_text("b")
    agent._ev_response_ended()
    resets = [e for e in drain(agent) if isinstance(e, OutputEvent) and isinstance(e.output, InterruptedOutput)]
    assert len(resets) == 1


def test_first_audio_proposes_start_and_commit_proposes_stop_then_finalizes(monkeypatch):
    agent = make_agent(monkeypatch, live=False)
    frame = np.zeros(1024, dtype=np.float32)
    agent._sync_send_input(AudioInput(audio=frame))
    agent._sync_send_input(AudioInput(audio=frame))
    agent._sync_commit()
    calls = agent._handle.calls
    assert calls[0] == ("start",) and calls[1] == ("remind",)
    assert calls[2] == ("audio", 2048) and calls[3] == ("audio", 2048)
    assert calls[4:] == [("stop",), ("finalize",)]
    assert agent._turn_awaiting is True
    # the next utterance opens a fresh proposal
    agent._sync_send_input(AudioInput(audio=frame))
    assert agent._handle.calls[6] == ("start",)


def test_commit_without_audio_ends_the_turn_at_once(monkeypatch):
    agent = make_agent(monkeypatch, live=False)
    agent._sync_commit()
    events = drain(agent)
    assert len(events) == 1 and isinstance(events[0], TurnDoneEvent)
    assert not events[0].execution_completed
    assert agent._handle.calls == []


def test_empty_transcript_ends_a_committed_turn(monkeypatch):
    agent = make_agent(monkeypatch, live=False)
    agent._sync_send_input(AudioInput(audio=np.zeros(8, dtype=np.float32)))
    agent._sync_commit()
    agent._ev_stt_turn_finalized(had_text=False)
    events = drain(agent)
    assert isinstance(events[-1], TurnDoneEvent) and not events[-1].execution_completed
    # ...but a transcribed turn is left to the LLM
    agent._sync_send_input(AudioInput(audio=np.zeros(8, dtype=np.float32)))
    agent._sync_commit()
    agent._ev_stt_turn_finalized(had_text=True)
    assert drain(agent) == []


def test_interruption_in_turn_mode_emits_no_turn_done(monkeypatch):
    agent = make_agent(monkeypatch, live=False)
    agent._ev_user_turn_started()
    agent._ev_response_started()
    agent._ev_text("half a rep")
    gen_before = agent._gen
    agent._ev_interruption()  # the next utterance's first frame cut the reply
    events = drain(agent)
    assert not any(isinstance(e, TurnDoneEvent) for e in events)
    assert agent._gen == gen_before + 1
    assert not agent._response_open


# --- tool calls -------------------------------------------------------------------------


def test_tool_call_defers_turn_done_to_the_follow_up_response(monkeypatch):
    agent = make_agent(monkeypatch, live=False)
    agent._ev_user_turn_started()
    agent._ev_response_started()
    agent._ev_calls_started(["c1"])
    agent._ev_response_ended()  # tool-call-only response: no TurnDone yet
    assert not any(isinstance(e, TurnDoneEvent) for e in drain(agent))
    agent._ev_call_result("c1", run_llm=True)
    assert drain(agent) == []
    reply(agent, "done")
    events = drain(agent)
    assert isinstance(events[-1], TurnDoneEvent) and events[-1].execution_completed


def test_tool_result_without_follow_up_ends_the_turn(monkeypatch):
    agent = make_agent(monkeypatch, live=False)
    agent._ev_user_turn_started()
    agent._ev_response_started()
    agent._ev_calls_started(["c1", "c2"])
    agent._ev_response_ended()
    agent._ev_call_result("c1", run_llm=False)
    assert drain(agent) == [] or not any(isinstance(e, TurnDoneEvent) for e in drain(agent))
    agent._ev_call_result("c2", run_llm=False)
    events = drain(agent)
    assert isinstance(events[-1], TurnDoneEvent) and events[-1].execution_completed


def test_end_turn_fences_the_rest_of_the_user_turn(monkeypatch):
    agent = make_agent(monkeypatch, live=False)
    agent._ev_user_turn_started()
    agent._ev_response_started()
    agent._ev_calls_started(["c1"])
    agent._ev_response_ended()
    # the orchestrator delegated and stopped reading
    agent.end_turn()
    # ...and the model still answers the tool result
    agent._ev_call_result("c1", run_llm=True)
    reply(agent, "Sure, delegating that now.")
    # next user turn
    agent._ev_user_turn_started()
    reply(agent, "Paris.")
    yielded = [o for o in agent.receive(stop_on_done=True)]
    texts = [o.text for o in yielded if isinstance(o, TextOutput)]
    assert texts == ["Paris."], texts


def test_delegate_result_never_runs_the_llm_but_emotion_does(monkeypatch):
    agent = make_agent(monkeypatch, live=False)

    async def run():
        agent._loop = asyncio.get_running_loop()
        agent._ev_user_turn_started()
        f1 = agent._begin_tool_call(DELEGATE, '{"message": "play music"}', "c1")
        f2 = agent._begin_tool_call(EMOTION, '{"emotion": "happy"}', "c2")
        agent._resolve_tool_call("c1", '{"result": "delegated"}', True)
        agent._resolve_tool_call("c2", '{"result": "expressed"}', True)
        return await asyncio.wait_for(asyncio.gather(f1, f2), 2)

    (out1, run1), (out2, run2) = asyncio.run(run())
    assert run1 is False and out1 == '{"result": "delegated"}'
    assert run2 is True
    calls = [e.output for e in drain(agent) if isinstance(e, OutputEvent) and isinstance(e.output, FunctionCallOutput)]
    assert [c.name for c in calls] == [DELEGATE, EMOTION]
    assert calls[0].user_turn_id == agent._user_turn_id


def test_tool_call_carries_the_provider_transcript(monkeypatch):
    agent = make_agent(monkeypatch, live=False)

    async def run():
        agent._loop = asyncio.get_running_loop()
        agent._ev_user_turn_started()
        agent._ev_transcript("please play", False)
        agent._ev_transcript("Please play some music", True)
        f = agent._begin_tool_call(DELEGATE, "{}", "c1")
        agent._resolve_tool_call("c1", "{}", True)
        await asyncio.wait_for(f, 2)

    asyncio.run(run())
    call = next(e.output for e in drain(agent) if isinstance(e, OutputEvent) and isinstance(e.output, FunctionCallOutput))
    assert call.user_transcript == "Please play some music"


def test_unanswered_tool_call_times_out_with_an_error(monkeypatch):
    agent = make_agent(monkeypatch, live=False)
    agent._config = agent._config.model_copy(update={"tool_result_timeout_s": 0.05})

    async def run():
        agent._loop = asyncio.get_running_loop()
        f = agent._begin_tool_call(DELEGATE, "{}", "c1")
        return await asyncio.wait_for(f, 2)

    out, run_llm = asyncio.run(run())
    assert "error" in out and run_llm is False


# --- live mode ------------------------------------------------------------------------------


def test_live_mode_reports_user_speech_and_interruptions(monkeypatch):
    agent = make_agent(monkeypatch, live=True)
    agent._ev_user_turn_started()
    agent._ev_transcript("what is", False)
    agent._ev_transcript("What is the capital of France?", True)
    agent._ev_user_turn_stopped()
    agent._ev_response_started()
    agent._ev_text("Par")
    gen = agent._gen
    agent._ev_interruption()  # the user spoke over the reply
    events = drain(agent)
    speech = [e.output for e in events if isinstance(e, OutputEvent) and isinstance(e.output, UserSpeechOutput)]
    assert speech[0].method == "server_vad" and speech[0].endpoint_at is None
    # interims are not surfaced; the final is
    assert speech[1].method == "provider_transcript" and speech[1].transcript == "What is the capital of France?"
    assert speech[1].transcript_finished
    assert speech[2].method == "server_vad" and speech[2].endpoint_at is not None
    assert {s.turn_id for s in speech} == {agent._user_turn_id}
    interrupts = [e.output for e in events if isinstance(e, OutputEvent) and isinstance(e.output, InterruptedOutput)]
    assert [i.reason for i in interrupts] == ["output_reset", "server_interrupt"]
    assert interrupts[1].at is not None and events[-1].__class__ is TurnDoneEvent
    assert not events[-1].execution_completed
    assert agent._gen == gen + 1


def test_live_transcripts_are_emitted_as_final_deltas_only(monkeypatch):
    agent = make_agent(monkeypatch, live=True)
    agent._ev_user_turn_started()
    agent._ev_transcript("what is", False)            # interims never reach the pump
    agent._ev_transcript("What is the capital", False)
    agent._ev_transcript("What is the capital of France?", True)   # Flux EndOfTurn
    agent._ev_transcript("What is the capital of France? And Spain?", True)  # a second final
    agent._ev_transcript("What is the capital", True)  # shorter rewrite: nothing new
    chunks = [
        (e.output.transcript, e.output.transcript_finished)
        for e in drain(agent)
        if isinstance(e, OutputEvent) and isinstance(e.output, UserSpeechOutput) and e.output.method == "provider_transcript"
    ]
    assert chunks == [
        ("What is the capital of France?", True), (" And Spain?", True), ("", True),
    ], chunks
    # a rewritten final only contributes the words after the common prefix
    agent._ev_transcript("What is the Capital of France? And Spain? Thanks", True)
    chunk = drain(agent)[-1].output.transcript
    assert chunk == " Thanks"
    assert agent._user_transcript.startswith("What is the capital of France?")


def test_live_mode_ignores_commits(monkeypatch):
    agent = make_agent(monkeypatch, live=True)
    agent._sync_send_input(AudioInput(audio=np.zeros(8, dtype=np.float32)))
    agent._sync_commit()
    assert agent._handle.calls == [("audio", 16)]
    assert drain(agent) == []


# --- health ---------------------------------------------------------------------------------


def test_fail_fast_only_unblocks_a_waiting_turn(monkeypatch):
    agent = make_agent(monkeypatch, live=False)
    agent._fail_fast_turn("x")
    assert drain(agent) == []
    agent._sync_send_input(AudioInput(audio=np.zeros(8, dtype=np.float32)))
    agent._sync_commit()
    agent._drop_pipeline("pipeline run ended")
    events = drain(agent)
    assert isinstance(events[-1], TurnDoneEvent) and not agent.available


def test_orchestrator_factory_builds_pipecat_v1_with_the_voice_stt_provider(monkeypatch):
    from hal.realtime.orchestrator import RealtimeOrchestrator

    monkeypatch.setattr(config, "LIVE_MODE", False)
    orc = object.__new__(RealtimeOrchestrator)
    orc._tools = []
    orc._stt_provider = sentinel = object()
    agent = orc._make_agent("pipecat_v1", "be brief")
    assert isinstance(agent, PipecatV1Agent)
    assert agent._stt_provider is sentinel
    assert agent._config.instructions == "be brief"


# --- STT adapter (needs pipecat) ---------------------------------------------------------------


def test_stt_adapter_reads_the_turn_flag_after_close():
    pytest.importorskip("pipecat")
    from hal.realtime.voice_agent import pipecat_stt

    class Session:
        def __init__(self, cb_holder):
            self.h = cb_holder
            self.closed = threading.Event()
            self.audio = b""

        def start(self, cb):
            self.h["cb"] = cb
            return True

        def send_audio(self, data):
            self.audio += data

        def close(self):
            # the server flushes the final only on CloseStream
            self.h["cb"]("hello there", True)
            self.closed.set()

        def is_closed(self):
            return self.closed.is_set()

    holder: dict = {}
    provider = NS(create_session=lambda: Session(holder), name="fake", available=True)
    finalized: list = []
    transcripts: list = []
    stt = pipecat_stt.HALSTTService(
        provider, per_turn=True, on_transcript=lambda t, f: transcripts.append((t, f)),
        on_turn_finalized=finalized.append,
    )
    stt._ops.put(b"\x00" * 64)
    stt._ops.put(pipecat_stt._OP_FINALIZE)
    stt._ops.put(pipecat_stt._OP_FINALIZE)  # a commit with no audio in between
    stt._ops.put(pipecat_stt._OP_STOP)
    stt._sender_loop()
    assert transcripts == [("hello there", True)]
    assert finalized == [True, False]
