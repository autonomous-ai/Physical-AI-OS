#!/usr/bin/env python3
"""PermissionRequest hook: voice-approve Claude Code tool prompts on the device (opt-in).

Fail-safe: on any error or timeout, print nothing so Claude Code shows its native dialog.
"""

import json
import sys
import uuid

from buddy_client import default_device, load_config, request_approval

# Between the device long-poll ttl (55s) and the hooks.json timeout (60s).
REQUEST_TIMEOUT = 58


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}

    cfg = load_config()
    if not cfg.get("approval_enabled", False):
        return  # opt-in only

    dev = default_device(cfg)
    if not dev:
        return  # no device -> native dialog

    decision = request_approval({
        "id": str(uuid.uuid4()),
        "tool": event.get("tool_name", "?"),
        "input": event.get("tool_input", {}),
    }, dev, timeout=REQUEST_TIMEOUT)

    if decision not in ("allow", "deny"):
        return  # timeout / unreachable / error -> native dialog

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": decision},
        }
    }))


if __name__ == "__main__":
    main()
