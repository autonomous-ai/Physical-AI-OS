"""Capture ownership survives cancellation and overlapping mute/unmute."""

import subprocess
import sys
import threading
from unittest.mock import MagicMock, Mock

import numpy as np
import pytest

from hal.drivers.voice._internal.audio_recorder import ArecordStream
from hal.drivers.voice import voice_service as module


def service(monkeypatch):
    s = object.__new__(module.VoiceService)
    s._lifecycle_lock = threading.Lock()
    s._lifecycle_revision = 0
    s._mic_lock = threading.Lock()
    s._active_mic = None
    s._realtime_stop_thread = None
    s._thread = None
    s._running = True
    s._wakeword_focus = Mock()
    s._harness_capture = Mock()
    s.device_input = module.DeviceTapInput(s._harness_capture, s.start_harness_capture)
    s._turn_detector = None
    s._np = np
    s._sd = Mock()
    s._stt = Mock(name='stt')
    monkeypatch.setattr(module.hal_config, 'REALTIME_ENABLED', False)
    monkeypatch.setattr(module.voice_cfg, 'TURN_END_ENABLED', False)
    return s


@pytest.mark.parametrize('low_latency', [False, True])
def test_stop_reaps_recorder_and_unblocks_read(monkeypatch, low_latency):
    s = service(monkeypatch)
    popen = subprocess.Popen
    children = []

    def blocked_recorder(*args, **kwargs):
        child = popen([sys.executable, '-c',
                       'import sys,time; sys.stdout.buffer.write(b"xx"); '
                       'sys.stdout.buffer.flush(); time.sleep(60)'], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(module.ArecordStream.__module__ + '.subprocess.Popen', blocked_recorder)
    reading = threading.Event()
    errors = []

    def capture():
        try:
            with s._capture(ArecordStream('test', 16000, 1, 320, np, low_latency=low_latency)) as mic:
                reading.set()
                while s._running:
                    mic.read(320)
        except IOError:
            pass
        except Exception as error:
            errors.append(error)

    s._thread = threading.Thread(target=capture)
    s._thread.start()
    assert reading.wait(2)
    old_thread = s._thread
    s.stop()
    assert not old_thread.is_alive()
    assert s._thread is None
    assert s._active_mic is None
    assert children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stderr.closed
    assert not errors


def test_abort_kills_and_reaps_unresponsive_recorder():
    stream = ArecordStream('test', 16000, 1, 320, np)
    proc = Mock()
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired('arecord', 2), -9]
    stream._proc = proc
    stream.abort()
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert proc.wait.call_count == 2


def test_manual_recorder_bounds_unresponsive_pipe_teardown():
    stream = ArecordStream('test', 16000, 1, 320, np, low_latency=True)
    proc = Mock()
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired('arecord', .1), -9]
    stream._proc = proc
    stream.abort()
    assert proc.wait.call_args_list[0].kwargs == {'timeout': .1}
    proc.kill.assert_called_once()
    assert proc.wait.call_count == 2


@pytest.mark.parametrize('low_latency', [False, True])
def test_only_manual_recorder_requests_short_period(monkeypatch, low_latency):
    popen = Mock()
    monkeypatch.setattr('hal.drivers.voice._internal.audio_recorder.subprocess.Popen', popen)
    stream = ArecordStream('test', 16000, 1, 320, np, low_latency=low_latency)
    stream.__enter__()
    command = popen.call_args.args[0]
    assert ('--period-time=10000' in command) == low_latency
    assert not any(arg.startswith('--buffer-time') for arg in command)


def test_stopped_service_cannot_open_capture(monkeypatch):
    s = service(monkeypatch)
    s._running = False
    backend = MagicMock()
    with pytest.raises(InterruptedError):
        with s._capture(backend):
            pytest.fail('stopped capture opened')
    backend.__enter__.assert_not_called()
    backend.close.assert_called_once()


def test_background_mute_cannot_be_overtaken_by_unmute(monkeypatch):
    s = service(monkeypatch)
    teardown_entered = threading.Event()
    finish_teardown = threading.Event()
    started = threading.Event()

    def teardown():
        teardown_entered.set()
        assert finish_teardown.wait(2)

    s._stop_locked = teardown
    s._start_locked = started.set
    s.stop(background=True)
    assert not s._running
    assert teardown_entered.wait(2)
    starter = threading.Thread(target=s.start)
    starter.start()
    try:
        assert not started.wait(0.05)
    finally:
        finish_teardown.set()
        starter.join(2)
    assert started.is_set()


@pytest.mark.parametrize('cancel_restart', [False, True])
def test_pending_restart_waits_for_old_worker_and_respects_new_mute(monkeypatch, cancel_restart):
    s = service(monkeypatch)
    s._running = False
    release = threading.Event()
    restarted = threading.Event()
    s._loop = restarted.set
    old_thread = threading.Thread(target=lambda: release.wait(3))
    s._thread = old_thread
    old_thread.start()
    s.start()
    assert s._thread is old_thread
    assert not s._running
    if cancel_restart:
        s._stop_locked = Mock()
        s.stop()
    release.set()
    old_thread.join(2)
    if cancel_restart:
        assert not restarted.wait(0.1)
    else:
        assert restarted.wait(2)


def test_stop_keeps_timed_out_voice_thread(monkeypatch):
    s = service(monkeypatch)
    old_thread = Mock()
    old_thread.is_alive.return_value = True
    s._thread = old_thread
    s.stop()
    assert s._thread is old_thread
    assert not s._running
    old_thread.join.assert_called_once_with(timeout=5)


def test_stop_aborts_raw_backend_under_aec_wrapper(monkeypatch):
    s = service(monkeypatch)
    backend = MagicMock()
    wrapped = MagicMock()
    monkeypatch.setattr(module.aec, 'wrap_mic', Mock(return_value=wrapped))
    with s._capture(backend, 16000) as mic:
        assert mic is wrapped.__enter__.return_value
        s.stop()
        backend.abort.assert_called_once()
        wrapped.abort.assert_not_called()
    wrapped.__exit__.assert_called_once()
    assert s._active_mic is None


def test_mute_preserves_perception_but_close_disposes_it(monkeypatch):
    s = service(monkeypatch)
    s._decorator = Mock()
    s.stop()
    s._decorator.close.assert_not_called()
    s.close()
    s._decorator.close.assert_called_once()


def test_constructor_failure_does_not_start_optional_workers(monkeypatch):
    monkeypatch.setattr(module, 'SileroVADFilter', Mock())
    monkeypatch.setattr(module, 'WebRTCVADFilter', Mock())
    monkeypatch.setattr(module, 'Backchannel', Mock())
    monkeypatch.setattr(module, 'SensingSender', Mock())
    decorator = Mock()
    monkeypatch.setattr(module, 'SpeakerDecorator', decorator)
    monkeypatch.setattr(module, 'RealtimeOrchestrator',
                        Mock(side_effect=RuntimeError('mock realtime init failure')))
    with pytest.raises(RuntimeError, match='mock realtime init failure'):
        module.VoiceService(stt_provider=Mock())
    decorator.assert_not_called()
