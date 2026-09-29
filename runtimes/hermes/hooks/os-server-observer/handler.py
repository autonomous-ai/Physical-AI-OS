"""os-server-observer — forwards every Hermes gateway turn to os-server."""

import asyncio
import json
import urllib.request

OS_SERVER_TURN_URL = "__OS_SERVER_TURN_URL__"


def _post(body: bytes) -> None:
    req = urllib.request.Request(
        OS_SERVER_TURN_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=3).close()
    except Exception:
        # Best-effort: os-server unreachable / restarting must not affect the turn.
        pass


async def handle(event_type, context):
    try:
        body = json.dumps({"event": event_type, "context": context}).encode("utf-8")
    except (TypeError, ValueError):
        return
    # Run the blocking POST off the event loop so the agent pipeline never stalls.
    await asyncio.to_thread(_post, body)
