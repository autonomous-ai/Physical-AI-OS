"""AnnounceInput on the providers that support it (Gemini, pipecat_v1) and the
orchestrator's announce / preemption contract."""

import asyncio
import queue
import threading
from types import SimpleNamespace

import pytest

from hal import config
from hal.realtime import orchestrator as orchestrator_module
from hal.realtime.config import PipecatV1Config
from hal.realtime.models import AnnounceInput, InputEvent, TextOutput, TurnDoneEvent
from hal.realtime.orchestrator import RealtimeOrchestrator
from hal.realtime.voice_agent.base import VoiceAgentBase
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent
from hal.realtime.voice_agent.pipecat_v1 import PipecatV1Agent


# --- Gemini ---------------------------------------------------------------------------------


class _Session:
    def __init__(self) -> None:
        self.client_contents: list[dict] = []

    async def send_client_content(self, **kwargs) -> None:
        self.client_contents.append(kwargs)


def _gemini(session: _Session) -> GeminiLiveAgent:
    agent = object.__new__(GeminiLiveAgent)
    agent._session = session
    agent._pending_tool_calls = set()
    agent._activity_started = False
    agent._gated_audio_frames = 0
    agent._turn_done = threading.Event()
    agent._turn_done.set()
    agent._recv_queue = queue.Queue()
    return agent


def test_gemini_announce_is_a_complete_user_text_turn():
    session = _Session()
    agent = _gemini(session)
    asyncio.run(agent._async_send_input(AnnounceInput(text="update")))
    assert len(session.client_contents) == 1
    sent = session.client_contents[0]
    assert sent["turn_complete"] is True
    assert sent["turns"].role == "user" and sent["turns"].parts[0].text == "update"
    assert not agent._turn_done.is_set()  # the next commit waits for this response


@pytest.mark.parametrize("busy", ["tool", "activity"])
def test_gemini_announce_ends_at_once_when_the_session_is_busy(busy):
    session = _Session()
    agent = _gemini(session)
    if busy == "tool":
        agent._pending_tool_calls.add("call-1")
    else:
        agent._activity_started = True
    asyncio.run(agent._async_send_input(AnnounceInput(text="update")))
    assert not session.client_contents
    assert isinstance(agent._recv_queue.get_nowait(), TurnDoneEvent)
    assert agent._turn_done.is_set()


