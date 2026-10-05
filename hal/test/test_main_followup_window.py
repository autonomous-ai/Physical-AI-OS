"""A heard main-agent question opens a follow-up window for the user's answer (#564)."""

import pytest

import hal.config as hal_config
from hal.drivers.voice._internal import main_followup as mf
from hal.drivers.voice.voice_service import VoiceService

QUESTION = "What name should I save you under?"


class _RecordingRealtime:
    def save_main_agent_reply_fragment(self, text: str) -> None:
        pass

    def send_text(self, text: str) -> None:
        pass


def _service() -> VoiceService:
    service = object.__new__(VoiceService)
    service._realtime = _RecordingRealtime()
    return service


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(hal_config, "REALTIME_MAIN_FOLLOWUP_S", 60, raising=False)


def test_question_detection():
    assert mf.ends_with_question(QUESTION)
    assert mf.ends_with_question(QUESTION + '"  ')
    assert mf.ends_with_question("Bạn tên là gì？")
    assert not mf.ends_with_question("Done — I'll remember you as Momo.")
    assert not mf.ends_with_question("")


def test_spoken_main_question_opens_window_with_text():
    _service().feed_realtime_history(QUESTION)
    assert mf.pending_main_question() == QUESTION


def test_take_consumes_once():
    mf.note_main_reply(QUESTION, heard=True)
    assert mf.take_main_followup()
    assert not mf.take_main_followup()
    assert mf.pending_main_question() == ""


def test_statement_closes_open_window():
    service = _service()
    service.feed_realtime_history(QUESTION)
    service.feed_realtime_history("Got it, saving you as Momo — hold still.")
    assert mf.pending_main_question() == ""


def test_last_fragment_decides():
    service = _service()
    service.feed_realtime_history("Hi there!")
    service.feed_realtime_history(QUESTION)
    assert mf.pending_main_question() == QUESTION


def test_unspoken_question_does_not_open():
    _service().feed_realtime_history(QUESTION, spoken=False)
    assert mf.pending_main_question() == ""


def test_interrupted_question_still_opens():
    _service().feed_realtime_history(QUESTION, spoken=False, interrupted=True)
    assert mf.pending_main_question() == QUESTION


def test_window_expires(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(mf.time, "monotonic", lambda: clock[0])
    mf.note_main_reply(QUESTION, heard=True)
    clock[0] += 61
    assert mf.pending_main_question() == ""
    assert not mf.take_main_followup()


def test_zero_disables(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_MAIN_FOLLOWUP_S", 0, raising=False)
    mf.note_main_reply(QUESTION, heard=True)
    assert mf.pending_main_question() == ""
