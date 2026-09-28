"""POST transcripts to os-server + drop transcripts that echo our own TTS.

`_is_echo` is the third layer of echo handling:
  Layer 1: temporal isolation (mic closed during TTS) — voice_service main loop
  Layer 2: adaptive RMS reverb gate                    — voice_service._wait_for_tts
  Layer 3: transcript similarity filter                — here
"""

import json as _json
import logging
import time
from difflib import SequenceMatcher

import requests

from hal.drivers.voice._internal.config import (
    ECHO_RELEVANCE_WINDOW_S,
    ECHO_SIMILARITY_THRESHOLD,
    OS_SENSING_URL,
)

logger = logging.getLogger("hal.voice")


class SendResult:
    """What os-server said about a forwarded turn.

    ``run_id`` correlates this utterance with the os-server run it created.
    ``speech_suppressed`` is os-server's ANSWER to "did you actually take the
    speaker away from the older turn" — only meaningful for
    voice_agent_handled, and False when the supersession policy is off. Both
    are for measurement; nothing in the voice path branches on them.
    """

    __slots__ = ("run_id", "speech_suppressed", "delivered", "handled_locally")

    def __init__(self, run_id: str = "", speech_suppressed: bool = False,
                 delivered: bool = False, handled_locally: bool = False):
        self.run_id = run_id
        self.speech_suppressed = speech_suppressed
        self.delivered = delivered
        # os-server answered the command itself (local intent match: volume,
        # LED, time). Served, just without an agent run — so a missing run id
        # here is success, not a failed dispatch.
        self.handled_locally = handled_locally

    def __bool__(self) -> bool:
        return self.delivered


def _result_of(resp) -> "SendResult":
    """Parse the os-server response. Never raises: a correlation id is nice to
    have, not a reason to fail a voice turn."""
    try:
        data = (resp.json() or {}).get("data") or {}
        return SendResult(
            run_id=data.get("runId", "") or "",
            speech_suppressed=bool(data.get("speechSuppressed", False)),
            delivered=True,
            handled_locally=str(data.get("handledLocally", "")).lower() == "true"
            or data.get("handler") == "local",
        )
    except Exception:
        return SendResult(delivered=True)


