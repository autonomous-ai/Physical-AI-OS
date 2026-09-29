#!/usr/bin/env python3
"""os-server-observer — PicoClaw process hook forwarding channel turns to os-server.

Speaks NDJSON JSON-RPC over stdio; every request is answered with "continue" at once.
"""

import datetime
import json
import os
import sys
import threading
import urllib.request

OS_SERVER_TURN_URL = "__OS_SERVER_TURN_URL__"
DEBUG = os.environ.get("OBSERVER_DEBUG") == "1"
HOOK_LOG = os.environ.get("OBSERVER_LOG", "/root/.picoclaw/logs/messages_hooks.log")
# Optional channel allowlist; empty forwards every channel.
FORWARD_CHANNELS = {
    c.strip().lower()
    for c in os.environ.get("OBSERVER_CHANNELS", "").split(",")
    if c.strip()
}
# Internal senders riding a real channel (e.g. heartbeat) — never forwarded.
SKIP_SENDERS = {
    s.strip()
    for s in os.environ.get("OBSERVER_SKIP_SENDERS", "heartbeat").split(",")
    if s.strip()
}


def _debug(*a):
    if DEBUG:
        print("[os-server-observer]", *a, file=sys.stderr, flush=True)


def _log_json(record):
    """Append one JSON line to HOOK_LOG (best-effort, independent of the POST)."""
    try:
        os.makedirs(os.path.dirname(HOOK_LOG), exist_ok=True)
        with open(HOOK_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        _debug("log write failed:", e)


def _send(payload):
    try:
        req = urllib.request.Request(
            OS_SERVER_TURN_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3).close()
    except Exception as e:  # noqa: BLE001 — best-effort; os-server down must not affect the turn
        _debug("post failed:", e)


def _post(event, ctx):
    payload = {"event": event, "context": ctx}
    _log_json({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), **payload})
    # POST off the stdin loop: observer_timeout_ms is only 500ms.
    threading.Thread(target=_send, args=(payload,), daemon=True).start()


def _ctx(scope, message="", response=""):
    """Map a PicoClaw runtime-event scope → the ChannelTurn payload context."""
    # Default platform "" so os-server's skipPlatform drops channel-less events.
    return {
        "platform": str(scope.get("channel") or "").lower(),
        "user_id": str(scope.get("sender_id") or ""),
        "chat_id": str(scope.get("chat_id") or ""),
        "session_id": str(scope.get("session_key") or ""),
        "message": message,
        "response": response,
    }


def handle(msg):
    if not isinstance(msg, dict):
        return

    # Critical path: answer every request immediately so picoclaw never blocks.
    if msg.get("id") is not None:
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"action": "continue"}})
            + "\n"
        )
        sys.stdout.flush()

    if msg.get("method") != "hook.runtime_event":
        return
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return

    kind = str(params.get("kind") or "")
    scope = params.get("scope") or {}
    payload = params.get("payload") or {}
    channel = str(scope.get("channel") or "").lower()
    sender = str(scope.get("sender_id") or "")

    if FORWARD_CHANNELS and channel not in FORWARD_CHANNELS:
        return
    if sender in SKIP_SENDERS or str(scope.get("session_key") or "") in SKIP_SENDERS:
        return

    if kind.endswith("turn.start"):
        _post("agent:start", _ctx(scope, message=str(payload.get("UserMessage") or "")))
    elif kind.endswith("turn.end"):
        _post("agent:end", _ctx(scope, response=str(payload.get("FinalContent") or "")))


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        _debug("recv:", line[:2000])
        try:
            msg = json.loads(line)
        except Exception as e:  # noqa: BLE001
            _debug("json error:", e)
            continue
        try:
            handle(msg)
        except Exception as e:  # noqa: BLE001 — one bad event must not kill the hook
            _debug("handle error:", e)


if __name__ == "__main__":
    main()
