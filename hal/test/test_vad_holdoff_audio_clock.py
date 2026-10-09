"""Capture scheduling must neither erase real speech nor turn a short sound into speech."""
import threading
from unittest.mock import Mock, patch

import numpy as np
import pytest


@pytest.mark.parametrize('frame_values,holdoff,clock_step,expected_capture', [
    ([1000] * 5, 0.2, 0.0, True),
    ([1000] * 2, 0.2, 0.4, False),
    ([1000, 1000, 0, 1000, 1000], 0.2, 0.0, False),
    ([1000] * 2, 0.05, 0.0, True),
])
def test_holdoff_depends_on_audio_duration(monkeypatch, frame_values, holdoff, clock_step, expected_capture):
    from hal.drivers.voice import voice_service as module

    service = Mock()
    service._running = True
    service._np = np
    service._automatic_reply_lock = threading.Lock()
    service._automatic_reply_stop = None
    service._automatic_reply_cancelled_at = None
    service._tts_is_speaking.return_value = False
    service._music_is_playing.return_value = False
    service._backchannel.self_audio_active = False
    service._hardware_aec_live_entry.return_value = False
    service._webrtcvad_is_speech.return_value = True
    service._vad_entry_is_speech = module.VoiceService._vad_entry_is_speech.__get__(service)
    service._silero_vad = None
    service._stream_session.return_value = True
    frames = iter([np.full((1024, 1), value, dtype=np.int16) for value in frame_values])

    def read(_):
        frame = next(frames, None)
        if frame is None:
            service._running = False
            frame = np.zeros((1024, 1), dtype=np.int16)
        return frame, False

    tick = [100.0]

    def clock():
        tick[0] += clock_step
        return tick[0]

    for name, value in {'STT_KEEPALIVE': False, 'LIVE_MODE': False, 'WARM_MIC': True,
                        'RMS_THRESHOLD': 500, 'SPEECH_HOLDOFF_S': holdoff}.items():
        monkeypatch.setattr(module.voice_cfg, name, value)
    monkeypatch.setattr(module.hal_config, 'WAKEWORD_ENABLED', False)
    monkeypatch.setattr(module.time, 'time', clock)
    with patch.object(module, 'read_voice_mode', return_value={'enabled': False, 'generation': 0}), \
         patch.object(module, 'resample_to_stt', side_effect=lambda data, *args: data), \
         patch('hal.drivers.tracking.gaze.on_speech_start', return_value=False):
        module.VoiceService._vad_loop(service, Mock(read=read), 1024, 16000)
    assert service._stream_session.called is expected_capture
    if expected_capture:
        assert len(service._stream_session.call_args.kwargs['speech_pre_buffer']) == len(frame_values)
