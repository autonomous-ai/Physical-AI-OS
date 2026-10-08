"""Exclusive device input defers mandatory replies without a playback claim."""

import threading
from unittest.mock import Mock

import pytest

from hal.drivers.voice.tts.device_input_gate import DeviceInputGate
from hal.test.test_tts_turn_queue import _queue_service


def service(tmp_path):
    tts = _queue_service()
    tts._device_input_gate = DeviceInputGate(lambda: tts.speaking or tts._lock.locked())
    tts._input_capture_lock = threading.RLock()
    tts._input_captures = set()
    tts._input_capture_generation = 0
    tts._tts_cache_path = lambda _: tmp_path / "not-cached.wav"
    tts._owner_suppressed = lambda _: False
    tts._begin_playback = Mock()
    return tts


@pytest.mark.parametrize("method", ["speak", "speak_cached", "speak_queue"])
def test_mandatory_reply_waits_without_speaking_flag(tmp_path, method):
    tts = service(tmp_path)
    played = threading.Event()
    def play(*_args, **_kwargs):
        tts._speaking = False
        tts._lock.release()
        played.set()
    tts._speak_sync = play
    tts._cached_play_thread = play
    token = tts.begin_device_input()
    assert token is not None
    assert getattr(tts, method)("reply", realtime_feedback=True, turn_id="run-1")
    assert not tts.speaking and not tts._lock.locked()
    assert not played.is_set()
    tts.end_device_input(token)
    tts.end_device_input(token)
    assert played.wait(1)


def test_worker_fifo_and_capacity_do_not_spawn_per_item():
    gate = DeviceInputGate(lambda: False, max_items=2, max_bytes=2)
    token = gate.begin()
    played = []
    complete = threading.Event()
    assert gate.submit(lambda: played.append(1) or True, "a")
    worker = gate._worker
    assert gate.submit(lambda: (played.append(2), complete.set(), True)[-1], "b")
    assert gate._worker is worker
    assert not gate.submit(lambda: True, "c")
    gate.end(token)
    assert complete.wait(1)
    assert played == [1, 2]


def test_stop_cancels_deferred_before_capture_releases(tmp_path):
    tts = service(tmp_path)
    token = tts.begin_device_input()
    tts._speak_sync = Mock()
    assert tts.speak("discard on explicit stop")
    tts.stop()
    assert not tts._device_input_gate._pending
    tts.end_device_input(token)
    tts._speak_sync.assert_not_called()


@pytest.mark.parametrize("reason", ["muted", "suppressed"])
def test_replay_rechecks_mute_and_suppression(tmp_path, reason):
    tts = service(tmp_path)
    token = tts.begin_device_input()
    tts._speak_sync = Mock()
    reported = threading.Event()
    tts._on_unspoken_reply = lambda _: reported.set()
    tts._note_speech_muted = Mock()
    assert tts.speak("reply", realtime_feedback=True, turn_id="main")
    if reason == "muted":
        tts._speaker_muted = lambda: True
    else:
        tts._owner_suppressed = lambda _: True
    tts.end_device_input(token)
    assert reported.wait(1)
    tts._speak_sync.assert_not_called()


def test_optional_and_native_playback_cannot_claim_exclusive_input(tmp_path):
    tts = service(tmp_path)
    token = tts.begin_device_input()
    assert not tts.speak("optional filler", interruptible=True)
    assert not tts.native_play_begin(24000)
    assert not tts.speaking
    tts.end_device_input(token)


def test_input_cannot_race_speech_admission_before_speaking_flag():
    gate = DeviceInputGate(lambda: False)
    admitted = threading.Event()
    finish = threading.Event()
    def invoke():
        admitted.set()
        return finish.wait(1)
    thread = threading.Thread(target=lambda: gate.submit(invoke, "reply"))
    thread.start()
    try:
        assert admitted.wait(1)
        assert gate.begin() is None
    finally:
        finish.set()
        thread.join(1)
    assert gate.begin() is not None


def test_input_cannot_take_speaker_during_internal_queue_handoff(tmp_path):
    tts = service(tmp_path)
    # End-of-speech clears _speaking before handing its held lock to the tail.
    tts._lock.acquire()
    assert not tts.speaking
    assert tts.begin_device_input() is None
    tts._lock.release()
    assert tts.begin_device_input() is not None


def test_stop_between_replay_and_claim_does_not_resurrect_reply(tmp_path):
    tts = service(tmp_path)
    token = tts.begin_device_input()
    admitted = threading.Event()
    resume = threading.Event()
    reported = threading.Event()
    original_claim = tts._claim_speech
    def claim(*args, **kwargs):
        admitted.set()
        assert resume.wait(1)
        return original_claim(*args, **kwargs)
    tts._claim_speech = claim
    tts._speak_sync = Mock()
    tts._on_unspoken_reply = lambda _: reported.set()
    assert tts.speak("reply", realtime_feedback=True)
    tts.end_device_input(token)
    assert admitted.wait(1)
    tts.stop()
    resume.set()
    assert reported.wait(1)
    assert not tts.speaking and not tts._lock.locked()
    tts._speak_sync.assert_not_called()


def test_device_reservation_blocks_stale_optional_listening_cue(tmp_path):
    tts = service(tmp_path)
    before = tts.input_capture_state
    token = tts.begin_device_input()
    assert tts.input_capture_state[0]
    assert not tts.prepare_listening_cue(before)
    tts.end_device_input(token)
    assert not tts.input_capture_state[0]


def test_next_capture_wins_before_deferred_replay_without_releasing_old_token():
    gate = DeviceInputGate(lambda: False)
    token = gate.begin()
    played = threading.Event()
    assert gate.submit(lambda: played.set() or True, "reply")
    with gate.lock:
        gate.end(token)
        next_token = gate.begin()
        assert next_token is not None
    gate.end(token)
    assert not played.is_set()
    gate.end(next_token)
    assert played.wait(1)


def test_synchronous_preemption_preserves_its_own_admission_generation():
    gate = DeviceInputGate(lambda: False)
    def invoke():
        gate.cancel()
        assert gate.current_valid()
        return True
    assert gate.submit(invoke, "newer reply")


@pytest.mark.parametrize("reason", ["muted", "unavailable", "suppressed"])
def test_known_ineligible_speech_is_not_accepted_into_deferral(tmp_path, reason):
    tts = service(tmp_path)
    token = tts.begin_device_input()
    tts._note_speech_muted = Mock()
    if reason == "muted":
        tts._speaker_muted = lambda: True
    elif reason == "suppressed":
        tts._owner_suppressed = lambda _: True
    else:
        tts._backend = None
    assert not tts.speak_queue("reply", turn_id="main", realtime_feedback=True)
    assert not tts._device_input_gate._pending
    tts.end_device_input(token)


def test_gate_close_joins_worker_and_rejects_further_admission():
    gate = DeviceInputGate(lambda: False)
    gate.begin()
    assert gate.submit(lambda: True, "reply")
    worker = gate._worker
    assert gate.close()
    assert not worker.is_alive()
    assert gate.begin() is None
    assert not gate.submit(lambda: True, "late reply")
    assert gate.close()


def test_empty_deferral_worker_retires_instead_of_leaking_service():
    gate = DeviceInputGate(lambda: False)
    token = gate.begin()
    assert gate.submit(lambda: True, "reply")
    worker = gate._worker
    gate.end(token)
    worker.join(1)
    assert not worker.is_alive()
    assert gate._worker is None
