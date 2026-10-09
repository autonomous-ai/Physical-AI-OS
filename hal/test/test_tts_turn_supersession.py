"""A response already in transit cannot become audible after the next tap."""

import threading
from unittest.mock import Mock

import pytest

from hal.drivers.voice.tts import turn_supersession as policy
from hal.drivers.voice.tts.service import TTSService
from hal.test.test_tts_device_input_gate import service


@pytest.fixture(autouse=True)
def reset_policy(monkeypatch):
    monkeypatch.setattr(policy, "_before_ms", 0)


@pytest.mark.parametrize("method", ["speak", "speak_cached", "speak_queue"])
def test_old_response_arriving_after_new_capture_is_rejected(tmp_path, method):
    tts = service(tmp_path)
    tts._owner_suppressed = TTSService._owner_suppressed
    tts._speak_sync = Mock()
    tts._cached_play_thread = Mock()
    policy.suppress_before(1791500000000)
    token = tts.begin_device_input()
    assert not getattr(tts, method)("old answer", turn_id="device-chat-1-1791499999999")
    assert not tts._device_input_gate._pending
    tts.end_device_input(token)
    tts._speak_sync.assert_not_called()
    tts._cached_play_thread.assert_not_called()


def test_previously_deferred_old_reply_drops_but_new_reply_plays(tmp_path):
    tts = service(tmp_path)
    tts._owner_suppressed = TTSService._owner_suppressed
    played = []
    finished = threading.Event()

    def play(text, **kwargs):
        played.append(text)
        tts._speaking = False
        tts._lock.release()
        finished.set()

    tts._speak_sync = play
    token = tts.begin_device_input()
    assert tts.speak_queue("old answer", turn_id="device-chat-1-1791499999999", turn_seq=1)
    policy.suppress_before(1791500000000)
    assert tts.speak_queue("new answer", turn_id="device-chat-2-1791500000001", turn_seq=2)
    tts.end_device_input(token)
    assert finished.wait(1)
    assert played == ["new answer"]


def test_cutoff_is_monotonic_and_does_not_block_unowned_cues():
    policy.suppress_before(1791500000000)
    policy.suppress_before(1791499999000)
    assert policy.owner_superseded("run:device-chat-1-1791500000000")
    assert not policy.owner_superseded("run:device-chat-2-1791500000001")
    assert not policy.owner_superseded("interaction:1791499999999")
    assert not policy.owner_superseded("run:legacy-without-stamp")
    assert not policy.owner_superseded("")


def test_cutoff_survives_tts_service_replacement(tmp_path):
    policy.suppress_before(1791500000000)
    replacement = service(tmp_path)
    replacement._owner_suppressed = TTSService._owner_suppressed
    assert not replacement.speak_queue("old answer", turn_id="device-chat-1-1791499999999")
