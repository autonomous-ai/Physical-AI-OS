"""Pauses are provisional; speech and revised text revoke old decisions."""

from unittest.mock import Mock

import pytest

from hal.drivers.voice._internal.turn_endpoint import TurnEndpoint, needs_more_time


@pytest.mark.parametrize("text", [
    "do task abc, uhm...", "and...", "send the report but", "uh", "hmm",
    "làm việc đó và...", "ừm…", "để tôi nghĩ", "mở đèn rồi",
])
def test_hesitation_hints(text):
    assert needs_more_time(text)


@pytest.mark.parametrize("text", ["turn on the lamp", "bật đèn", "thank you", "Hùng"])
def test_complete_text_is_not_a_filler(text):
    assert not needs_more_time(text)


def decision(endpoint, silence, text="turn on the lamp", speech=10.0, final_at=0.0):
    return endpoint.should_close(
        now=speech + silence, last_speech=speech, text=text, pcm=b"\x00\x00", final_at=final_at,
    )


def model(result):
    return Mock(failed=False, submit=Mock(return_value=True), poll=Mock(return_value=result))


def test_complete_model_can_close_at_candidate_without_fallback_delay():
    endpoint = TurnEndpoint(model(True))
    assert decision(endpoint, 0.9)
    assert endpoint.reason == "smart_turn"


def test_incomplete_model_keeps_one_request_and_waits_to_bound():
    detector = model(False)
    endpoint = TurnEndpoint(detector)
    assert not decision(endpoint, 0.9)
    assert not decision(endpoint, 3)
    assert not decision(endpoint, 5.9)
    detector.submit.assert_called_once()
    detector.poll.assert_called_once()
    assert decision(endpoint, 6)
    assert endpoint.reason == "turn_pause_limit"


def test_final_with_unchanged_text_refreshes_incomplete_prediction():
    detector = model(False)
    endpoint = TurnEndpoint(detector)
    assert not decision(endpoint, 1)
    old_token = detector.submit.call_args.args[0]
    detector.poll.return_value = True
    assert decision(endpoint, 2, final_at=11.5)
    assert detector.submit.call_args.args[0] != old_token
    assert endpoint.reason == "smart_turn"


def test_final_refresh_does_not_reuse_complete_result_or_retry_every_frame():
    detector = model(True)
    endpoint = TurnEndpoint(detector)
    assert not decision(endpoint, 1, "Hello lamp.")
    detector.poll.return_value = False
    assert not decision(endpoint, 3, "Hello lamp.", final_at=12)
    assert not decision(endpoint, 4, "Hello lamp.", final_at=12)
    assert detector.submit.call_count == 2
    assert detector.poll.call_count == 2
    assert decision(endpoint, 6, "Hello lamp.", final_at=12)
    assert endpoint.reason == "turn_pause_limit"


def test_final_refresh_cannot_bypass_hesitation_guard():
    detector = model(False)
    endpoint = TurnEndpoint(detector)
    assert not decision(endpoint, 1, "Please check and um...")
    detector.poll.return_value = True
    assert not decision(endpoint, 3, "Please check and um...", final_at=12)
    assert decision(endpoint, 6, "Please check and um...", final_at=12)


def test_final_refresh_stalled_inference_retains_bounded_fallback():
    detector = model(False)
    endpoint = TurnEndpoint(detector)
    assert not decision(endpoint, 1)
    detector.poll.return_value = None
    assert not decision(endpoint, 3, final_at=12)
    assert decision(endpoint, 3.6, final_at=12)
    assert endpoint.reason == "turn_fallback"


def test_filler_overrides_complete_prediction_but_is_bounded():
    endpoint = TurnEndpoint(model(True))
    assert not decision(endpoint, 3, "send the report and um...")
    assert decision(endpoint, 6, "send the report and um...")


def test_greeting_leaves_room_for_the_request():
    endpoint = TurnEndpoint(model(True))
    assert not decision(endpoint, 0.9, "Hello lamp.")
    assert decision(endpoint, 2.5, "Hello lamp.")


@pytest.mark.parametrize("detector", [None, model(None), Mock(failed=True)])
def test_missing_failed_or_stalled_model_uses_conservative_fallback(detector):
    endpoint = TurnEndpoint(detector)
    assert not decision(endpoint, 1.2)
    assert decision(endpoint, 2.5)
    assert endpoint.reason == "turn_fallback"


def test_resumed_speech_revokes_complete_prediction():
    detector = model(True)
    endpoint = TurnEndpoint(detector)
    assert not decision(endpoint, 0.9, "Hello lamp.")
    first_token = detector.submit.call_args.args[0]
    detector.poll.return_value = None
    assert not decision(endpoint, 0.9, "Hello lamp. Send a report", speech=12)
    assert detector.submit.call_args.args[0] != first_token


def test_revised_transcript_revokes_prediction_without_new_vad():
    detector = model(False)
    endpoint = TurnEndpoint(detector)
    assert not decision(endpoint, 1)
    first_token = detector.submit.call_args.args[0]
    detector.poll.return_value = True
    assert decision(endpoint, 1.5, "turn off the lamp")
    assert detector.submit.call_args.args[0] != first_token


def test_previous_capture_cannot_authorize_new_capture():
    detector = model(None)
    decision(TurnEndpoint(detector), 1)
    first_token = detector.submit.call_args.args[0]
    decision(TurnEndpoint(detector), 1)
    assert detector.submit.call_args.args[0] != first_token


def test_busy_worker_can_be_retried_without_closing_early():
    detector = model(None)
    detector.submit.side_effect = [False, True]
    endpoint = TurnEndpoint(detector)
    assert not decision(endpoint, 0.9)
    assert not decision(endpoint, 1)
    assert detector.submit.call_count == 2


def test_late_candidate_waits_for_model_before_falling_back():
    detector = model(None)
    endpoint = TurnEndpoint(detector)
    assert not decision(endpoint, 2.6)
    detector.poll.return_value = False
    assert not decision(endpoint, 2.8)
    assert not decision(endpoint, 3.2)


def test_late_candidate_stalled_model_has_bounded_grace():
    endpoint = TurnEndpoint(model(None))
    assert not decision(endpoint, 2.6)
    assert decision(endpoint, 3.2)


def test_empty_noise_uses_existing_clock_without_model():
    detector = model(None)
    endpoint = TurnEndpoint(detector)
    assert decision(endpoint, 1.2, "...")
    detector.submit.assert_not_called()


def test_maximum_pause_cannot_be_shorter_than_fallback():
    endpoint = TurnEndpoint(model(False), fallback_s=3, max_pause_s=1)
    assert not decision(endpoint, 2)
    assert decision(endpoint, 3)
