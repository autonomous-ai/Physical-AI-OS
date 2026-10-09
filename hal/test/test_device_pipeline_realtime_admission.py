"""Buffered realtime has the same final say as the streaming device path."""

from unittest.mock import Mock

import pytest

from hal.drivers.voice._internal import device_voice_pipeline as module
from hal.drivers.voice._internal.harness_capture import Capture
from hal.test.test_device_voice_pipeline import setup_pipeline  # noqa: F401


def completed_turn(text):
    capture = Capture(dict(enabled=False, generation=1, deviceInputMode="tap_to_talk",
                           capturedAtMs=1791450223261))
    turn = module._RecordedTurn(capture)
    turn.audio = [b"\x01\x00" * 1024] * 32
    turn.byte_count = sum(map(len, turn.audio))
    turn.last_speech_idx = 20
    turn.finals = [text] if text else []
    turn.upload_ok = True
    turn.finished_at = 1.0
    turn.capture_done.set()
    turn.upload_done.set()
    turn.realtime_done.set()
    return turn


@pytest.mark.parametrize("route", ["ai_rejected", "noise_dropped", "realtime_cancelled",
                                  "foreign_dropped"])
def test_buffered_realtime_verdict_precedes_speaker_persistence(setup_pipeline, monkeypatch, route):
    setup = setup_pipeline
    monkeypatch.setattr(module.hal_config, "REALTIME_AI_REJECT_FILTER", True)
    turn = completed_turn("Please turn on the light")
    decisions = []
    setup.pipeline.decorator.recognize_speaker = lambda audio, accept: decisions.append(accept())
    setup.pipeline.decorator.decorate = Mock()
    setup.pipeline.realtime_turn = Mock(return_value=module.RealtimeTurnResult(
        route=route, rejected=route == "ai_rejected",
    ))
    try:
        setup.pipeline._finalize(turn)
    finally:
        setup.pipeline._cleanup(turn)
    assert decisions == [False]
    setup.pipeline.decorator.decorate.assert_not_called()
    assert setup.pipeline.realtime_turn.call_count == 1


def test_buffered_realtime_can_handle_empty_stt_without_retry(setup_pipeline, monkeypatch):
    setup = setup_pipeline
    monkeypatch.setattr(module.hal_config, "REALTIME_REQUIRE_TRANSCRIPT", True)
    turn = completed_turn("")
    setup.pipeline.tts.speak_cached = Mock(return_value=True)
    setup.pipeline.realtime_turn = Mock(return_value=module.RealtimeTurnResult(
        route="realtime_handled", handled=True,
    ))
    try:
        setup.pipeline._finalize(turn)
    finally:
        setup.pipeline._cleanup(turn)
    assert setup.pipeline.realtime_turn.call_count == 1
    assert setup.pipeline.realtime_turn.call_args.kwargs["combined"] == ""
    setup.pipeline.tts.speak_cached.assert_not_called()


def test_finalizing_partial_does_not_duplicate_realtime_snapshot_text(setup_pipeline):
    turn = completed_turn("")
    turn.partial = ["Turn on the light"]
    setup_pipeline.pipeline._finalize(turn)
    try:
        assert turn.finals == []
        assert turn.partial == ["Turn on the light"]
    finally:
        setup_pipeline.pipeline._cleanup(turn)
