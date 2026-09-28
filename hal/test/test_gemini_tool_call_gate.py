"""Regression tests for the pending-tool-call session quarantine (Gemini Live 1008).

Gemini refuses client input while a tool call it emitted is unanswered and
closes the session with 1008 ("The operation was aborted"). A tool response is
the only way to make that session reusable. Fire-and-forget tools deliberately
do not send one, so their session must be rebuilt before another capture.
"""

import asyncio
import queue
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from hal.realtime.models import (
    AudioInput,
    FunctionCallResultInput,
    ImageInput,
    TextInput,
)
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


class _RecordingSession:
    """Captures what actually reached the wire."""

    def __init__(self) -> None:
        self.realtime_inputs: list[dict] = []
        self.tool_responses: list[object] = []
        self.client_contents: list[dict] = []
        self.fail_tool_response = False

    async def send_realtime_input(self, **kwargs) -> None:
        self.realtime_inputs.append(kwargs)

    async def send_tool_response(self, function_responses) -> None:
        if self.fail_tool_response:
            raise RuntimeError("tool response send failed")
        self.tool_responses.append(function_responses)

    async def send_client_content(self, **kwargs) -> None:
        self.client_contents.append(kwargs)

    async def receive(self):
        for message in self._messages:
            yield message


def _agent(session: _RecordingSession) -> GeminiLiveAgent:
    agent = object.__new__(GeminiLiveAgent)
    agent._session = session
    agent._config = SimpleNamespace(sample_rate=16000)
    agent._vad_disabled = False
    agent._activity_started = False
    agent._last_audio_sent_at = None
    agent._activity_end_sent_at = None
    agent._pending_tool_calls = set()
    agent._gated_audio_frames = 0
    agent._turn_done = threading.Event()
    return agent


def _audio() -> AudioInput:
    return AudioInput(audio=np.zeros(160, dtype=np.float32))


def _frame() -> np.ndarray:
    return np.zeros((4, 4, 3), dtype=np.uint8)


def _tool_call_message(*call_ids: str) -> SimpleNamespace:
    return SimpleNamespace(
        usage_metadata=None,
        server_content=None,
        tool_call=SimpleNamespace(
            function_calls=[
                SimpleNamespace(name="express_emotion", args={}, id=call_id)
                for call_id in call_ids
            ]
        ),
        session_resumption_update=None,
        go_away=None,
    )


def test_audio_flows_when_no_tool_call_is_pending():
    session = _RecordingSession()
    agent = _agent(session)

    asyncio.run(agent._async_send_input(_audio()))

    assert len(session.realtime_inputs) == 1
    assert "audio" in session.realtime_inputs[0]


def test_receiving_a_tool_call_closes_the_audio_gate():
    session = _RecordingSession()
    agent = _agent(session)
    session._messages = [_tool_call_message("call-1")]
    agent._recv_queue = queue.Queue()
    agent._turn_done = threading.Event()
    agent._first_audio_received = False

    asyncio.run(agent._async_receive_turn())
    assert agent._pending_tool_calls == {"call-1"}

    asyncio.run(agent._async_send_input(_audio()))

    assert session.realtime_inputs == []
    assert agent._gated_audio_frames == 1


def test_fire_and_forget_result_requires_a_fresh_session():
    """Skipping Gemini's acknowledgement makes this socket permanently unsafe."""
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}

    asyncio.run(
        agent._async_send_input(
            FunctionCallResultInput(
                call_id="call-1", output='{"result": "expressed"}', trigger_response=False
            )
        )
    )

    # Gemini was deliberately told nothing, so the original call remains
    # unresolved from the server's point of view.
    assert session.tool_responses == []
    assert agent._pending_tool_calls == {"call-1"}
    assert agent.requires_fresh_session is True

    # No client input may touch a session Gemini is still waiting on.
    asyncio.run(agent._async_send_input(_audio()))
    asyncio.run(agent._async_send_input(TextInput(text="turn context")))
    assert session.realtime_inputs == []
    assert session.client_contents == []


def test_acked_result_reopens_the_gate_only_after_the_send_succeeds():
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}

    asyncio.run(
        agent._async_send_input(
            FunctionCallResultInput(
                call_id="call-1", output='{"result": "ok"}', trigger_response=True
            )
        )
    )

    assert len(session.tool_responses) == 1
    assert agent._pending_tool_calls == set()
    assert agent.requires_fresh_session is False


