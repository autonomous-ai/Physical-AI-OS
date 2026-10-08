"""Hot input policy changes preserve privacy and retire the previous capture."""

import threading
import time
from unittest.mock import Mock

import pytest

from hal import app_state, config
from hal.drivers.voice._internal import input_mode, config as voice_config, live_playback
from hal.realtime.config import GeminiConfig, OpenAIConfig
from hal.test.test_voice_capture_lifecycle import service


@pytest.fixture(autouse=True)
def restore_config(monkeypatch):
    for module, names in ((config, ('VOICE_INPUT_MODE', 'WAKEWORD_ENABLED', 'LIVE_MODE',
                                    'REALTIME_TURN_DETECTION')),
                          (voice_config, ('LIVE_MODE',)), (live_playback, ('ENABLED',))):
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))
    monkeypatch.setattr(app_state, '_mic_muted', False)
    monkeypatch.setattr(app_state, '_sleeping', False)
    monkeypatch.setattr(app_state, '_enrolling', False)
    monkeypatch.setattr(app_state.privacy, 'mic_locked', lambda: False)
    monkeypatch.setattr(config, 'VOICE_INPUT_MODE', 'automatic')
    monkeypatch.setattr(config, 'WAKEWORD_ENABLED', True)


def test_transition_cancels_capture_before_publishing_and_acknowledges_ready(monkeypatch):
    s = service(monkeypatch)
    order = []

    def stop(**kwargs):
        assert config.VOICE_INPUT_MODE == 'automatic'
        assert not s._running
        assert kwargs == {'summarize': False}
        order.append('cancel')

    def start():
        assert config.VOICE_INPUT_MODE == 'tap_to_talk'
        order.append('ready')
        s._input_mode_ready.set()
        s._running = True

    s._stop_locked = stop
    s._start_locked = start
    s.set_input_mode('tap_to_talk', True)
    assert order == ['cancel', 'ready']
    assert not config.WAKEWORD_ENABLED


@pytest.mark.parametrize('gate', ['privacy', 'manual_mute', 'sleep', 'enrollment', 'stopped'])
def test_mode_change_does_not_start_blocked_microphone(monkeypatch, gate):
    s = service(monkeypatch)
    s._stop_locked = Mock()
    s._start_locked = Mock()
    if gate == 'privacy':
        monkeypatch.setattr(app_state.privacy, 'mic_locked', lambda: True)
    elif gate == 'manual_mute':
        monkeypatch.setattr(app_state, '_mic_muted', True)
    elif gate == 'sleep':
        monkeypatch.setattr(app_state, '_sleeping', True)
    elif gate == 'enrollment':
        monkeypatch.setattr(app_state, '_enrolling', True)
    else:
        s._running = False
    s.set_input_mode('tap_to_talk', True)
    assert config.VOICE_INPUT_MODE == 'tap_to_talk'
    s._start_locked.assert_not_called()


def test_slow_teardown_rejects_change_without_publishing(monkeypatch):
    s = service(monkeypatch)
    s._running = False
    s._thread = Mock()
    s._thread.is_alive.return_value = True
    s._stop_locked = Mock()
    with pytest.raises(RuntimeError, match='teardown'):
        s.set_input_mode('tap_to_talk', True)
    assert config.VOICE_INPUT_MODE == 'automatic'


def test_live_and_provider_vad_restore_after_multiple_switches(monkeypatch):
    monkeypatch.setenv('HAL_LIVE_MODE', 'true')
    monkeypatch.setenv('HAL_REALTIME_TURN_DETECTION', 'off')
    for _ in range(3):
        input_mode._publish('tap_to_talk', True)
        assert not voice_config.LIVE_MODE
        assert OpenAIConfig().turn_detection_type is None
        assert not GeminiConfig().vad_enabled
        input_mode._publish('automatic', True)
        assert config.LIVE_MODE and voice_config.LIVE_MODE
        assert config.WAKEWORD_ENABLED
        assert OpenAIConfig().turn_detection_type is not None
        assert GeminiConfig().vad_enabled


def test_realtime_transition_skips_memory_network_calls():
    from hal.realtime.orchestrator import RealtimeOrchestrator

    rt = object.__new__(RealtimeOrchestrator)
    rt._connect_retry_stop = threading.Event()
    rt._idle_park_stop = threading.Event()
    rt._lifecycle_lock = threading.Lock()
    rt._started = threading.Event()
    rt._context = Mock()
    rt._context.summarize_device_memory.side_effect = lambda: time.sleep(0.2)
    rt._context.summarize_realtime_memory.side_effect = lambda: time.sleep(0.2)
    rt._agent = Mock()
    start = time.perf_counter()
    rt.stop(summarize=False)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.1
    rt._context.summarize_device_memory.assert_not_called()
    rt._context.summarize_realtime_memory.assert_not_called()


