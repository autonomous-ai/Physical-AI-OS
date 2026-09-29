"""POST /voice/wake-focus forwards to voice_service.grant_wakeword_focus."""
from unittest.mock import MagicMock

import hal.app_state as state
from hal.routes.voice import grant_wake_focus


def test_wake_focus_route(monkeypatch):
    monkeypatch.setattr(state, "voice_service", None)
    assert grant_wake_focus()["status"] == "unavailable"

    voice = MagicMock()
    voice.grant_wakeword_focus.return_value = True
    monkeypatch.setattr(state, "voice_service", voice)
    assert grant_wake_focus("boot_greeting")["status"] == "ok"
    voice.grant_wakeword_focus.assert_called_with("boot_greeting")

    voice.grant_wakeword_focus.return_value = False
    assert grant_wake_focus()["status"] == "skipped"