def test_failed_tool_response_keeps_the_session_non_reusable():
    session = _RecordingSession()
    session.fail_tool_response = True
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}

    with pytest.raises(RuntimeError, match="tool response send failed"):
        asyncio.run(
            agent._async_send_input(
                FunctionCallResultInput(
                    call_id="call-1", output='{"result": "ok"}', trigger_response=True
                )
            )
        )

    assert session.tool_responses == []
    assert agent._pending_tool_calls == {"call-1"}
    assert agent.requires_fresh_session is True


def test_gate_stays_closed_until_every_parallel_call_is_acknowledged():
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1", "call-2"}

    asyncio.run(
        agent._async_send_input(
            FunctionCallResultInput(
                call_id="call-1", output="{}", trigger_response=False
            )
        )
    )
    asyncio.run(agent._async_send_input(_audio()))
    assert session.realtime_inputs == []

    asyncio.run(
        agent._async_send_input(
            FunctionCallResultInput(call_id="call-2", output="{}", trigger_response=True)
        )
    )
    asyncio.run(agent._async_send_input(_audio()))
    assert session.realtime_inputs == []
    assert agent._pending_tool_calls == {"call-1"}
    assert agent.requires_fresh_session is True


def test_unresolved_tool_call_never_expires_into_sending_input():
    """Only rebuilding the session can recover an unanswered Gemini tool call."""
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-lost"}

    asyncio.run(agent._async_send_input(_audio()))
    asyncio.run(agent._async_send_input(TextInput(text="turn context")))

    assert session.realtime_inputs == []
    assert session.client_contents == []
    assert agent._pending_tool_calls == {"call-lost"}
    assert agent.requires_fresh_session is True


def test_reconnect_drops_a_gate_belonging_to_the_dead_session():
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}

    agent._clear_pending_tool_calls()

    assert agent._pending_tool_calls == set()
    asyncio.run(agent._async_send_input(_audio()))
    assert len(session.realtime_inputs) == 1


def test_activity_start_is_gated_with_the_audio_it_brackets():
    """Manual VAD: no activityStart for frames we are not going to send."""
    session = _RecordingSession()
    agent = _agent(session)
    agent._vad_disabled = True
    agent._pending_tool_calls = {"call-1"}

    asyncio.run(agent._async_send_input(_audio()))

    assert session.realtime_inputs == []
    assert agent._activity_started is False


def test_activity_end_is_suppressed_while_a_tool_call_is_pending():
    """Manual VAD must not emit activityEnd into an unresolved tool turn."""
    session = _RecordingSession()
    agent = _agent(session)
    agent._vad_disabled = True
    agent._activity_started = True
    agent._pending_tool_calls = {"call-1"}

    asyncio.run(agent._async_commit())

    assert session.realtime_inputs == []
    # The bracket belongs to the dying session, so do not carry it into its
    # replacement.
    assert agent._activity_started is False
    assert agent.requires_fresh_session is True


def test_an_image_is_dropped_while_a_tool_call_is_pending():
    """The gate is not audio-only: ImageInput rides send_realtime_input too.

    This is the `look` flow's frame. It shares the API Gemini refuses while a
    call is unanswered, so it must be gated for the same reason audio is —
    otherwise the session dies with 1008 instead of losing one frame.
    """
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}

    asyncio.run(agent._async_send_input(ImageInput(image=_frame())))

    assert session.realtime_inputs == []
    # Not audio, so it must not be counted as a gated audio frame.
    assert agent._gated_audio_frames == 0


def test_an_image_flows_once_the_tool_call_has_been_answered():
    """What the look flow depends on: ack first, then the frame goes out.

    The fresh-frame path used to send no result at all and relied on the gate's
    10s expiry outliving an 8s aim — flaky by construction, and broken outright
    once the expiry was removed. Acknowledging the call is what reopens
    send_realtime_input for both the frame and the replayed audio.
    """
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}

    asyncio.run(
        agent._async_send_input(
            FunctionCallResultInput(
                call_id="call-1",
                output='{"result": "frame incoming; wait for the image"}',
                trigger_response=True,
            )
        )
    )
    assert agent._pending_tool_calls == set()

    asyncio.run(agent._async_send_input(ImageInput(image=_frame())))
    assert len(session.realtime_inputs) == 1
    assert "video" in session.realtime_inputs[0]


