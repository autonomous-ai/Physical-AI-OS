"""Device finalization keeps realtime ordered while the next capture owns input."""

import threading
from unittest.mock import Mock

import numpy as np
import pytest

from hal.drivers.voice._internal import device_realtime as module
from hal.drivers.voice._internal.realtime_turn import RealtimeTurnResult
from hal.drivers.voice.tts.device_input_gate import DeviceInputGate
from hal.test.test_tts_device_input_gate import service as tts_service


@pytest.fixture
def executor(monkeypatch):
    monkeypatch.setattr(module.config, "REALTIME_ENABLED", True)
    realtime = Mock(sample_rate=24000)
    realtime.wait_until_available.return_value = True
    cancelled = threading.Event()
    valid = Mock(return_value=True)
    result = RealtimeTurnResult(handled=True)
    runner = Mock(return_value=result)
    monkeypatch.setattr(module, "run_realtime_turn", runner)
    tts = Mock()
    invoke = module.DeviceRealtimeTurn(realtime=lambda: realtime, tts=lambda: tts,
                                       strip_markers=lambda text: text)
    args = dict(audio=[np.ones(320, dtype=np.int16).tobytes()] * 2,
                combined="Please explain the result", duration=2.0, speech=True,
                interaction_id="device-1", speaker="Alice", cancelled=cancelled,
                valid=valid)
    return invoke, args, realtime, runner, result


def test_finalized_turn_uploads_resampled_audio_and_forwards_cancel(executor):
    invoke, args, realtime, runner, result = executor
    assert invoke(**args) is result
    assert realtime.append_audio.call_count == 2
    uploaded = realtime.append_audio.call_args.args[0]
    assert uploaded.dtype == np.float32
    assert len(uploaded) == 480
    realtime.send_text.assert_called_once()
    assert "Alice" in realtime.send_text.call_args.args[0]
    assert runner.call_args.kwargs["stop_event"] is args["cancelled"]
    assert runner.call_args.kwargs["suppress_visual_feedback"] is True
    assert runner.call_args.kwargs["harness_followup"] is False
    realtime.finish_capture.assert_called_once()
    realtime.recover_session.assert_not_called()
    realtime.save_main_handoff.assert_not_called()


@pytest.mark.parametrize("stage", ["prepare", "wait", "bind", "upload"])
def test_invalidated_turn_is_never_committed(executor, stage):
    invoke, args, realtime, runner, _ = executor
    methods = {"prepare": realtime.prepare_turn, "wait": realtime.wait_until_available,
               "bind": realtime.bind_audio_turn, "upload": realtime.append_audio}

    def invalidate(*unused, **kwargs):
        args["valid"].return_value = False
        return True

    methods[stage].side_effect = invalidate
    assert invoke(**args).route == module.ROUTE_CANCELLED
    assert args["cancelled"].is_set()
    runner.assert_not_called()
    realtime.finish_capture.assert_called_once()
    realtime.save_main_handoff.assert_not_called()
    if stage in ("bind", "upload"):
        realtime.recover_session.assert_called_once_with(
            "device-turn-discarded", discard_old_on_failure=True,
        )


@pytest.mark.parametrize("state", ["off", "noise", "unavailable", "upload_error"])
def test_skip_or_failure_preserves_main_fallback(executor, monkeypatch, state):
    invoke, args, realtime, runner, _ = executor
    if state == "off":
        monkeypatch.setattr(module.config, "REALTIME_ENABLED", False)
    elif state == "noise":
        args["combined"] = ""
        args["duration"] = 0.01
        args["speech"] = False
    elif state == "unavailable":
        realtime.wait_until_available.return_value = False
    else:
        realtime.append_audio.side_effect = RuntimeError("lost connection")
    result = invoke(**args)
    assert not result.handled
    runner.assert_not_called()
    if state == "noise":
        realtime.save_main_handoff.assert_not_called()
    else:
        realtime.save_main_handoff.assert_called_once_with(args["combined"])
    if state in ("off", "noise"):
        realtime.prepare_turn.assert_not_called()
    if state == "upload_error":
        realtime.recover_session.assert_called_once_with(
            "device-turn-discarded", discard_old_on_failure=True,
        )


