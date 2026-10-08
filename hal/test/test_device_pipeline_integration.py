"""Admission, capture ownership and TTS guard wiring for device turns."""

from contextlib import contextmanager
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest

from hal.drivers.voice._internal.device_input import DeviceInputLease, DeviceTapInput
from hal.drivers.voice._internal.device_turn_queue import DeviceTurnQueue
from hal.drivers.voice._internal.harness_capture import HarnessCapture
from hal.drivers.voice._internal.input_policy import same_capture_target
from hal.drivers.voice import voice_service as module


@pytest.fixture
def control(monkeypatch):
    monkeypatch.setattr(module.hal_config, "VOICE_INPUT_MODE", "tap_to_talk")
    queue = DeviceTurnQueue()
    owner = HarnessCapture(target_matches=same_capture_target)
    route = {"enabled": False, "generation": 1}
    tokens = []
    ended = []

    def begin():
        token = object()
        tokens.append(token)
        return token

    controller = DeviceTapInput(
        owner, owner.start, read_mode=lambda: dict(route), turn_queue=queue,
        begin_input=begin, end_input=ended.append,
    )
    yield SimpleNamespace(queue=queue, owner=owner, route=route, tokens=tokens,
                          ended=ended, input=controller)
    controller.cancel()
    assert queue.shutdown(2)


def test_admission_binds_guard_and_cancellation_before_claim(control):
    assert control.input.start()
    capture = control.owner.claim(control.route)
    assert capture.cancelled is capture.reservation.cancelled
    assert len(control.tokens) == 1 and control.ended == []
    control.input.cancel()
    assert capture.cancelled.is_set()
    assert control.ended == control.tokens
    assert not control.input.claim(capture)
    control.input.release(capture)
    assert control.ended == control.tokens


def test_privacy_after_worker_claim_retains_guard_until_mic_release(control):
    assert control.input.start()
    capture = control.owner.claim(control.route)
    assert control.input.claim(capture)
    control.input.cancel()
    assert capture.cancelled.is_set()
    assert control.ended == []
    control.input.release(capture)
    assert control.ended == control.tokens


def test_route_change_before_claim_releases_guard_and_reservation(control):
    assert control.input.start()
    control.route["generation"] = 2
    control.input.observe(control.route)
    assert control.ended == control.tokens
    assert control.queue.pending == 0
    assert control.owner.claim(control.route) is None


def test_full_queue_rejects_before_guard_and_capture(control):
    first = control.queue.reserve(dict(control.route, deviceInputMode="tap_to_talk"))
    second = control.queue.reserve(dict(control.route, deviceInputMode="tap_to_talk"))
    assert not control.input.start()
    assert control.tokens == [] and not control.owner.active
    assert not first.cancelled.is_set() and not second.cancelled.is_set()


def test_rejected_capture_and_failed_guard_release_reservation(control):
    control.input._start_capture = lambda *a, **kw: False
    assert not control.input.start()
    assert control.ended == control.tokens and control.queue.pending == 0

    def broken_guard():
        raise RuntimeError("guard unavailable")

    control.input._begin_input = broken_guard
    with pytest.raises(RuntimeError, match="guard unavailable"):
        control.input.start()
    assert control.queue.pending == 0


@pytest.mark.parametrize("cancel_while_recording", [False, True])
def test_voice_loop_releases_mic_then_guard_without_waiting_for_finalizer(
        control, monkeypatch, cancel_while_recording):
    assert control.input.start()
    capture = control.owner.claim(control.route)
    network_done = threading.Event()
    order = []
    backend = object()
    monkeypatch.setattr(module, "ArecordStream", lambda **kw: backend)

    @contextmanager
    def open_mic(actual_backend, rate):
        assert actual_backend is backend
        order.append("open")
        try:
            yield "mic"
        finally:
            assert control.ended == []
            order.append("close")

    def pipeline(mic, frame_size, rate, actual_capture, ticket, **kwargs):
        assert mic == "mic" and actual_capture is capture
        assert control.queue.submit(ticket, lambda _: network_done.wait(2), lambda: None)
        if cancel_while_recording:
            control.input.cancel()
            assert control.ended == []
        order.append("local stop")

    service = SimpleNamespace(
        device_input=control.input, _device_capture_valid=lambda snapshot: True,
        _live_running=False, _alsa_device="test", _np=None, _music_is_playing=lambda: False,
        _capture=open_mic, _device_pipeline=SimpleNamespace(run_capture=pipeline),
        _device_turn_queue=control.queue, _harness_capture=control.owner,
    )
    try:
        module.VoiceService._run_device_capture(service, capture, 1024, 16000)
        assert order == ["open", "local stop", "close"]
        assert not control.owner.active
        assert control.ended == control.tokens
        if not cancel_while_recording:
            assert not capture.reservation.done.is_set()
            assert not capture.cancelled.is_set()
            assert control.input.start()
        network_done.set()
        assert capture.reservation.done.wait(1)
    finally:
        network_done.set()


