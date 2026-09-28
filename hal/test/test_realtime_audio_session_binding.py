"""A LIVE OFF capture must never split audio and commit across sessions."""

import asyncio
import queue
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from hal.realtime.models import AudioInput, TextOutput, TurnDoneEvent
from hal.realtime.orchestrator import AudioTurnSessionChanged, RealtimeOrchestrator
from hal.realtime.voice_agent.base import VoiceAgentBase
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


class _Session:
    def __init__(self, after_send=None):
        self.inputs = []
        self.after_send = after_send

    async def send_realtime_input(self, **kwargs):
        self.inputs.append(kwargs)
        if self.after_send:
            self.after_send()


def _agent(session):
    agent = object.__new__(GeminiLiveAgent)
    VoiceAgentBase.__init__(agent)
    agent._session = session
    agent._connected.set()
    agent._config = SimpleNamespace(sample_rate=16000)
    agent._vad_disabled = True
    agent._activity_started = False
    agent._pending_tool_calls = set()
    agent._gated_audio_frames = 0
    agent._turn_done = threading.Event()
    agent._turn_done.set()
    return agent


def _orchestrator(agent):
    rt = object.__new__(RealtimeOrchestrator)
    rt._agent = agent
    rt._started = threading.Event()
    rt._started.set()
    rt._lifecycle_lock = threading.Lock()
    rt._rebuild_lock = threading.Lock()
    rt._idle_parked = False
    rt._skip_post_idle_recycle = True
    return rt


def test_bound_capture_rejects_agent_swap_before_tail_or_commit():
    old = _agent(_Session())
    rt = _orchestrator(old)
    turn = rt.bind_audio_turn()
    rt.append_audio(np.ones(160, dtype=np.float32), turn=turn)
    replacement = _agent(_Session())
    rt._agent = replacement

    for operation in (
        lambda: rt.append_audio(np.zeros(160, dtype=np.float32), turn=turn),
        lambda: rt.flush_output(turn=turn),
        lambda: rt.commit_audio(turn=turn),
        lambda: list(rt.stream_output(turn=turn)),
    ):
        with pytest.raises(AudioTurnSessionChanged):
            operation()
    assert replacement._send_queue.empty()
    assert old._send_queue.qsize() == 1


def test_bound_capture_rejects_stop_and_stale_output():
    agent = _agent(_Session())
    rt = _orchestrator(agent)
    turn = rt.bind_audio_turn()

    def receive(**kwargs):
        rt._started.clear()
        yield TextOutput(text="stale reply")

    agent.receive = receive
    with pytest.raises(AudioTurnSessionChanged):
        list(rt.stream_output(turn=turn))
    with pytest.raises(AudioTurnSessionChanged):
        rt.commit_audio(turn=turn)


def test_socket_swap_during_activity_start_never_sends_tail_to_new_socket():
    replacement = _Session()
    agent = _agent(_Session())
    original = agent._session
    original.after_send = lambda: setattr(agent, "_session", replacement)

    with pytest.raises(AudioTurnSessionChanged):
        asyncio.run(agent._async_send_input(
            AudioInput(audio=np.ones(160, dtype=np.float32)), session=original,
        ))
    assert len(original.inputs) == 1
    assert "activity_start" in original.inputs[0]
    assert replacement.inputs == []
    assert not agent._activity_started


def _run_queued_events(agent):
    class _StopAfterDrain(queue.Queue):
        def get(self, *args, **kwargs):
            event = super().get(*args, **kwargs)
            if self.empty():
                agent._stop_event.set()
            return event

    pending = _StopAfterDrain()
    while not agent._send_queue.empty():
        pending.put(agent._send_queue.get_nowait())
    agent._send_queue = pending
    agent._loop = object()
    agent._queue_poll_s = 0.01
    agent._max_retries = 2
    agent._send_timeout_s = 1
    agent._ensure_connected = lambda: None
    agent._submit_and_wait = lambda coro, **kwargs: asyncio.run(coro)
    agent._send_loop()


def test_queued_audio_and_commit_are_dropped_after_provider_reconnect():
    original = _Session()
    agent = _agent(original)
    agent.append_audio(np.ones(160, dtype=np.float32), session=original)
    agent.commit_audio(session=original)
    replacement = _Session()
    agent._session = replacement

    _run_queued_events(agent)

    assert original.inputs == []
    assert replacement.inputs == []
    assert isinstance(agent._recv_queue.get_nowait(), TurnDoneEvent)


