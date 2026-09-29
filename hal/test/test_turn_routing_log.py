"""Every turn logs where it went and why — not just the delegated ones."""

import logging

import pytest

from hal.drivers.voice._internal.realtime_turn import (
    ROUTE_DELEGATED,
    ROUTE_HANDLED,
    ROUTE_NO_OUTPUT,
    ROUTE_NOISE_DROPPED,
    RealtimeTurnResult,
)
from hal.drivers.voice._internal.turn_dispatch import dispatch_turn


class _Decorator:
    def classify_wake_word(self, combined):
        return combined, "voice"

    def identify_and_decorate(self, final_text, audio_buffer):
        return final_text, "leo", "Leo"

    def submit_speech_emotion_from_session(self, buf, user=None):
        pass


class _Sender:
    def __init__(self):
        self.sent = []

    def send(self, msg, event_type="", skip_echo=False, image_b64="", **_kwargs):
        # **_kwargs absorbs measurement-only fields.
        self.sent.append((msg, event_type))


def _dispatch(rt, combined, caplog, event_type_override=None, interaction_id=""):
    with caplog.at_level(logging.INFO, logger="hal.voice"):
        dispatch_turn(
            _Decorator(),
            _Sender(),
            combined,
            [],
            [],
            rt,
            event_type_override=event_type_override,
            interaction_id=interaction_id,
        )
    return [r.getMessage() for r in caplog.records if "[turn] route=" in r.getMessage()]


def test_a_turn_the_main_agent_answers_names_its_route(caplog):
    lines = _dispatch(
        RealtimeTurnResult(route=ROUTE_NO_OUTPUT), "research my email", caplog
    )
    assert len(lines) == 1
    assert ROUTE_NO_OUTPUT in lines[0]
    assert "main agent" in lines[0]


def test_route_log_identifies_the_utterance_for_delayed_telemetry(caplog):
    lines = _dispatch(
        RealtimeTurnResult(route=ROUTE_NO_OUTPUT), "hello lamp", caplog,
        interaction_id="vi-current-utterance",
    )
    assert "interaction_id=vi-current-utterance" in lines[0]


def test_delegated_turn_is_labelled_as_such(caplog):
    lines = _dispatch(
        RealtimeTurnResult(delegated=True, route=ROUTE_DELEGATED), "turn on the light", caplog
    )
    assert ROUTE_DELEGATED in lines[0]
    assert "main agent" in lines[0]


def test_delegated_tool_call_reaches_main_without_local_transcript(caplog):
    """A tool call is usable even when local STT has not finalized yet."""
    sender = _Sender()
    with caplog.at_level(logging.INFO, logger="hal.voice"):
        dispatch_turn(
            _Decorator(),
            sender,
            "",
            [],
            [],
            RealtimeTurnResult(
                delegated=True,
                delegate_msg="Ask agent temp in Harness for restaurants in Hanoi",
                route=ROUTE_DELEGATED,
            ),
        )

    assert sender.sent == [
        ("[voice-instruction] Ask agent temp in Harness for restaurants in Hanoi", "voice")
    ]
    assert any("main agent" in record.getMessage() for record in caplog.records)


def test_handled_turn_does_not_claim_the_main_agent_answers(caplog):
    lines = _dispatch(
        RealtimeTurnResult(handled=True, transcript="sure", route=ROUTE_HANDLED),
        "hello there",
        caplog,
    )
    assert ROUTE_HANDLED in lines[0]
    assert "main agent" not in lines[0].split("→")[1].split("(")[0]


def test_dropped_turn_reports_that_it_reached_nobody(caplog):
    lines = _dispatch(RealtimeTurnResult(route=ROUTE_NOISE_DROPPED), "", caplog)
    assert ROUTE_NOISE_DROPPED in lines[0]
    assert "nowhere" in lines[0]


def test_route_defaults_so_an_old_style_result_still_logs(caplog):
    lines = _dispatch(RealtimeTurnResult(), "hello", caplog)
    assert len(lines) == 1


@pytest.mark.parametrize("override", ["voice_followup", None])
def test_event_type_travels_with_the_route(caplog, override):
    lines = _dispatch(
        RealtimeTurnResult(route=ROUTE_NO_OUTPUT),
        "research my email",
        caplog,
        event_type_override=override,
    )
    assert f"event={override or 'voice'}" in lines[0]


class _LocalResult:
    """os-server response for a local intent match: 200, spoken reply, no run id."""

    run_id = ""
    speech_suppressed = False
    delivered = True
    handled_locally = True


def test_locally_handled_command_is_not_marked_as_failed(monkeypatch):
    """A local intent reply without run id is not a failed dispatch."""
    from hal.drivers.voice._internal import turn_dispatch

    failed = []
    monkeypatch.setattr(turn_dispatch.voice_metrics, "mark_failed",
                        lambda iid, reason: failed.append((iid, reason)))

    turn_dispatch._note_dispatch_outcome("vi-1", _LocalResult())
    assert failed == []


def test_undelivered_command_is_still_marked_as_failed(monkeypatch):
    from hal.drivers.voice._internal import turn_dispatch

    failed = []
    monkeypatch.setattr(turn_dispatch.voice_metrics, "mark_failed",
                        lambda iid, reason: failed.append((iid, reason)))

    turn_dispatch._note_dispatch_outcome("vi-2", turn_dispatch._NoResult)
    assert failed == [("vi-2", turn_dispatch.voice_metrics.FAIL_DISPATCH_FAILED)]


def _sent_message(rt, combined):
    sender = _Sender()
    dispatch_turn(_Decorator(), sender, combined, [], [], rt)
    assert len(sender.sent) == 1, sender.sent
    return sender.sent[0][0]


def test_the_transcript_survives_next_to_the_paraphrase():
    """The delegate text keeps the user's own words."""
    msg = _sent_message(
        RealtimeTurnResult(
            delegated=True,
            delegate_msg="movement demonstration with rotation and tilting",
            route=ROUTE_DELEGATED,
        ),
        "show me your maximum capability",
    )
    assert msg.startswith("[voice-instruction] movement demonstration"), msg
    assert "\n[transcript] show me your maximum capability" in msg, (
        "the trigger vocabulary was dropped in favour of the paraphrase")


def test_the_paraphrase_alone_is_forwarded_when_there_is_no_transcript():
    """Only the paraphrase is forwarded when no transcript exists yet."""
    msg = _sent_message(
        RealtimeTurnResult(delegated=True, delegate_msg="find my keyboard",
                           route=ROUTE_DELEGATED),
        "",
    )
    assert msg == "[voice-instruction] find my keyboard"


def test_the_transcript_alone_is_forwarded_when_the_delegate_said_nothing():
    """device-chat-16: no preamble, the raw words reached the skill and matched."""
    msg = _sent_message(
        RealtimeTurnResult(delegated=True, delegate_msg="", route=ROUTE_DELEGATED),
        "Maximum capability",
    )
    assert msg == "Maximum capability"
