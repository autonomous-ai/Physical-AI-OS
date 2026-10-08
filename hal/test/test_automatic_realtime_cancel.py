"""A playback stop releases automic without replaying a cancelled reply."""

import threading
import time
from unittest.mock import Mock

import numpy as np
import pytest

from hal.drivers.voice.voice_service import VoiceService
from hal.drivers.voice._internal import realtime_turn as module
from hal.realtime.models import AudioOutput, TextOutput
from hal.realtime.orchestrator import RealtimeOrchestrator
from hal.test.test_live_receive_cancel import Agent, ObservedQueue
from hal.test.test_manual_realtime_cancel import turn, assert_cancelled
from hal.test.test_turn_endpoint_capture import capture


def voice_service():
    service = object.__new__(VoiceService)
    service._automatic_reply_lock = threading.Lock()
    service._automatic_reply_stop = None
    service._automatic_reply_cancelled_at = None
    return service


def test_stop_route_signals_receive_before_stopping_speaker(monkeypatch):
    from hal.routes import voice

    service = voice_service()
    stop = threading.Event()
    service._automatic_reply_stop = stop
    tts = Mock()
    tts.stop.side_effect = lambda: stop.is_set() or pytest.fail("speaker stopped before receive cancellation")
    monkeypatch.setattr(voice.state, "voice_service", service)
    monkeypatch.setattr(voice.state, "tts_service", tts)
    assert voice.stop_tts() == {"status": "ok"}
    assert stop.is_set()
    tts.stop.assert_called_once()
    assert service._automatic_reply_cancelled_at is not None


def test_stop_without_reply_does_not_cancel_next_turn():
    service = voice_service()
    assert not service.cancel_automatic_reply()
    assert service._automatic_reply_cancelled_at is None


def test_button_releases_silent_provider_receive_under_half_second(turn):
    realtime, _, _, _, _ = turn
    service = voice_service()
    agent = Agent()
    agent._recv_queue = ObservedQueue()
    realtime.stream_output.side_effect = lambda **kwargs: agent.receive(stop_event=kwargs["stop_event"])
    stop = threading.Event()
    result = []

    def run():
        result.append(service._run_automatic_realtime_turn(
            stop, realtime, Mock(_provider="test"), lambda value: value,
            "Please explain the result.", [np.zeros(320, dtype=np.float32)], 2.0,
            interaction_id="auto-1", wait_filler=Mock(), harness_followup=False,
        ))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert agent._recv_queue.waiting.wait(2)
    started = time.monotonic()
    assert service.cancel_automatic_reply()
    worker.join(1)
    assert not worker.is_alive()
    assert time.monotonic() - started < 0.5
    assert_cancelled(result[0], realtime)
    realtime.recover_cancelled_turn.assert_called_once()
    realtime.recover_session.assert_not_called()


@pytest.mark.parametrize("native", [False, True])
def test_cancelled_automatic_reply_drops_late_text_and_audio(turn, monkeypatch, native):
    realtime, tts, stop, filler, run = turn
    monkeypatch.setattr(module.hal_config, "REALTIME_NATIVE_AUDIO", native)
    if native:
        tts.native_play_begin.return_value = True
        tts.native_play_frame.side_effect = lambda frame: (stop.set() or True)
        output = AudioOutput(audio=np.zeros(100, dtype=np.float32), transcript="Hello")
    else:
        tts.speak.side_effect = lambda *args, **kwargs: (stop.set() or True)
        output = TextOutput(text="This is the first complete sentence.")

    def outputs(**kwargs):
        yield output
        pytest.fail("consumed late output after button stop")

    realtime.stream_output.side_effect = outputs
    assert_cancelled(run(background_cancel_recovery=True), realtime)
    realtime.recover_cancelled_turn.assert_called_once()
    realtime.recover_session.assert_not_called()
    tts.speak_queue.assert_not_called()
    filler.cancel.assert_called()


