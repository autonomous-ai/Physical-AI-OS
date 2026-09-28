"""Acknowledgement and commit do not wait for slow speaker recognition."""
import threading
from unittest.mock import Mock, patch

from hal.drivers.voice import voice_service as module
from hal.drivers.voice._internal.realtime_turn import RealtimeTurnResult, ROUTE_DELEGATED


def test_slow_identity_overlaps_realtime_but_is_ready_for_dispatch(monkeypatch):
    service = Mock()
    service._running = False
    service._tts = None
    service._wakeword_focus.is_active.return_value = False
    service._realtime.available = True
    service._realtime.rebuilding = False
    service._decorator.classify_wake_word.return_value = ('Please turn on the light', 'voice')
    service._decorator.starts_with_wake_word.return_value = False
    identity_release = threading.Event()
    identity_started = threading.Event()
    identity = ('Speaker - Mai: Please turn on the light', 'mai', 'Mai')

    def identify(*args, **kwargs):
        identity_started.set()
        assert identity_release.wait(2)
        return identity

    service._decorator.identify_and_decorate.side_effect = identify
    stt = Mock()
    stt.is_closed.return_value = False
    monkeypatch.setattr(module.hal_config, 'REALTIME_ENABLED', True)
    monkeypatch.setattr(module.hal_config, 'WAKEWORD_ENABLED', False)
    monkeypatch.setattr(module.voice_cfg, 'LIVE_MODE', False)
    monkeypatch.setattr(module.voice_cfg, 'SPEAKER_PREPASS_COMMIT_JOIN_S', 0.01)
    filler = Mock()

    def reply(*args, **kwargs):
        assert identity_started.is_set()
        assert not identity_release.is_set()
        filler.arm.assert_called_once()
        assert kwargs['wait_filler'] is filler
        identity_release.set()
        return RealtimeTurnResult(delegated=True, delegate_msg='Please turn on the light', route=ROUTE_DELEGATED)

    with patch.object(module, 'read_voice_mode', return_value={'enabled': False, 'generation': 1}), \
         patch.object(module, 'finalize_session', return_value=('Please turn on the light', [], 2.0)), \
         patch.object(module, 'is_noise_turn', return_value=False), \
         patch.object(module, 'run_realtime_turn', side_effect=reply), \
         patch.object(module, '_WaitFiller', return_value=filler), \
         patch.object(module, 'dispatch_turn') as dispatch, \
         patch.object(module, 'voice_metrics'), \
         patch.object(module.requests, 'post'):
        try:
            module.VoiceService._stream_session(service, Mock(), 320, 16000,
                speech_pre_buffer=[], preconnected_session=stt,
                harness_voice={"enabled": False, "generation": 1})
        finally:
            identity_release.set()
    assert dispatch.call_args.kwargs['identity'] == identity
    service._decorator.identify_and_decorate.assert_called_once()