def test_replayed_audio_flows_once_the_tool_call_has_been_answered():
    """The other half of the look flow: the user's utterance is sent twice.

    The replay re-appends the SAME mic frames so the queued image joins the
    question. Those are AudioInput, so an unanswered call silences the replay
    as surely as it drops the frame — the model then answers from nothing.
    """
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"call-1"}

    asyncio.run(agent._async_send_input(_audio()))
    assert session.realtime_inputs == []

    asyncio.run(
        agent._async_send_input(
            FunctionCallResultInput(call_id="call-1", output="{}", trigger_response=True)
        )
    )
    asyncio.run(agent._async_send_input(_audio()))

    assert len(session.realtime_inputs) == 1
    assert "audio" in session.realtime_inputs[0]


# --- The look ack tells the model whether the aim found the user ---


def _ack_payload(res):
    """The dict the orchestrator would send as the look tool's result."""
    import json as _json

    found_user = bool(res is None or getattr(res, "aimed", False)
                      or getattr(res, "reason", "") != "subject not found")
    ack = {"result": "frame incoming; wait for the image"}
    if not found_user:
        ack["found_user"] = False
        ack["note"] = "..."
    return _json.loads(_json.dumps(ack))


class _Aim:
    def __init__(self, aimed, reason):
        self.aimed = aimed
        self.reason = reason


def test_a_centred_aim_reports_nothing_unusual():
    assert "found_user" not in _ack_payload(_Aim(True, "centred on person"))


def test_running_out_of_time_still_counts_as_finding_them():
    """The person IS in frame on these exits — just not centred yet. Flagging
    them would make the model hedge on a perfectly good picture."""
    assert "found_user" not in _ack_payload(_Aim(False, "deadline"))
    assert "found_user" not in _ack_payload(_Aim(False, "max iterations"))


def test_finding_nobody_is_reported_to_the_model():
    """Without this the model cannot tell a framed shot from "wherever the
    camera happened to be pointing", and answers both with equal confidence."""
    ack = _ack_payload(_Aim(False, "subject not found"))
    assert ack["found_user"] is False


def test_a_disabled_aim_makes_no_claim_either_way():
    """LOOK_AIM_ENABLED off, or the aim raised — no aim ran, so nothing is
    known about framing and the model should not be told otherwise."""
    assert "found_user" not in _ack_payload(None)


@pytest.mark.parametrize("tool_name", ["look", "express_emotion", "delegate_to_main"])
def test_tool_response_preserves_provider_call_name(tool_name):
    session = _RecordingSession()
    agent = _agent(session)
    message = _tool_call_message("call-named")
    message.tool_call.function_calls[0].name = tool_name
    session._messages = [message]
    agent._recv_queue = queue.Queue()
    agent._first_audio_received = False
    asyncio.run(agent._async_receive_turn())

    asyncio.run(agent._async_send_input(FunctionCallResultInput(
        call_id="call-named", output='{"result": "done"}',
    )))

    response, = session.tool_responses[-1]
    assert response.id == "call-named"
    assert response.name == tool_name
    assert response.response == {"result": "done"}
    assert not agent._pending_tool_calls
    assert not agent._pending_tool_names


def test_tool_name_survives_failed_ack_and_is_cleared_on_session_reset():
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"look-1"}
    agent._pending_tool_names = {"look-1": "look"}
    session.fail_tool_response = True
    with pytest.raises(RuntimeError, match="tool response send failed"):
        asyncio.run(agent._async_send_input(FunctionCallResultInput(
            call_id="look-1", output='{"result": "frame incoming"}',
        )))
    assert agent._pending_tool_names == {"look-1": "look"}
    agent._clear_pending_tool_calls()
    assert agent._pending_tool_names == {}


def test_ack_keeps_other_pending_tool_names():
    session = _RecordingSession()
    agent = _agent(session)
    agent._pending_tool_calls = {"look-1", "emotion-2"}
    agent._pending_tool_names = {"look-1": "look", "emotion-2": "express_emotion"}
    asyncio.run(agent._async_send_input(FunctionCallResultInput(
        call_id="look-1", output='{"result": "frame incoming"}',
    )))
    assert agent._pending_tool_calls == {"emotion-2"}
    assert agent._pending_tool_names == {"emotion-2": "express_emotion"}