class SensingSender:
    """Send sensing events to os-server with retry + echo suppression."""

    def __init__(self, tts_service=None):
        self._tts = tts_service

    def is_echo(self, transcript: str) -> bool:
        """True if transcript matches our last TTS output above threshold."""
        if not self._tts or not self._tts.last_spoken_text:
            return False
        elapsed = time.time() - self._tts.last_spoken_time
        if elapsed > ECHO_RELEVANCE_WINDOW_S:
            return False
        similarity = SequenceMatcher(
            None, transcript.lower(), self._tts.last_spoken_text.lower(),
        ).ratio()
        if similarity >= ECHO_SIMILARITY_THRESHOLD:
            logger.info(
                "Echo detected (similarity=%.2f): '%s' ≈ TTS:'%s' — dropping",
                similarity, transcript[:60], self._tts.last_spoken_text[:60],
            )
            return True
        return False

    def send(
        self,
        message: str,
        event_type: str = "voice",
        skip_echo: bool = False,
        image_b64: str = "",
        interaction_id: str = "",
        harness_voice: dict | None = None,
        voice_turn_type: str = "",
    ) -> "SendResult":
        """POST decorated message to os-server /api/sensing/event with retry.

        ``image_b64`` (raw base64 JPEG, no data-URI prefix) rides the payload's
        ``images`` list. HAL only ever has ONE snapshot per event, so it sends a
        single-element list; the field is a list because a chat client can attach
        several photos to one turn and every wire format behind os-server already
        carries `attachments[]`. os-server describes it with a vision model and forwards
        the description as text (describe-first) — required because the main
        model can be text-only: a file path in ``message`` is useless there
        (tool-read image blocks are silently dropped), and a raw attachment
        404s at the smart-agent-router when it picks a no-vision backend.
        """
        if not skip_echo and self.is_echo(message):
            return SendResult()

        # Someone spoke to the device: keep presence from timing out to AWAY
        # (and the sleep announcement) while the camera cannot see them.
        if event_type in ("voice", "voice_command", "voice_followup", "voice_agent_handled"):
            try:
                from hal import app_state as presence_state

                presence_state.note_user_activity(event_type)
            except Exception:
                logger.exception("[voice] presence activity update failed")

        payload = {"type": event_type, "message": message}
        # Observational classification only; never replace the routing event.
        if voice_turn_type in ("voice", "voice_command", "voice_followup"):
            payload["voice_turn_type"] = voice_turn_type
        if harness_voice is not None:
            payload["harness_voice"] = {
                "enabled": harness_voice["enabled"],
                "generation": harness_voice["generation"],
            }
        if interaction_id:
            # Voice metrics ownership, sent UP so os-server can tag the audio it
            # starts on its own. The opening filler fires the moment this POST
            # arrives — before the response carrying runId gets back here — so
            # binding on the response would leave a cache-hit filler unowned
            # and its turn counted as unacknowledged.
            payload["interaction_id"] = interaction_id
        # Voice turns used to ship NO current_user at all, so the identity in
        # them reached os-server only as the `Speaker - <Name>:` text prefix and
        # the backend fell back to its last cached value. Send the resolved
        # identity like every sensing event does. Face still wins inside the
        # resolver, so on a camera device this is the same value those events
        # already carry — it only adds a user on the voice-only path.
        try:
            from hal import app_state as identity_state

            cu, _display, _source, _age = identity_state.resolve_current_user()
            if cu:
                payload["current_user"] = cu
        except Exception:
            logger.exception("[voice] current_user resolution failed")
        if image_b64:
            payload["images"] = [image_b64]
        # Log a copy with the image masked — a ~70KB base64 blob would drown the log.
        log_payload = {**payload, "images": [f"<{len(image_b64)} b64 chars>"]} if image_b64 else payload
        logger.info(
            "curl -s -X POST %s -H 'Content-Type: application/json' -d '%s'",
            OS_SENSING_URL, _json.dumps(log_payload),
        )
        # Image turns wait for the os-server-side vision describe (up to 80s,
        # system/vision DescribeTimeout — qwen measured 2026-07-06: scene
        # images 8-20s but TEXT-DENSE images 23-38s per attempt) before the
        # response comes back — don't let the plain-text timeout abort them
        # into a spurious "failed to send" warning.
        harness_only = bool(harness_voice and harness_voice["enabled"])
        user_voice = event_type in ("voice", "voice_command", "voice_followup")
        # Local/Jev handling can take 3s for classification and multiple 5s HAL
        # calls. Give it time to return a receipt; keep direct Harness unchanged.
        timeout_s = 90 if image_b64 else (30 if user_voice and not harness_only else 5)
        # A transport error can occur after Harness accepted the turn. Never
        # retry that mutation without a known receipt / idempotency outcome.
        max_retries = 1 if harness_only else 3
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(OS_SENSING_URL, json=payload, timeout=timeout_s)
                if resp.status_code == 503 and attempt < max_retries:
                    logger.warning(
                        "os-server agent not ready (503), retrying in 2s... (attempt %d/%d)",
                        attempt, max_retries,
                    )
                    time.sleep(2)
                    continue
                elif resp.status_code != 200:
                    logger.warning("os-server returned %d: %s", resp.status_code, resp.text)
                else:
                    logger.info("Sent to os-server: %r", message)
                    # Returned so a caller can correlate this turn with the
                    # os-server run it created (voice metrics does; nothing else
                    # has to care).
                    return _result_of(resp)
                return SendResult()
            except requests.ConnectionError as e:
                # A disconnect does not prove the POST was rejected. Relative
                # dim/volume commands must not be replayed after a lost receipt.
                # interaction_id is telemetry, not an idempotency guarantee.
                if user_voice:
                    logger.warning("Voice event delivery uncertain; not retrying: %s", e)
                    return SendResult()
                if attempt < max_retries:
                    logger.warning(
                        "os-server not reachable (attempt %d/%d), retrying in 2s...",
                        attempt, max_retries,
                    )
                    time.sleep(2)
                else:
                    logger.warning(
                        "Failed to send voice event to os-server after %d attempts: %s",
                        max_retries, e,
                    )
            except requests.RequestException as e:
                logger.warning("Failed to send voice event to os-server: %s", e)
                return SendResult()
        return SendResult()
