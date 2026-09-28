"""Hardware AEC live entry uses one adaptive gate, not three stacked speech filters."""
from unittest.mock import Mock
import numpy as np
import pytest
from hal import config
from hal.drivers.voice.voice_service import VoiceService
from hal.drivers.voice._internal.live_gate import AdaptiveLiveGate


def make_service(hardware_aec):
    service = object.__new__(VoiceService)
    service._live_gate = AdaptiveLiveGate() if hardware_aec else None
    service._np = np
    service._music_is_playing = lambda: False
    service._wakeword_focus = Mock(is_active=lambda: False)
    service._realtime = Mock()
    service._realtime.wait_until_available.return_value = True
    service._webrtcvad_is_speech = Mock(return_value=False)
    service._rt_noise_is_speech = Mock(return_value=False)
    return service


@pytest.mark.parametrize('hardware_aec', [True, False])
def test_wake_disabled_aec_live_needs_no_button_or_secondary_noise_filter(monkeypatch, hardware_aec):
    monkeypatch.setattr(config, 'REALTIME_ENABLED', True)
    monkeypatch.setattr(config, 'WAKEWORD_ENABLED', False)
    service = make_service(hardware_aec)
    result = service._live_decision([np.full(320, 800, np.int16).tobytes()])
    assert result == ('live' if hardware_aec else 'skip')
    assert service._rt_noise_is_speech.called is not hardware_aec


def test_aec_live_entry_passes_quiet_speech_without_webrtc_veto(monkeypatch):
    monkeypatch.setattr(config, 'REALTIME_ENABLED', True)
    monkeypatch.setattr(config, 'WAKEWORD_ENABLED', False)
    service = make_service(True)
    frame = np.full((1024, 1), 450, np.int16)
    assert service._vad_entry_is_speech(frame, 16000, 450)
    service._webrtcvad_is_speech.assert_not_called()
    assert not service._vad_entry_is_speech(frame, 16000, 50)


def test_wake_enabled_still_requires_focus(monkeypatch):
    monkeypatch.setattr(config, 'REALTIME_ENABLED', True)
    monkeypatch.setattr(config, 'WAKEWORD_ENABLED', True)
    service = make_service(True)
    assert not service._hardware_aec_live_entry()
    service._rt_noise_is_speech.return_value = True
    assert service._live_decision([]) == 'turn'
    service._wakeword_focus.is_active = lambda: True
    assert service._hardware_aec_live_entry()
    assert service._live_decision([]) == 'live'