def test_open_mic_failure_releases_owner_guard_and_capacity(control, monkeypatch):
    assert control.input.start()
    capture = control.owner.claim(control.route)

    def fail(**kwargs):
        raise OSError("device busy")

    monkeypatch.setattr(module, "ArecordStream", fail)
    service = SimpleNamespace(
        device_input=control.input, _device_capture_valid=lambda snapshot: True,
        _live_running=False, _alsa_device="test", _np=None, _music_is_playing=lambda: False,
        _device_turn_queue=control.queue, _harness_capture=control.owner,
    )
    module.VoiceService._run_device_capture(service, capture, 1024, 16000)
    assert not control.owner.active
    assert control.queue.pending == 0
    assert control.ended == control.tokens


def test_tts_replacement_retains_original_guard_and_guards_new_service(control):
    old_token, new_token = object(), object()
    old_ended, new_ended, cues = [], [], []
    old = SimpleNamespace(begin_device_input=lambda: old_token, end_device_input=old_ended.append,
                          play_device_capture_chime=lambda **kw: cues.append(("old", kw)))
    new = SimpleNamespace(begin_device_input=lambda: new_token, end_device_input=new_ended.append,
                          play_device_capture_chime=lambda **kw: cues.append(("new", kw)))
    current = [old]
    control.input._begin_input = lambda: DeviceInputLease.begin(current[0])
    control.input._end_input = lambda lease: lease.close()
    assert control.input.start()
    capture = control.owner.claim(control.route)
    assert control.input.claim(capture)
    lease = control.input.input_lease(capture)
    lease.play_device_capture_chime()

    def install(replacement):
        assert lease.owner is new
        current[0] = replacement

    assert control.input.replace_input(lambda: new, install) is new
    assert old_ended == [] and new_ended == []
    lease.play_device_capture_chime(finished=True)
    assert cues == [("old", {"finished": False}), ("new", {"finished": True})]
    control.input.release(capture)
    assert old_ended == [old_token] and new_ended == [new_token]
    control.input.release(capture)
    assert old_ended == [old_token] and new_ended == [new_token]


def test_busy_replacement_cancels_recording_but_does_not_release_guard_before_mic(control):
    ended = []
    old = SimpleNamespace(begin_device_input=lambda: "old-token", end_device_input=ended.append)
    new = SimpleNamespace(begin_device_input=lambda: None)
    control.input._begin_input = lambda: DeviceInputLease.begin(old)
    control.input._end_input = lambda lease: lease.close()
    assert control.input.start()
    capture = control.owner.claim(control.route)
    assert control.input.claim(capture)
    control.input.replace_input(lambda: new, lambda replacement: None)
    assert capture.cancelled.is_set()
    assert ended == []
    control.input.release(capture)
    assert ended == ["old-token"]


@pytest.mark.parametrize("mode, harness, expected_sleep", [
    ("tap_to_talk", False, []), ("automatic", False, [0.5]),
    ("tap_to_talk", True, [0.5]),
])
def test_device_startup_skips_fixed_delay_but_other_modes_retain_it(monkeypatch, mode, harness, expected_sleep):
    sleeps = []
    monkeypatch.setattr(module.hal_config, "VOICE_INPUT_MODE", mode)
    monkeypatch.setattr(module.hal_config, "REALTIME_ENABLED", False)
    monkeypatch.setattr(module, "read_voice_mode", lambda: {"enabled": harness, "generation": 1})
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(module.aec, "configure", lambda rate: True)
    monkeypatch.setattr(module.cpu_affinity, "pin_current_thread", lambda role: True)
    service = SimpleNamespace(_running=False, _alsa_device="test")
    module.VoiceService._loop(service)
    assert sleeps == expected_sleep


def test_route_replaces_tts_before_publish_and_stops_deferred_old_voice(monkeypatch):
    from hal.routes import voice as route

    old = Mock(available=True, speaking=False, _voice="old", _instructions=None, _provider="openai")
    new = Mock(available=True, _voice="new")
    voice = SimpleNamespace(available=True, _tts=old)
    monkeypatch.setattr(route.state, "simulation_audio", False)
    monkeypatch.setattr(route.state, "voice_service", voice)
    monkeypatch.setattr(route.state, "tts_service", old)
    monkeypatch.setattr(route.state, "music_service", None)

    def construct(**kwargs):
        assert route.state.tts_service is old
        old.stop.assert_called_once()
        old.release_stream.assert_called_once()
        return new

    def replace(factory):
        replacement = factory()
        assert route.state.tts_service is old
        voice._tts = replacement
        return replacement

    voice.replace_tts_service = replace
    monkeypatch.setattr(route, "TTSService", construct)
    assert route.start_voice(route.VoiceStartRequest(
        llm_api_key="test", llm_base_url="http://localhost", tts_voice="new",
    )) == {"status": "already_running"}
    assert voice._tts is new and route.state.tts_service is new
