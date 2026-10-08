"""The STT socket stays warm while someone is around, and only then."""

from types import SimpleNamespace
from unittest.mock import Mock

import hal.app_state as state
from hal.drivers.sensing.presence_service import PresenceState
from hal.drivers.voice._internal import config as voice_cfg
from hal.drivers.voice._internal import stt_warm
from hal.drivers.voice.voice_service import VoiceService


def test_mode_parsing_keeps_the_legacy_booleans():
    assert stt_warm.normalize_mode("true") == "always"
    assert stt_warm.normalize_mode("always") == "always"
    assert stt_warm.normalize_mode("presence") == "presence"
    assert stt_warm.normalize_mode("false") == "off"
    assert stt_warm.normalize_mode("") == "off"


def test_presence_mode_follows_the_room_and_recent_speech():
    kw = dict(warm_after_s=300.0)
    assert stt_warm.keepalive_wanted("presence", now=1000.0, last_speech_ts=0.0, present=True, **kw)
    assert stt_warm.keepalive_wanted("presence", now=1000.0, last_speech_ts=900.0, present=False, **kw)
    assert not stt_warm.keepalive_wanted("presence", now=1400.0, last_speech_ts=900.0, present=False, **kw)
    assert not stt_warm.keepalive_wanted("presence", now=1000.0, last_speech_ts=0.0, present=False, **kw)
    assert stt_warm.keepalive_wanted("always", now=1000.0, last_speech_ts=0.0, present=False, **kw)
    assert not stt_warm.keepalive_wanted("off", now=1000.0, last_speech_ts=999.0, present=True, **kw)


def test_presence_reads_the_sensing_service(monkeypatch):
    monkeypatch.setattr(state, "sensing_service", None, raising=False)
    assert not stt_warm.presence_present()
    sensing = SimpleNamespace(_presense_service=SimpleNamespace(state=PresenceState.PRESENT))
    monkeypatch.setattr(state, "sensing_service", sensing, raising=False)
    assert stt_warm.presence_present()


def test_voice_service_decision_uses_mode_and_last_transcript(monkeypatch):
    service = Mock()
    service._last_transcript_ts = 0.0
    monkeypatch.setattr(voice_cfg, "STT_KEEPALIVE_MODE", "off")
    assert not VoiceService._stt_keepalive_wanted(service)
    monkeypatch.setattr(voice_cfg, "STT_KEEPALIVE_MODE", "presence")
    monkeypatch.setattr(voice_cfg, "STT_WARM_AFTER_SPEECH_S", 300.0)
    monkeypatch.setattr(stt_warm, "presence_present", lambda: False)
    monkeypatch.setattr(stt_warm, "now", lambda: 1000.0)
    assert not VoiceService._stt_keepalive_wanted(service)
    service._last_transcript_ts = 950.0
    assert VoiceService._stt_keepalive_wanted(service)
    service._last_transcript_ts = 0.0
    monkeypatch.setattr(stt_warm, "presence_present", lambda: True)
    assert VoiceService._stt_keepalive_wanted(service)