def test_gemini_supports_announce_only_on_text_capable_turn_based_models(monkeypatch):
    agent = object.__new__(GeminiLiveAgent)
    monkeypatch.setattr(config, "LIVE_MODE", False)
    monkeypatch.setattr(config, "REALTIME_GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    assert agent.supports_announce
    monkeypatch.setattr(config, "REALTIME_GEMINI_MODEL", "gemini-2.5-flash-native-audio-preview")
    assert not agent.supports_announce
    monkeypatch.setattr(config, "REALTIME_GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    monkeypatch.setattr(config, "LIVE_MODE", True)
    assert not agent.supports_announce


# --- pipecat_v1 -------------------------------------------------------------------------------


class _Handle:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.alive = True

    def run_announcement(self, text):
        self.calls.append(("announce", text))


def _pipecat(monkeypatch, *, live=False) -> PipecatV1Agent:
    monkeypatch.setattr(config, "LIVE_MODE", live)
    agent = PipecatV1Agent(PipecatV1Config(instructions="x"), tools=[])
    agent._handle = _Handle()
    agent._connected.set()
    return agent


def test_pipecat_announce_opens_a_new_generation_past_a_fenced_turn(monkeypatch):
    agent = _pipecat(monkeypatch)
    agent.end_turn()  # previous turn delegated: its generation is fenced
    before = agent._gen
    agent._sync_send_input(AnnounceInput(text="update"))
    assert agent._handle.calls == [("announce", "update")]
    assert agent._gen == before + 1 and agent._turn_awaiting
    agent._ev_response_started()
    agent._ev_text("Done.")
    agent._ev_response_ended()
    outputs = list(agent.receive())
    assert [o.text for o in outputs if isinstance(o, TextOutput)] == ["Done."]


def test_pipecat_announce_refused_while_a_committed_turn_awaits_its_reply(monkeypatch):
    agent = _pipecat(monkeypatch)
    agent._turn_awaiting = True
    agent._sync_send_input(AnnounceInput(text="update"))
    assert agent._handle.calls == []
    assert isinstance(agent._recv_queue.get_nowait(), TurnDoneEvent)


def test_pipecat_announce_not_blocked_by_an_abandoned_noise_capture(monkeypatch):
    agent = _pipecat(monkeypatch)
    agent._manual_turn_open = True  # a noise-dropped capture never committed
    agent._sync_send_input(AnnounceInput(text="update"))
    assert agent._handle.calls == [("announce", "update")]


def test_pipecat_supports_announce_only_turn_based(monkeypatch):
    assert _pipecat(monkeypatch).supports_announce
    assert not _pipecat(monkeypatch, live=True).supports_announce


# --- providers without support ------------------------------------------------------------------


def test_base_provider_refuses_announce():
    class Plain(VoiceAgentBase):
        sample_rate = 16000

        def _do_connect(self): ...
        def _do_disconnect(self): ...
        def _send_loop(self): ...
        def _recv_loop(self): ...

    agent = Plain()
    agent._connected.set()
    assert not agent.supports_announce and not agent.announce("x")
    assert agent._send_queue.empty()


# --- orchestrator ---------------------------------------------------------------------------------


class _ScriptedAgent(VoiceAgentBase):
    """Announce-capable agent whose reply is scripted onto the recv queue."""

    sample_rate = 16000

    def __init__(self, reply) -> None:
        super().__init__()
        self._connected.set()
        self.reply = reply
        self.ended = 0

    @property
    def supports_announce(self) -> bool:
        return True

    def announce(self, text):
        if not super().announce(text):
            return False
        for item in self.reply:
            self._recv_queue.put(item)
        return True

    def end_turn(self):
        self.ended += 1

    def _do_connect(self): ...
    def _do_disconnect(self): ...
    def _send_loop(self): ...
    def _recv_loop(self): ...


def _orchestrator(agent) -> RealtimeOrchestrator:
    orch = object.__new__(RealtimeOrchestrator)
    orch._agent = agent
    orch._started = threading.Event()
    orch._started.set()
    orch._rebuild_lock = threading.Lock()
    orch._idle_parked = False
    orch._park_resume_failed = False
    orch._vision_enabled = False
    orch._expression_enabled = False
    orch._skip_post_idle_recycle = False
    orch._consecutive_silent = 0
    orch._idle_reset_pending = False
    orch._last_activity_monotonic = 0.0
    orch._last_turn_monotonic = 0.0
    orch._turn_in_flight = False
    orch._turn_started_monotonic = 0.0
    orch._turns_since_recycle = 0
    orch._force_rebuild = lambda: None
    orch._announce_stop = None
    orch._announce_idle = threading.Event()
    orch._announce_idle.set()
    return orch


def _event(output):
    from hal.realtime.models import OutputEvent

    return OutputEvent(gen=0, output=output)


def test_orchestrator_announce_streams_the_reply_and_ends_the_turn(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_PROVIDER", "pipecat_v1")
    agent = _ScriptedAgent([_event(TextOutput(text="Done.")), TurnDoneEvent()])
    orch = _orchestrator(agent)
    assert orch.prepare_announcement(allow_resume=True)
    outputs = list(orch.announce("update", stop_event=threading.Event()))
    assert [o.text for o in outputs if isinstance(o, TextOutput)] == ["Done."]
    queued = agent._send_queue.get_nowait()
    assert isinstance(queued, InputEvent) and queued.input == AnnounceInput(text="update")
    assert not orch.turn_in_flight and orch._announce_idle.is_set()


def test_orchestrator_refuses_announcements_during_a_user_turn_or_when_parked(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_PROVIDER", "pipecat_v1")
    orch = _orchestrator(_ScriptedAgent([]))
    orch._turn_in_flight = True
    orch._turn_started_monotonic = orchestrator_module.time.monotonic()
    assert not orch.prepare_announcement(allow_resume=True)
    orch._turn_in_flight = False
    orch._idle_parked = True
    assert not orch.prepare_announcement(allow_resume=False)


def test_user_capture_preempts_and_drains_the_announcement(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_PROVIDER", "pipecat_v1")
    monkeypatch.setattr(orchestrator_module, "ANNOUNCE_DRAIN_S", 0.5)
    agent = _ScriptedAgent([_event(TextOutput(text="First.")), _event(TextOutput(text="Second."))])
    orch = _orchestrator(agent)
    stop = threading.Event()
    stream = orch.announce("update", stop_event=stop)
    first = next(stream)
    assert first.text == "First."
    assert not orch._announce_idle.is_set()
    orch.prepare_turn()  # the user starts talking
    assert stop.is_set()
    assert list(stream) == []  # nothing more reaches the speaker
    assert orch._announce_idle.is_set() and agent.ended == 1
    assert agent._recv_queue.empty()  # the rest of the reply was drained


def test_a_finished_capture_reopens_the_announcement_gate(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_PROVIDER", "pipecat_v1")
    orch = _orchestrator(_ScriptedAgent([]))
    orch.prepare_turn()  # a capture that is later dropped as noise
    assert orch.turn_in_flight and not orch.prepare_announcement(allow_resume=True)
    orch.finish_capture()
    assert not orch.turn_in_flight and orch.prepare_announcement(allow_resume=True)


def test_announcement_waits_for_an_in_flight_rebuild(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_PROVIDER", "pipecat_v1")
    orch = _orchestrator(_ScriptedAgent([]))
    orch._rebuild_done = threading.Event()
    orch._rebuild_lock.acquire()  # a noise-drop rebuild is connecting

    def finish_rebuild():
        orch._rebuild_lock.release()
        orch._rebuild_done.set()

    threading.Timer(0.2, finish_rebuild).start()
    assert orch.prepare_announcement(allow_resume=True)


def test_abandoned_activity_gets_a_fresh_session_before_announcing(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_PROVIDER", "pipecat_v1")
    stale = _ScriptedAgent([])
    stale._activity_started = True  # a capture ended without a commit
    fresh = _ScriptedAgent([])
    orch = _orchestrator(stale)
    reasons = []

    def rebuild(reason, **kwargs):
        reasons.append(reason)
        orch._agent = fresh
        return True

    orch._rebuild_now = rebuild
    assert orch.prepare_announcement(allow_resume=True)
    assert reasons == ["announce-abandoned-activity"] and orch._agent is fresh
