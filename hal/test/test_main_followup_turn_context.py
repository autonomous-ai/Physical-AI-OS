"""Realtime is told, on the turn itself, that main is waiting for this answer (#564)."""

import pytest

import hal.config as hal_config
from hal.drivers.voice._internal import main_followup as mf
from hal.drivers.voice._internal import realtime_turn as module

QUESTION = "What name should I save you under?"


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_MAIN_FOLLOWUP_S", 60)
    monkeypatch.setattr(module, "device_city", lambda: "")
    monkeypatch.setattr(module, "_reply_language_name", lambda: "English")


def test_open_window_adds_delegate_note_with_question():
    mf.note_main_reply(QUESTION, heard=True)

    ctx = module.build_turn_context("Momo")

    assert "Main agent is waiting for this answer" in ctx
    assert QUESTION in ctx
    assert "delegate_to_main" in ctx
    assert mf.pending_main_question() == QUESTION  # building context must not consume


def test_closed_window_has_no_note():
    assert "Main agent is waiting" not in module.build_turn_context("Momo")
