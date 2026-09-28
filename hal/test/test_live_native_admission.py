"""Gemini's native live reply retains its prefix while the speaker is busy."""

from unittest.mock import Mock

import numpy as np

from hal.realtime.models.output import AudioOutput, InterruptedOutput
from hal.test.test_live_voice_metrics import _pump
from hal.test.test_voice_metrics import kpi  # noqa: F401


def speaker():
    tts = Mock(speaking=False, realtime_speaking=True)
    tts._provider = 'gemini'
    tts._voice = 'Kore'
    tts.native_play_begin.side_effect = [False, True]
    tts.native_play_frame.return_value = True
    return tts


def frame(value, key='reply'):
    return AudioOutput(audio=np.full(240, value, dtype=np.float32), user_turn_id=key)


def played(tts):
    return [float(call.args[0][0]) for call in tts.native_play_frame.call_args_list]


def test_gemini_tts_auto_native_preserves_first_busy_frames(monkeypatch, kpi):
    from hal import config
    monkeypatch.setattr(config, 'REALTIME_PROVIDER', 'gemini')
    tts = speaker()
    # Native mode must also activate from Gemini TTS, without the manual flag.
    _pump(monkeypatch, kpi, [([frame(1), frame(2), frame(3)], 'reply', True)],
          native=False, tts=tts)
    assert played(tts) == [1, 2, 3]
    tts.speak.assert_not_called()


def test_new_reply_does_not_inherit_pending_old_audio(monkeypatch, kpi):
    tts = speaker()
    _pump(monkeypatch, kpi, [([frame(1, 'old'), frame(2, 'new')], 'new', True)],
          native=True, tts=tts)
    assert played(tts) == [2]


def test_interruption_discards_pending_prefix(monkeypatch, kpi):
    tts = speaker()
    _pump(monkeypatch, kpi, [([
        frame(1, 'old'), InterruptedOutput(reason='server_interrupt', user_turn_id='old'),
        frame(2, 'old'), frame(3, 'new'),
    ], 'new', True)], native=True, tts=tts)
    assert played(tts) == [3]


def test_failed_frame_does_not_resume_at_sentence_tail(monkeypatch, kpi):
    tts = speaker()
    tts.native_play_begin.side_effect = None
    tts.native_play_begin.return_value = True
    tts.native_play_frame.return_value = False
    _pump(monkeypatch, kpi, [([frame(1), frame(2), frame(3)], 'reply', True)],
          native=True, tts=tts)
    assert played(tts) == [1]
