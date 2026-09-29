"""Tests for opening the voice follow-up window when a person enters."""

import os
from unittest import mock

# Face perception mkdirs at import; redirect off /root so this runs on a dev host.
os.environ.setdefault("HAL_USERS_DIR", "/tmp/autonomous-hal-test-users")
os.environ.setdefault("HAL_STRANGERS_DIR", "/tmp/autonomous-hal-test-strangers")

import hal.app_state as state
import hal.config as config
from hal.drivers.sensing.sensing_service import SensingService


def test_recognized_presence_enter_grants_the_existing_wakeword_focus_window(monkeypatch):
    voice = mock.Mock()
    monkeypatch.setattr(state, "voice_service", voice)

    SensingService._grant_wakeword_focus_for_presence(
        "Person detected — new: friend (leo); faces in frame: 1 (leo)"
    )

    voice.grant_wakeword_focus.assert_called_once_with("presence.enter")


def test_stranger_only_presence_enter_does_not_grant_wakeword_focus(monkeypatch):
    voice = mock.Mock()
    monkeypatch.setattr(state, "voice_service", voice)
    monkeypatch.setattr(config, "PRESENCE_WAKE_STRANGERS", False)

    SensingService._grant_wakeword_focus_for_presence(
        "Person detected — new: stranger (stranger_1); faces in frame: 1 (stranger_1)"
    )

    voice.grant_wakeword_focus.assert_not_called()


def test_stranger_only_presence_enter_can_grant_wakeword_focus_for_guest_mode(monkeypatch):
    voice = mock.Mock()
    monkeypatch.setattr(state, "voice_service", voice)
    monkeypatch.setattr(config, "PRESENCE_WAKE_STRANGERS", True)

    SensingService._grant_wakeword_focus_for_presence(
        "Person detected — new: stranger (stranger_1); faces in frame: 1 (stranger_1)"
    )

    voice.grant_wakeword_focus.assert_called_once_with("presence.enter")


def test_presence_enter_without_voice_pipeline_is_a_noop(monkeypatch):
    monkeypatch.setattr(state, "voice_service", None)

    SensingService._grant_wakeword_focus_for_presence(
        "Person detected — new: friend (leo); faces in frame: 1 (leo)"
    )


def test_a_stranger_joining_a_present_friend_does_not_grant_wakeword_focus(monkeypatch):
    """`already present: leo (friend)` is not an arrival — the voice gate stays shut (#426)."""
    voice = mock.Mock()
    monkeypatch.setattr(state, "voice_service", voice)
    monkeypatch.setattr(config, "PRESENCE_WAKE_STRANGERS", False)

    SensingService._grant_wakeword_focus_for_presence(
        "Person detected — new: stranger (stranger_1); "
        "already present: leo (friend); faces in frame: 2 (leo, stranger_1)"
    )

    voice.grant_wakeword_focus.assert_not_called()
