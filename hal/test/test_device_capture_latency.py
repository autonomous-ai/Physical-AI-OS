"""Local capture cues must not wait for the cloud handshake or final transcript."""

import threading
from unittest.mock import Mock, call

import pytest

from hal.drivers.voice._internal.device_capture import DeviceCapture
from hal.drivers.voice._internal.harness_capture import Capture


def fixture():
    capture = Capture({})
    tts = Mock()
    controller = DeviceCapture(capture, tts, Mock(), lambda: not capture.cancelled.is_set())
    return capture, tts, controller


def test_local_ready_before_connection_and_first_words_preserved():
    capture, tts, controller = fixture()
    connected = threading.Event()
    reads = []

    def read(_):
        reads.append(len(reads))
        if len(reads) == 3:
            tts.play_device_capture_chime.assert_called_once_with(finished=False)
            assert not connected.is_set()
        if len(reads) == 4:
            connected.set()
        return bytes([len(reads)]), False

    result = controller.prepare(Mock(read=read), 320, 16000, connected, lambda data: data)
    assert result == [b'\x02', b'\x03', b'\x04']
    assert not capture.cancelled.is_set()


def test_finish_while_connecting_stops_reads_and_cues_before_connection():
    capture, tts, controller = fixture()
    connected = Mock()
    connected.is_set.return_value = False
    reads = []

    def read(_):
        reads.append(1)
        assert len(reads) <= 3, "read after user finished"
        if len(reads) == 3:
            capture.finished.set()
        return b'first words', False

    def wait(**_):
        tts.play_device_capture_chime.assert_any_call(finished=True)
        connected.is_set.return_value = True

    connected.wait.side_effect = wait
    assert controller.prepare(Mock(read=read), 320, 16000, connected, lambda data: data) == [b'first words', b'first words']
    controller.stop()
    assert tts.play_device_capture_chime.call_args_list == [call(finished=False), call(finished=True)]


@pytest.mark.parametrize("reason", ["cancel", "route", "timeout"])
def test_pending_connection_fails_closed(reason):
    capture, tts, controller = fixture()
    connected = threading.Event()
    reads = []

    def read(_):
        reads.append(1)
        if len(reads) == 2:
            if reason == "cancel":
                capture.cancelled.set()
            elif reason == "route":
                controller.valid = lambda: False
        return b'audio', False

    result = controller.prepare(Mock(read=read), 320, 16000, connected, lambda data: data,
                                timeout=0 if reason == "timeout" else 1)
    assert result is None
    assert capture.cancelled.is_set()
    assert tts.play_device_capture_chime.call_args_list == [call(finished=False)]


def test_finish_before_microphone_ready_is_not_announced():
    capture, tts, controller = fixture()
    capture.finished.set()
    mic = Mock()
    assert controller.prepare(mic, 320, 16000, threading.Event(), lambda data: data) is None
    mic.read.assert_not_called()
    tts.play_device_capture_chime.assert_not_called()


def test_finish_feedback_is_local_and_does_not_fetch_route():
    capture, tts, controller = fixture()
    controller.ready = True
    controller.valid = Mock(side_effect=AssertionError("local stop must not wait for HTTP"))
    controller.stop()
    tts.play_device_capture_chime.assert_called_once_with(finished=True)
    controller.valid.assert_not_called()


def test_buffer_has_hard_pcm_bound_without_dropping_first_words():
    capture, tts, controller = fixture()
    mic = Mock(read=Mock(return_value=(b'audio', False)))
    assert controller.prepare(mic, 320, 16000, threading.Event(), lambda data: data,
                              timeout=0.1) is None
    assert capture.cancelled.is_set()
    assert mic.read.call_count <= 6  # First frame + five buffered frames.


@pytest.mark.parametrize("edge", ["cancel", "finish"])
def test_edge_during_ready_cue_prevents_further_microphone_reads(edge):
    capture, tts, controller = fixture()
    connected = threading.Event()
    connected.set()
    def cue(*, finished):
        if not finished:
            getattr(capture, "cancelled" if edge == "cancel" else "finished").set()
    tts.play_device_capture_chime.side_effect = cue
    mic = Mock(read=Mock(return_value=(b'first frame', False)))
    result = controller.prepare(mic, 320, 16000, connected, lambda data: data)
    assert result == (None if edge == "cancel" else [])
    assert mic.read.call_count == 1
    assert tts.play_device_capture_chime.call_args_list == (
        [call(finished=False)] if edge == "cancel" else [call(finished=False), call(finished=True)])


def test_privacy_cancel_during_first_read_never_announces_ready():
    capture, tts, controller = fixture()
    def read(_):
        capture.cancelled.set()
        return b'audio', False
    assert controller.prepare(Mock(read=read), 320, 16000, threading.Event(), lambda data: data) is None
    tts.play_device_capture_chime.assert_not_called()
