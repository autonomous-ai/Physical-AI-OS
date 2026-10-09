"""Explicit retry cues must never compete with a successful or newer turn."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hal.drivers.voice._internal import device_retry_feedback as module
from hal.drivers.voice._internal.harness_capture import Capture
from hal.drivers.voice._internal.realtime_turn import RealtimeTurnResult
from hal.drivers.voice.tts.service import TTSService
from hal.i18n import PHRASE_VOICE_RETRY, localized_phrase
from hal.test.test_tts_device_input_gate import service


@pytest.fixture
def capture():
    return Capture(dict(enabled=False, deviceInputMode="tap_to_talk",
                        capturedAtMs=1791450223261))


def emit(tts, capture, **changes):
    args = dict(combined="", speech_confirmed=True,
                realtime_result=RealtimeTurnResult(route="realtime_unavailable"),
                valid=lambda: True)
    args.update(changes)
    return module.maybe_emit_retry(tts, capture, **args)


@pytest.mark.parametrize("route", ["realtime_unavailable", "realtime_error",
                                  "realtime_no_output", "realtime_not_started"])
def test_empty_failed_turn_admits_localized_owned_optional_cue(capture, monkeypatch, route):
    monkeypatch.setattr(module, "localized_phrase", lambda _: "Please repeat.")
    tts = SimpleNamespace(speak_cached=Mock(return_value=True))
    assert emit(tts, capture, realtime_result=RealtimeTurnResult(route=route))
    args, kwargs = tts.speak_cached.call_args
    assert args == ("Please repeat.",)
    assert kwargs["interruptible"] and not kwargs["realtime_feedback"]
    assert kwargs["turn_id"] == "tap-retry-1791450223261"
    assert kwargs["_device_turn_valid"]()
    capture.cancelled.set()
    assert not kwargs["_device_turn_valid"]()


@pytest.mark.parametrize("result", [
    RealtimeTurnResult(route=route) for route in
    ["realtime_handled", "delegated", "noise_dropped", "foreign_dropped",
     "realtime_cancelled", "ai_rejected", "unknown"]
] + [RealtimeTurnResult(**{field: True}) for field in
     ["handled", "delegated", "execution_completed", "rejected"]])
def test_terminal_or_successful_realtime_never_retries(capture, result):
    tts = SimpleNamespace(speak_cached=Mock())
    assert not emit(tts, capture, realtime_result=result)
    tts.speak_cached.assert_not_called()


@pytest.mark.parametrize("changes", [dict(combined="hello"), dict(speech_confirmed=False),
                                    dict(speech_confirmed=None), dict(valid=lambda: False)])
def test_no_cue_without_empty_confirmed_current_input(capture, changes):
    tts = SimpleNamespace(speak_cached=Mock())
    assert not emit(tts, capture, **changes)
    tts.speak_cached.assert_not_called()


@pytest.mark.parametrize("changes", [dict(enabled=True), dict(deviceInputMode="automatic"),
                                    dict(unavailable=True), dict(capturedAtMs=None),
                                    dict(capturedAtMs=True), dict(capturedAtMs=1)])
def test_only_timestamped_device_tap_is_eligible(capture, changes):
    capture.snapshot.update(changes)
    assert not emit(SimpleNamespace(speak_cached=Mock()), capture)


def test_cancelled_turn_never_calls_tts(capture):
    capture.cancelled.set()
    tts = SimpleNamespace(speak_cached=Mock())
    assert not emit(tts, capture)
    tts.speak_cached.assert_not_called()


@pytest.mark.parametrize("blocked", ["muted", "capturing", "superseded"])
def test_real_cached_speech_admission_drops_ineligible_cue(tmp_path, capture, blocked):
    tts = service(tmp_path)
    tts._speaker_muted = lambda: blocked == "muted"
    tts._note_speech_muted = Mock()
    tts._owner_suppressed = lambda _: blocked == "superseded"
    tts._cached_play_thread = Mock()
    token = tts.begin_device_input() if blocked == "capturing" else None
    try:
        assert not emit(tts, capture)
        tts._cached_play_thread.assert_not_called()
        assert not tts._device_input_gate._pending
    finally:
        if token:
            tts.end_device_input(token)
        tts._device_input_gate.close()


def test_retry_phrase_is_warmed_without_speaking(monkeypatch):
    from hal import i18n
    monkeypatch.setattr(i18n, "localized_phrase", lambda key: localized_phrase(key, "vi"))
    tts = SimpleNamespace(speak_cached=Mock(return_value=True), _provider="test", _voice="test")
    assert TTSService.warm_lifecycle_phrases(tts) == 5
    tts.speak_cached.assert_any_call(localized_phrase(PHRASE_VOICE_RETRY, "vi"), prerender=True)
    assert all(call.kwargs == {"prerender": True} for call in tts.speak_cached.call_args_list)


def test_cancel_between_precheck_and_real_cached_admission(tmp_path, capture):
    tts = service(tmp_path)
    tts._cached_play_thread = Mock()
    checks = []

    def valid():
        checks.append(True)
        if len(checks) > 1:
            capture.cancelled.set()
            return False
        return True

    try:
        assert not emit(tts, capture, valid=valid)
        tts._cached_play_thread.assert_not_called()
        assert len(checks) == 2
    finally:
        tts._device_input_gate.close()


def test_new_tap_timestamp_suppresses_retry_owner(tmp_path, capture):
    from hal.drivers.voice.tts.turn_supersession import suppress_before
    tts = service(tmp_path)
    tts._owner_suppressed = TTSService._owner_suppressed
    tts._cached_play_thread = Mock()
    suppress_before(capture.snapshot["capturedAtMs"] + 1)
    try:
        assert not emit(tts, capture)
        tts._cached_play_thread.assert_not_called()
    finally:
        tts._device_input_gate.close()