@pytest.mark.parametrize("result,save", [
    (RealtimeTurnResult(delegated=True), True),
    (RealtimeTurnResult(route=module.ROUTE_CANCELLED), False),
    (RealtimeTurnResult(route=module.ROUTE_NOISE_DROPPED), False),
])
def test_handoff_memory_preserves_only_downstream_requests(executor, result, save):
    invoke, args, realtime, runner, _ = executor
    runner.return_value = result
    assert invoke(**args) is result
    assert realtime.save_main_handoff.call_count == int(save)


class _TTS:
    def __init__(self):
        self.gate = DeviceInputGate(lambda: False)
        self.observed = threading.Event()
        self.admitted = threading.Event()
        self.frames = []
        self.ended = []

    @property
    def input_capture_state(self):
        self.observed.set()
        with self.gate.lock:
            return bool(self.gate._tokens), 0

    def native_play_begin(self, rate, owner=""):
        return self.gate.submit(lambda: (self.admitted.set() or True), "", can_defer=False)

    def native_play_frame(self, frame):
        self.frames.append(frame)
        return True

    def native_play_end(self, transcript=""):
        self.ended.append(transcript)


def test_native_reply_waits_for_next_capture_and_pins_admitted_tts():
    first, replacement = _TTS(), _TTS()
    current = [first]
    token = first.gate.begin()
    cancel = threading.Event()
    output = module._DeviceOutput(lambda: current[0], cancel, lambda: True)
    result = []
    worker = threading.Thread(target=lambda: result.append(output.native_play_begin(24000)))
    worker.start()
    assert first.observed.wait(1)
    assert not first.admitted.is_set()
    first.gate.end(token)
    worker.join(1)
    assert not worker.is_alive()
    assert result == [True]
    current[0] = replacement
    output.native_play_frame("frame")
    output.native_play_end("spoken")
    assert first.frames == ["frame"]
    assert first.ended == ["spoken"]
    assert not replacement.frames
    assert not replacement.ended


def test_native_admission_wait_cancels_without_speech():
    tts = _TTS()
    token = tts.gate.begin()
    cancel = threading.Event()
    output = module._DeviceOutput(lambda: tts, cancel, lambda: True)
    result = []
    worker = threading.Thread(target=lambda: result.append(output.native_play_begin(24000)))
    worker.start()
    assert tts.observed.wait(1)
    cancel.set()
    worker.join(1)
    assert not worker.is_alive()
    assert result == [False]
    assert not tts.admitted.is_set()
    tts.gate.end(token)


def test_native_admission_has_bounded_wait():
    tts = _TTS()
    token = tts.gate.begin()
    output = module._DeviceOutput(lambda: tts, threading.Event(), lambda: True, timeout=0)
    with pytest.raises(TimeoutError):
        output.native_play_begin(24000)
    tts.gate.end(token)


@pytest.mark.parametrize("method", ["speak", "speak_queue"])
@pytest.mark.parametrize("reason", ["route", "cancel"])
def test_deferred_reply_rechecks_turn_after_finalizer_returns(tmp_path, method, reason):
    tts = tts_service(tmp_path)
    token = tts.begin_device_input()
    cancelled = threading.Event()
    valid = Mock(return_value=True)
    output = module._DeviceOutput(lambda: tts, cancelled, valid)
    tts._speak_sync = Mock()
    tts._report_unspoken_reply = Mock()
    assert getattr(output, method)("Earlier reply", turn_id="old-turn", realtime_reply=True)
    assert not tts.speaking
    # The realtime finalizer has now returned HANDLED and released its queue
    # ticket. A later routing change must still invalidate its deferred speech.
    if reason == "route":
        valid.return_value = False
    else:
        cancelled.set()
    worker = tts._device_input_gate._worker
    tts.end_device_input(token)
    worker.join(1)
    assert not worker.is_alive()
    tts._speak_sync.assert_not_called()
    tts._report_unspoken_reply.assert_not_called()
    assert not tts.speaking


@pytest.mark.parametrize("method", ["speak", "speak_queue"])
def test_valid_deferred_reply_keeps_normal_playback(tmp_path, method):
    tts = tts_service(tmp_path)
    token = tts.begin_device_input()
    played = threading.Event()

    def play(*args, **kwargs):
        assert "_device_turn_valid" not in kwargs
        tts._speaking = False
        tts._lock.release()
        played.set()

    tts._speak_sync = play
    output = module._DeviceOutput(lambda: tts, threading.Event(), lambda: True)
    assert getattr(output, method)("Valid reply", turn_id="old-turn", realtime_reply=True)
    assert not played.is_set()
    tts.end_device_input(token)
    assert played.wait(1)
