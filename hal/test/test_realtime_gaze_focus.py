"""A gaze focus grant at speech end belongs to the captured realtime turn."""

from unittest.mock import Mock, patch

import pytest

from hal.drivers.tracking import gaze
from hal.drivers.voice import voice_service as module
from hal.drivers.voice._internal.realtime_turn import RealtimeTurnResult


@pytest.mark.parametrize('initial_focus,end_focus,noise,expected', [
    (False, True, False, True),
    (False, False, False, False),
    (True, False, False, True),
    (False, True, True, False),
])
def test_speech_end_focus_controls_same_capture(
        monkeypatch, initial_focus, end_focus, noise, expected):
    request = 'How are you doing today?'
    service = Mock()
    service._running = False
    service._tts = None
    service._wakeword_focus.is_active.return_value = initial_focus
    service._realtime.available = True
    service._realtime.rebuilding = False
    service._decorator.classify_wake_word.return_value = (request, 'voice_followup')
    service._decorator.starts_with_wake_word.return_value = False
    service._decorator.identify_and_decorate.return_value = (request, None, None)
    stt = Mock()
    stt.is_closed.return_value = False
    monkeypatch.setattr(module.hal_config, 'REALTIME_ENABLED', True)
    monkeypatch.setattr(module.hal_config, 'WAKEWORD_ENABLED', True)
    monkeypatch.setattr(module.voice_cfg, 'LIVE_MODE', False)

    def speech_end():
        service._wakeword_focus.is_active.return_value = end_focus

    with patch.object(gaze, 'on_speech_end', side_effect=speech_end) as recheck, \
         patch.object(module, 'finalize_session', return_value=(request, [], 2.0)), \
         patch.object(module, 'is_noise_turn', return_value=noise), \
         patch.object(module, 'run_realtime_turn', return_value=RealtimeTurnResult()) as realtime, \
         patch.object(module, '_WaitFiller'), \
         patch.object(module, 'dispatch_turn'), \
         patch.object(module, 'voice_metrics'), \
         patch.object(module.requests, 'post'):
        module.VoiceService._stream_session(
            service, Mock(), 320, 16000, speech_pre_buffer=[],
            preconnected_session=stt,
            harness_voice={'enabled': False, 'generation': 1},
        )

    recheck.assert_called_once()
    assert realtime.call_count == int(expected)
    assert service._realtime.prepare_turn.call_count == int(expected)
    if expected:
        assert realtime.call_args.args[3] == request
        service._realtime.send_text.assert_called_once()
    else:
        service._realtime.send_text.assert_not_called()