def test_send_retry_does_not_move_failed_frame_or_commit_to_new_socket():
    def fail():
        raise RuntimeError("socket closed during audio send")

    original = _Session(after_send=fail)
    replacement = _Session()
    agent = _agent(original)
    agent._vad_disabled = False
    agent._reconnect = lambda: setattr(agent, "_session", replacement)
    agent.append_audio(np.ones(160, dtype=np.float32), session=original)
    agent.commit_audio(session=original)

    _run_queued_events(agent)

    assert len(original.inputs) == 1
    assert replacement.inputs == []
    assert isinstance(agent._recv_queue.get_nowait(), TurnDoneEvent)


def test_full_replay_to_new_agent_preserves_order_and_single_commit():
    old = _agent(_Session())
    rt = _orchestrator(old)
    first = rt.bind_audio_turn()
    frames = [np.full(160, value, dtype=np.float32) for value in (0.1, 0.2, 0.3)]
    rt.append_audio(frames[0], turn=first)
    replacement = _agent(_Session())
    rt._agent = replacement
    replay = rt.bind_audio_turn()
    for frame in frames:
        rt.append_audio(frame, turn=replay)
    rt.flush_output(turn=replay)
    rt.commit_audio(turn=replay)

    _run_queued_events(replacement)

    sent = replacement._session.inputs
    assert len(sent) == 5
    assert "activity_start" in sent[0]
    assert "activity_end" in sent[-1]
    actual = [np.frombuffer(event["audio"].data, dtype=np.int16).mean() for event in sent[1:-1]]
    assert actual[0] < actual[1] < actual[2]
    assert old._send_queue.qsize() == 1


def test_unbound_live_audio_continues_on_current_socket():
    original = _Session()
    agent = _agent(original)
    agent.append_audio(np.ones(160, dtype=np.float32))
    agent.commit_audio()
    replacement = _Session()
    agent._session = replacement

    _run_queued_events(agent)

    assert original.inputs == []
    assert len(replacement.inputs) == 3
    assert "activity_start" in replacement.inputs[0]
    assert "audio" in replacement.inputs[1]
    assert "activity_end" in replacement.inputs[2]


def test_live_stream_end_is_ordered_between_audio_without_committing():
    agent = _agent(_Session())
    agent._vad_disabled = False
    rt = _orchestrator(agent)
    rt.append_audio(np.full(160, 0.1, dtype=np.float32))
    assert rt.end_live_audio()
    rt.append_audio(np.full(160, 0.2, dtype=np.float32))
    _run_queued_events(agent)
    sent = agent._session.inputs
    assert [set(message) for message in sent] == [{'audio'}, {'audio_stream_end'}, {'audio'}]
    assert sent[1]['audio_stream_end'] is True
    assert agent._turn_done.is_set()
    assert agent._committed_at == 0.0


def test_live_stream_end_never_moves_to_replacement_transport():
    original = _Session()
    agent = _agent(original)
    agent._vad_disabled = False
    assert _orchestrator(agent).end_live_audio()
    replacement = _Session()
    agent._session = replacement
    _run_queued_events(agent)
    assert original.inputs == replacement.inputs == []
    assert agent._recv_queue.empty()


@pytest.mark.parametrize('reason', ['manual_vad', 'pending_tool', 'quarantine'])
@pytest.mark.parametrize('after_enqueue', [False, True])
def test_live_stream_end_skips_unsafe_session(reason, after_enqueue):
    agent = _agent(_Session())
    agent._vad_disabled = False
    rt = _orchestrator(agent)
    if after_enqueue:
        assert rt.end_live_audio()
    if reason == 'manual_vad':
        agent._vad_disabled = True
    elif reason == 'pending_tool':
        agent._pending_tool_calls.add('pending')
    else:
        agent._requires_fresh_session = True
    if not after_enqueue:
        assert not rt.end_live_audio()
        assert agent._send_queue.empty()
    else:
        _run_queued_events(agent)
    assert agent._session.inputs == []


def test_live_stream_end_keeps_original_agent_queue_when_orchestrator_rebuilds():
    old = _agent(_Session())
    old._vad_disabled = False
    rt = _orchestrator(old)
    assert rt.end_live_audio()
    replacement = _agent(_Session())
    replacement._vad_disabled = False
    rt._agent = replacement
    _run_queued_events(old)
    assert old._session.inputs == [{'audio_stream_end': True}]
    assert replacement._send_queue.empty()
    assert replacement._session.inputs == []


def test_other_providers_do_not_send_live_stream_end():
    agent = _agent(_Session())
    assert VoiceAgentBase.end_audio_stream(agent, session=agent._session) is False
    assert agent._send_queue.empty()