@pytest.mark.parametrize("failed", [False, True])
def test_recovery_retires_old_agent_before_slow_connect_finishes(failed):
    orchestrator = object.__new__(RealtimeOrchestrator)
    old = Mock(available=True)
    replacement = Mock(available=True)
    entered = threading.Event()
    release = threading.Event()

    def connect():
        entered.set()
        assert release.wait(2)
        if failed:
            raise RuntimeError("provider unavailable")

    replacement.connect.side_effect = connect
    orchestrator._agent = old
    orchestrator._context = Mock()
    orchestrator._make_agent = Mock(return_value=replacement)
    orchestrator._lifecycle_lock = threading.Lock()
    orchestrator._rebuild_lock = threading.Lock()
    orchestrator._rebuild_done = threading.Event()
    orchestrator._started = threading.Event()
    orchestrator._started.set()
    orchestrator._disconnect_in_background = lambda agent, reason: agent.disconnect()
    try:
        started = time.monotonic()
        assert orchestrator.recover_cancelled_turn()
        assert time.monotonic() - started < 0.5
        assert entered.wait(1)
        assert orchestrator._agent is None
        assert not orchestrator.available
        old.disconnect.assert_called_once()
    finally:
        release.set()
        assert orchestrator._rebuild_done.wait(2)
    assert orchestrator._agent is (None if failed else replacement)
    assert orchestrator.available is (not failed)


def test_existing_rebuild_cannot_keep_cancelled_agent_available():
    orchestrator = object.__new__(RealtimeOrchestrator)
    old = Mock()
    orchestrator._agent = old
    orchestrator._lifecycle_lock = threading.Lock()
    orchestrator._rebuild_lock = threading.Lock()
    orchestrator._disconnect_in_background = lambda agent, reason: agent.disconnect()
    with orchestrator._rebuild_lock:
        assert not orchestrator.recover_cancelled_turn()
    assert orchestrator._agent is None
    old.disconnect.assert_called_once()


def test_cancel_during_early_reply_does_not_wait_for_stt_close(monkeypatch):
    closing = threading.Event()
    release = threading.Event()
    state = {}

    def drain(stt, service):
        state["service"] = service
        closing.set()
        assert release.wait(2), "cancelled capture waited on slow STT drain"

    def reply(*args, **kwargs):
        assert closing.wait(1)
        service = state["service"]
        assert VoiceService.cancel_automatic_reply(service)
        assert kwargs["stop_event"].is_set()
        return module.RealtimeTurnResult(route=module.ROUTE_CANCELLED)

    try:
        with capture(
            monkeypatch, [(1, True, "Please explain the result"), (4, False, None)],
            realtime=True, on_drain=drain, on_realtime=reply,
        ) as result:
            assert not release.is_set()
            assert not result.service._stt_drain_future.done()
            assert result.service._automatic_reply_stop is None
            result.dispatch.assert_not_called()
            result.service._realtime.save_turn.assert_not_called()
    finally:
        release.set()
        if "service" in state:
            state["service"]._stt_drain_future.result(timeout=2)
            state["service"]._stt_drain_worker.shutdown(wait=True)


def test_normal_reply_cancel_does_not_fall_back_to_main(monkeypatch):
    state = {}

    def drain(stt, service):
        state["service"] = service

    def reply(*args, **kwargs):
        assert "save_history" not in kwargs
        assert VoiceService.cancel_automatic_reply(state["service"])
        return module.RealtimeTurnResult(route=module.ROUTE_CANCELLED)

    with capture(
        monkeypatch, [(1, True, "Please explain the result"), (4, False, None)],
        realtime=True, transcripts_final=False, on_drain=drain, on_realtime=reply,
    ) as result:
        result.realtime.assert_called_once()
        result.dispatch.assert_not_called()
        result.service._realtime.save_turn.assert_not_called()
        assert result.service._automatic_reply_stop is None
        if result.service._stt_drain_worker is not None:
            result.service._stt_drain_worker.shutdown(wait=True)


def test_stt_close_timeout_is_propagated_instead_of_polling_completed_future(monkeypatch):
    state = {}

    def drain(stt, service):
        state["service"] = service
        raise TimeoutError("provider close timed out")

    try:
        with pytest.raises(TimeoutError, match="provider close timed out"):
            with capture(
                monkeypatch, [(1, True, "Please explain the result"), (4, False, None)],
                realtime=True, on_drain=drain,
            ):
                pytest.fail("provider failure was swallowed")
    finally:
        if "service" in state:
            state["service"]._stt_drain_worker.shutdown(wait=True)
