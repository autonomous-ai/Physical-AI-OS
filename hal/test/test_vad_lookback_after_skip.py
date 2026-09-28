"""A trigger the live noise-guard rejects must not erase the pre-roll.

Device-observed on lamp-0c4e (ReSpeaker Lite) 2026-09-21: "Play song for me
again" — the plosive "Play" opened a trigger the guard scored as noise (span
0.32s, voiced 0.21) and skipped; "song for me again" re-triggered ~100ms later
with pre-roll=0 because the skip path had cleared the lookback, so Gemini heard
'song for me again.'. The lookback is a bounded deque, so keeping it after a
skip costs nothing and the next trigger carries the rejected word.
"""

from unittest.mock import Mock, patch

import numpy as np
import pytest


def _frame(value):
    return np.full((320, 1), value, dtype=np.int16)


@pytest.mark.parametrize("first_decision", ["skip", "live"])
def test_pre_roll_after_skipped_trigger_keeps_the_rejected_frames(monkeypatch, first_decision):
    from hal.drivers.voice import voice_service as module

    frames = iter([_frame(1000), _frame(2000), _frame(3000)])
    mic = Mock()
    mic.read.side_effect = lambda _: (next(frames), False)

    service = Mock()
    service._running = True
    service._np = np
    service._tts_is_speaking.return_value = False
    service._music_is_playing.return_value = False
    service._backchannel.self_audio_active = False
    service._webrtcvad_is_speech.return_value = True
    service._silero_vad = None  # energy + webrtc gate only; Silero is opt-in
    service._live_decision.side_effect = [first_decision, "live"]
    service._live_session.return_value = True  # ends the loop

    monkeypatch.setattr(module.voice_cfg, "STT_KEEPALIVE", False)
    monkeypatch.setattr(module.voice_cfg, "WARM_MIC", True)
    monkeypatch.setattr(module.voice_cfg, "LIVE_MODE", True)
    monkeypatch.setattr(module.voice_cfg, "RMS_THRESHOLD", 0)
    monkeypatch.setattr(module.voice_cfg, "SPEECH_HOLDOFF_S", 0.0)
    monkeypatch.setattr(module.voice_cfg, "PRE_ROLL_FRAMES", 12)
    with patch.object(module, "read_voice_mode", return_value={}), \
         patch.object(module, "bypass_realtime", return_value=False), \
         patch.object(module, "resample_to_stt", side_effect=lambda f, *a: f):
        module.VoiceService._vad_loop(service, mic, 320, 16000)

    pre_roll = service._live_session.call_args.args[3]
    values = [int(f[0, 0]) for f in pre_roll]
    if first_decision == "skip":
        # The rejected first trigger (frame 1000) rides along as pre-roll.
        assert values == [1000, 2000]
    else:
        # A session that actually opened ends the loop on the first trigger.
        assert values == [1000]
