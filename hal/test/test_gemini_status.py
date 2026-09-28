"""Exercise status preservation through the real SDK converter, not a mock parser."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from google.genai.live import AsyncSession

from hal.realtime.voice_agent.gemini_status import install_interaction_status


class Socket:
    def __init__(self, *messages):
        self.messages = iter(messages)
        self.sent = []

    async def recv(self, **kwargs):
        message = next(self.messages)
        if isinstance(message, BaseException):
            raise message
        return message

    async def send(self, value):
        self.sent.append(value)


def session_for(*messages):
    return AsyncSession(SimpleNamespace(vertexai=False), Socket(*messages))


@pytest.mark.parametrize("status", ["IN_PROGRESS", "IDLE"])
def test_status_survives_real_sdk_parser_with_known_fields(status):
    async def run():
        payload = json.dumps({"serverContent": {
            "interactionStatus": status,
            "turnComplete": True,
            "outputTranscription": {"text": "A blue shirt."},
        }}).encode()
        session = session_for(payload)
        install_interaction_status(session)
        message = await session._receive()
        assert message.server_content.interaction_status == status
        assert message.server_content.turn_complete is True
        assert message.server_content.output_transcription.text == "A blue shirt."
    asyncio.run(run())


def test_status_only_message_and_missing_status_do_not_leak():
    async def run():
        session = session_for(
            '{"serverContent":{"interactionStatus":"IN_PROGRESS"}}',
            '{"serverContent":{"turnComplete":true}}',
            '{"toolCall":{"functionCalls":[{"id":"x","name":"look","args":{}}]}}',
            '{"serverContent":{"interactionStatus":"IDLE"}}',
        )
        install_interaction_status(session)
        assert (await session._receive()).server_content.interaction_status == "IN_PROGRESS"
        assert getattr((await session._receive()).server_content, "interaction_status", None) is None
        message = await session._receive()
        assert message.server_content is None
        assert message.tool_call.function_calls[0].name == "look"
        assert (await session._receive()).server_content.interaction_status == "IDLE"
    asyncio.run(run())


@pytest.mark.parametrize("raw", ["invalid JSON", RuntimeError("socket failed"), asyncio.CancelledError()])
def test_errors_and_cancellation_stay_under_sdk_ownership(raw):
    async def run():
        expected = ValueError if isinstance(raw, str) else type(raw)
        session = session_for(raw, '{"serverContent":{"turnComplete":true}}')
        install_interaction_status(session)
        with pytest.raises(expected) as caught:
            await session._receive()
        if not isinstance(raw, str):
            assert caught.value is raw
        message = await session._receive()
        assert getattr(message.server_content, "interaction_status", None) is None
    asyncio.run(run())


def test_install_is_instance_local_idempotent_and_forwards_socket_methods():
    async def run():
        raw = '{"serverContent":{"interactionStatus":"IDLE"}}'
        session = session_for(raw)
        unwrapped = session_for(raw)
        socket = session._ws
        install_interaction_status(session)
        wrapped_receive = session._receive
        install_interaction_status(session)
        assert session._receive is wrapped_receive
        await session._ws.send("request")
        assert socket.sent == ["request"]
        assert (await session._receive()).server_content.interaction_status == "IDLE"
        assert getattr((await unwrapped._receive()).server_content, "interaction_status", None) is None
    asyncio.run(run())


def test_socket_without_decode_keyword_uses_sdk_fallback():
    class LegacySocket:
        async def recv(self):
            return '{"serverContent":{"interactionStatus":"IDLE"}}'

    async def run():
        session = AsyncSession(SimpleNamespace(vertexai=False), LegacySocket())
        install_interaction_status(session)
        assert (await session._receive()).server_content.interaction_status == "IDLE"
    asyncio.run(run())


def test_overlapping_receive_completion_keeps_status_with_its_message():
    async def run():
        first_parsed = asyncio.Event()
        release_first = asyncio.Event()

        class DelayedSession(AsyncSession):
            async def _receive(self):
                message = await super()._receive()
                if message.server_content.output_transcription.text == "first":
                    first_parsed.set()
                    await release_first.wait()
                return message

        socket = Socket(
            '{"serverContent":{"interactionStatus":"IN_PROGRESS","outputTranscription":{"text":"first"}}}',
            '{"serverContent":{"interactionStatus":"IDLE","outputTranscription":{"text":"second"}}}',
        )
        session = DelayedSession(SimpleNamespace(vertexai=False), socket)
        install_interaction_status(session)
        first = asyncio.create_task(session._receive())
        await first_parsed.wait()
        second = await session._receive()
        release_first.set()
        first_result = await first
        assert first_result.server_content.interaction_status == "IN_PROGRESS"
        assert second.server_content.interaction_status == "IDLE"

    asyncio.run(run())


def test_endpoint_fields_survive_real_sdk_and_missing_signal_is_counted(caplog):
    async def run():
        session = session_for(
            '{"voiceActivity":{"type":"ACTIVITY_END","audioOffset":"1.250s"}}',
            '{"serverContent":{"inputTranscription":{"text":"hello"},"turnComplete":true}}',
        )
        install_interaction_status(session)
        with caplog.at_level("INFO"):
            activity = (await session._receive()).voice_activity
            assert activity.voice_activity_type == "ACTIVITY_END"
            assert activity.audio_offset == "1.250s"
            assert (await session._receive()).voice_activity is None
        assert session._ws._endpoint_counts == {
            "messages": 2, "wire_activity": 1, "sdk_activity": 1,
            "wire_legacy": 0, "wire_speech_state": 0, "input_transcriptions": 1,
        }
        assert "[endpoint-wire]" in caplog.text
        assert "[endpoint-coverage]" in caplog.text
        assert "hello" not in caplog.text
    asyncio.run(run())


def test_endpoint_diagnostic_failure_cannot_break_receive(monkeypatch):
    async def run():
        session = session_for('{"serverContent":{"turnComplete":true}}')
        install_interaction_status(session)
        def broken(*args):
            raise RuntimeError("observer failed")
        monkeypatch.setattr(session._ws, "trace_endpoint", broken)
        assert (await session._receive()).server_content.turn_complete
    asyncio.run(run())