def test_readiness_failure_restores_old_policy_and_schedules_resume(monkeypatch):
    s = service(monkeypatch)
    s._stop_locked = Mock()
    s._start_locked = Mock()
    event = Mock()
    event.wait.return_value = False
    worker = Mock()
    monkeypatch.setattr(input_mode.threading, 'Event', lambda: event)
    monkeypatch.setattr(input_mode.threading, 'Thread', lambda **kwargs: worker)
    with pytest.raises(RuntimeError, match='ready'):
        s.set_input_mode('tap_to_talk', True)
    assert config.VOICE_INPUT_MODE == 'automatic'
    assert config.WAKEWORD_ENABLED
    assert s._stop_locked.call_count == 2
    worker.start.assert_called_once()


def test_requests_serialize_until_previous_transition_completes(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Service:
        def set_input_mode(self, mode, wakeword):
            calls.append(mode)
            if mode == 'tap_to_talk':
                entered.set()
                assert release.wait(2)

    monkeypatch.setattr(app_state, 'voice_service', Service())
    monkeypatch.setattr(app_state, 'simulation_audio', False)
    first = threading.Thread(target=input_mode.set_input_mode, args=('tap_to_talk', True))
    second = threading.Thread(target=input_mode.set_input_mode, args=('automatic', True))
    first.start()
    assert entered.wait(2)
    second.start()
    assert calls == ['tap_to_talk']
    release.set()
    first.join(2)
    second.join(2)
    assert calls == ['tap_to_talk', 'automatic']


def test_endpoint_rejects_invalid_mode_and_reports_runtime_failure(monkeypatch):
    from fastapi import HTTPException
    from pydantic import ValidationError
    from hal.models import VoiceInputModeRequest
    from hal.routes.voice import update_voice_input_mode

    with pytest.raises(ValidationError):
        VoiceInputModeRequest(mode='always_on', wakeword=True)
    apply = Mock(side_effect=RuntimeError('capture still closing'))
    monkeypatch.setattr(input_mode, 'set_input_mode', apply)
    with pytest.raises(HTTPException) as error:
        update_voice_input_mode(VoiceInputModeRequest(mode='tap_to_talk', wakeword=True))
    assert error.value.status_code == 503
    apply.side_effect = None
    assert update_voice_input_mode(VoiceInputModeRequest(mode='automatic', wakeword=True)) == {
        'status': 'ok',
    }


def test_same_mode_cannot_ack_deferred_resume_gap(monkeypatch):
    s = service(monkeypatch)
    s._running = False
    s._input_mode_resume_pending = True
    with pytest.raises(RuntimeError, match='pending'):
        s.set_input_mode('automatic', True)


def test_realtime_stop_retires_blocked_retry_without_waiting(monkeypatch):
    from hal.test.test_realtime_initial_connect_retry import (
        _orchestrator_for_initial_retry, _BlockingAgent,
    )

    rt = _orchestrator_for_initial_retry()
    rt._idle_park_stop = threading.Event()
    rt._idle_park_thread = None
    rt._initial_connect_exc = None
    entered, release, stopped, cleared = (threading.Event() for _ in range(4))
    replacement = _BlockingAgent(entered, release)
    rt._make_agent = lambda *_: replacement
    original_clear = rt._started.clear

    def clear():
        original_clear()
        cleared.set()

    monkeypatch.setattr(rt._started, 'clear', clear)
    rt._start_connect_retry_loop()
    assert entered.wait(1)
    stopper = threading.Thread(target=lambda: (rt.stop(summarize=False), stopped.set()))
    stopper.start()
    assert cleared.wait(1)
    assert stopped.wait(.5)
    assert not rt._begin_rebuild()
    release.set()
    stopper.join(2)
    rt._connect_retry_thread.join(2)
    assert stopped.is_set()
    assert not rt._connect_retry_thread.is_alive()
    assert replacement.disconnected
    assert rt._agent is None
    assert not rt._rebuild_lock.locked()


@pytest.mark.parametrize('action', ['start', 'stop'])
def test_explicit_lifecycle_invalidates_pending_mode_resume(monkeypatch, action):
    s = service(monkeypatch)
    s._running = False
    s._start_locked = Mock()
    s._stop_locked = Mock()
    pending = []

    def worker(**kwargs):
        pending.append(kwargs['target'])
        return Mock()

    monkeypatch.setattr(input_mode.threading, 'Thread', worker)
    input_mode._resume_previous(s, (None, None))
    assert s._input_mode_resume_pending
    getattr(s, action)()
    assert not s._input_mode_resume_pending
    pending[0]()
    s.set_input_mode('automatic', True)
    assert not s._input_mode_resume_pending
    assert s._start_locked.call_count == (1 if action == 'start' else 0)
    if action == 'stop':
        s.set_input_mode('tap_to_talk', True)
        s._start_locked.assert_not_called()