def test_async_look_frame_waits_for_sibling_tool_ack():
    session = _RecordingSession()
    agent = _agent(session)
    agent.supports_look_continuation = True
    agent._pending_tool_calls = {"emotion"}
    agent._pending_tool_names = {"emotion": "express_emotion"}
    asyncio.run(agent._async_send_input(ImageInput(image=_frame())))
    assert session.realtime_inputs == []
    asyncio.run(agent._async_send_input(FunctionCallResultInput(
        call_id="emotion", output='{"result": "ok"}',
    )))
    assert len(session.realtime_inputs) == 1
    assert session.realtime_inputs[0]["video"].mime_type == "image/jpeg"
    assert agent._pending_image is None


def test_pending_async_image_is_not_reused_after_reset():
    session = _RecordingSession()
    agent = _agent(session)
    agent.supports_look_continuation = True
    agent._pending_tool_calls = {"old"}
    asyncio.run(agent._async_send_input(ImageInput(image=_frame())))
    agent._clear_pending_tool_calls()
    asyncio.run(agent._finish_tool_ack("old"))
    assert session.realtime_inputs == []


def _look_image_agent():
    from unittest.mock import AsyncMock
    session = _RecordingSession()
    session._ws = SimpleNamespace(send=AsyncMock())
    agent = _agent(session)
    agent.supports_look_continuation = True
    agent._pending_tool_calls = {"look-1", "emotion-1"}
    agent._pending_tool_names = {"look-1": "look", "emotion-1": "express_emotion"}
    return agent, session


def test_look_image_is_one_multimodal_tool_response():
    import base64
    import json
    import cv2
    agent, session = _look_image_agent()
    frame = _frame()
    frame[:] = (10, 80, 180)
    asyncio.run(agent._async_send_input(FunctionCallResultInput(
        call_id="look-1", output='{"result":"attached"}', image=frame,
    )))
    wire = json.loads(session._ws.send.call_args.args[0])
    response, = wire["toolResponse"]["functionResponses"]
    assert response["id"] == "look-1"
    assert response["name"] == "look"
    assert response["response"] == {"result": "attached"}
    data = response["parts"][0]["inlineData"]
    assert data["mimeType"] == "image/jpeg"
    decoded = cv2.imdecode(np.frombuffer(base64.b64decode(data["data"]), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == frame.shape
    assert np.max(np.abs(decoded.astype(int) - frame.astype(int))) <= 3
    assert agent._pending_tool_calls == {"emotion-1"}
    assert not session.realtime_inputs and not session.client_contents and not session.tool_responses


@pytest.mark.parametrize("case", ["cancelled", "reset", "resolved", "wrong_tool", "legacy"])
def test_stale_or_unsupported_look_result_sends_no_image(case):
    agent, session = _look_image_agent()
    if case == "cancelled":
        agent._invalidate_look_images(["look-1"])
    elif case == "reset":
        agent._clear_pending_tool_calls()
    elif case == "resolved":
        asyncio.run(agent._finish_tool_ack("look-1"))
    elif case == "wrong_tool":
        agent._pending_tool_names["look-1"] = "delegate_to_main"
    else:
        agent.supports_look_continuation = False
    asyncio.run(agent._async_send_input(FunctionCallResultInput(
        call_id="look-1", output='{"result":"attached"}', image=_frame(),
    )))
    session._ws.send.assert_not_called()
    assert not session.tool_responses and not session.realtime_inputs


@pytest.mark.parametrize("failure", ["encode", "send"])
def test_look_image_failure_keeps_call_pending(monkeypatch, failure):
    agent, session = _look_image_agent()
    if failure == "encode":
        monkeypatch.setattr("hal.realtime.voice_agent.gemini_live.cv2.imencode", lambda *_: (False, None))
    else:
        session._ws.send.side_effect = RuntimeError("send failed")
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(agent._async_send_input(FunctionCallResultInput(
            call_id="look-1", output='{"result":"attached"}', image=_frame(),
        )))
    assert "look-1" in agent._pending_tool_calls
    assert not session.tool_responses


def test_look_send_does_not_clear_replacement_session_pending_calls():
    agent, old_session = _look_image_agent()
    replacement = _RecordingSession()

    async def reconnect_during_send(payload):
        agent._session = replacement
        agent._pending_tool_calls = {"look-1"}
        agent._pending_tool_names = {"look-1": "look"}

    old_session._ws.send.side_effect = reconnect_during_send
    asyncio.run(agent._async_send_input(FunctionCallResultInput(
        call_id="look-1", output='{"result":"attached"}', image=_frame(),
    )))
    assert agent._session is replacement
    assert agent._pending_tool_calls == {"look-1"}
