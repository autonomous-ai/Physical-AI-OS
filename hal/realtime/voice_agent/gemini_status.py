"""Preserve Gemini interaction status dropped by older google-genai converters."""

from contextvars import ContextVar
from collections import Counter
import json
import logging
import uuid

logger = logging.getLogger(__name__)


class _StatusWebSocket:
    """Forward the socket unchanged while observing each receive's own payload."""

    def __init__(self, socket, current_receive):
        self._socket = socket
        self._current_receive = current_receive
        self._trace_id = uuid.uuid4().hex[:12]
        self._receive_seq = 0
        self._endpoint_counts = Counter()

    def __getattr__(self, name):
        return getattr(self._socket, name)

    async def recv(self, *args, **kwargs):
        raw = await self._socket.recv(*args, **kwargs)
        self._receive_seq += 1
        capture = self._current_receive.get()
        if capture is not None:
            # Keep parsing and transport errors under the SDK's ownership.
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError, UnicodeError):
                payload = None
            content = payload.get("serverContent") if isinstance(payload, dict) else None
            if isinstance(payload, dict):
                capture["endpoint_wire"] = {
                    key: payload.get(key) for key in ("voiceActivity", "voiceActivityDetectionSignal")
                }
                capture["speech_state"] = content.get("speechState") if isinstance(content, dict) else None
            status = content.get("interactionStatus") if isinstance(content, dict) else None
            if isinstance(content, dict):
                transcription = content.get("outputTranscription")
                if isinstance(transcription, dict):
                    # Observe the wire before SDK conversion or HAL buffering.
                    # Never dump the full payload (audio/image bytes or tokens).
                    logger.info(
                        "[realtime][wire-output] session=%s rx=%d status=%s "
                        "generation_complete=%s turn_complete=%s interrupted=%s "
                        "finished=%s text=%r",
                        self._trace_id, self._receive_seq, status,
                        content.get("generationComplete", False),
                        content.get("turnComplete", False),
                        content.get("interrupted", False),
                        transcription.get("finished"), transcription.get("text", ""),
                    )
            capture["status"] = status if isinstance(status, str) else None
        return raw

    def trace_endpoint(self, capture, message):
        """Diagnose missing signals without changing SDK messages or VAD setup.

        Counts belong to this socket, never a user interaction. Only enums and
        offsets are logged; no audio, transcripts, credentials or full payloads.
        """
        wire = capture.get("endpoint_wire", {})
        activity = wire.get("voiceActivity")
        legacy = wire.get("voiceActivityDetectionSignal")
        parsed = getattr(message, "voice_activity", None)
        counts = self._endpoint_counts
        counts["messages"] += 1
        counts["wire_activity"] += int(isinstance(activity, dict))
        counts["sdk_activity"] += int(parsed is not None)
        counts["wire_legacy"] += int(isinstance(legacy, dict))
        counts["wire_speech_state"] += int(capture.get("speech_state") is not None)
        content = message.server_content
        counts["input_transcriptions"] += int(getattr(content, "input_transcription", None) is not None)
        if isinstance(activity, dict):
            logger.info(
                "[realtime][endpoint-wire] session=%s type=%s audio_offset=%s sdk_type=%s sdk_offset=%s",
                self._trace_id, activity.get("type"), activity.get("audioOffset"),
                getattr(parsed, "voice_activity_type", None), getattr(parsed, "audio_offset", None),
            )
        if (isinstance(activity, dict) or isinstance(legacy, dict)
                or capture.get("speech_state") is not None
                or getattr(content, "turn_complete", False)):
            logger.info("[realtime][endpoint-coverage] session=%s counts=%s",
                        self._trace_id, dict(counts))


def install_interaction_status(session):
    """Attach raw serverContent.interactionStatus to this session's parsed messages.

    google-genai 2.12.1 strips this field in its converter. Wrap only this live
    session, retaining the SDK's parsing, validation, and exception behavior.
    The context-local capture associates metadata with the exact receive call;
    missing fields never inherit status from a preceding message.
    """
    if isinstance(session._ws, _StatusWebSocket):
        return
    current_receive = ContextVar("gemini_interaction_status", default=None)
    original_receive = session._receive
    session._ws = _StatusWebSocket(session._ws, current_receive)

    async def receive_with_status():
        capture = {}
        token = current_receive.set(capture)
        try:
            message = await original_receive()
            try:
                session._ws.trace_endpoint(capture, message)
            except Exception:
                # Diagnostics must never abort a receive or alter playback.
                logger.exception("[realtime] endpoint observation failed")
            status = capture.get("status")
            if status is not None and message.server_content is not None:
                content = message.server_content.model_copy(
                    update={"interaction_status": status},
                )
                message = message.model_copy(update={"server_content": content})
            return message
        finally:
            current_receive.reset(token)

    session._receive = receive_with_status
