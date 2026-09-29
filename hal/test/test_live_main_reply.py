"""Main-agent speech must survive an idle LIVE session."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hal import config
from hal.drivers.voice.voice_service import VoiceService
from hal.models import SpeakRequest
from hal.realtime.models.output import InterruptedOutput, UserSpeechOutput
from hal.routes import voice
from hal.test.test_live_voice_metrics import _pump
from hal.test.test_voice_metrics import kpi  # noqa: F401 -- clock/transport fixture


@pytest.mark.parametrize("provider", ["gemini", "openai", "gptlive"])
@pytest.mark.parametrize("live", [False, True])
def test_both_segments_reach_existing_queue_when_live_opens(monkeypatch, live, provider):
    monkeypatch.setattr(config, "REALTIME_PROVIDER", provider)
    tts = SimpleNamespace(available=True, speak_queue=Mock(return_value=True))
    service = SimpleNamespace(live_active=False, live_speaker_busy=False)
    monkeypatch.setattr(voice.state, "tts_service", tts)
    monkeypatch.setattr(voice.state, "voice_service", service)
    monkeypatch.setattr(voice.state, "_speaker_muted", False)
    monkeypatch.setattr(voice.state, "music_service", None)
    for text in ("Nhớ đầy đủ này, Leo.", "Hôm nay chúng ta nói về âm nhạc."):
        assert voice.speak_queue_text(SpeakRequest(
            text=text, turn_id="main-38", turn_seq=62, realtime_feedback=True,
        )) == {"status": "ok"}
        service.live_active = live
    assert [c.args[0] for c in tts.speak_queue.call_args_list] == [
        "Nhớ đầy đủ này, Leo.", "Hôm nay chúng ta nói về âm nhạc.",
    ]


@pytest.mark.parametrize("provider", ["gemini", "openai", "gptlive"])
@pytest.mark.parametrize("main_reply", [True, False])
def test_live_cleanup_and_output_reset_only_stop_live_playback(monkeypatch, main_reply, provider):
    monkeypatch.setattr(config, "REALTIME_PROVIDER", provider)
    service = object.__new__(VoiceService)
    service._tts = SimpleNamespace(realtime_speaking=not main_reply, stop=Mock())
    service._live_stop_output()
    assert service._tts.stop.call_count == (0 if main_reply else 1)


@pytest.mark.parametrize("addressed, expected_stops", [(True, 1), (False, 0)])
def test_new_addressed_speech_interrupts_main_once(monkeypatch, kpi, addressed, expected_stops):
    monkeypatch.setattr(config, "REALTIME_PROVIDER", "gemini")
    spoken = _pump(monkeypatch, kpi, [([
        InterruptedOutput(),
        UserSpeechOutput(turn_id="noise", transcript=""),
        UserSpeechOutput(turn_id="new", transcript="Stop"),
        UserSpeechOutput(turn_id="new", transcript="Stop please"),
    ], "new", False)], main_reply=True, addressed=addressed)
    assert sum(text == "__stop__" for text, _ in spoken) == expected_stops


@pytest.mark.parametrize("provider, muted, music, expected", [
    ("gemini", True, False, "suppressed"),
    ("gemini", False, True, 409),
])
def test_existing_mute_and_music_guards(monkeypatch, provider, muted, music, expected):
    from fastapi import HTTPException

    monkeypatch.setattr(config, "REALTIME_PROVIDER", provider)
    tts = SimpleNamespace(available=True, speak_queue=Mock(return_value=True))
    monkeypatch.setattr(voice.state, "tts_service", tts)
    monkeypatch.setattr(voice.state, "voice_service", SimpleNamespace(live_active=True))
    monkeypatch.setattr(voice.state, "_speaker_muted", muted)
    monkeypatch.setattr(voice.state, "music_service", SimpleNamespace(streaming=music))
    if isinstance(expected, int):
        with pytest.raises(HTTPException) as error:
            voice.speak_queue_text(SpeakRequest(text="Main reply"))
        assert error.value.status_code == expected
    else:
        assert voice.speak_queue_text(SpeakRequest(text="Main reply")) == {"status": expected}
    tts.speak_queue.assert_not_called()
