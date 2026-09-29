#!/usr/bin/env python3
"""Notification hook: ping the device when Claude needs the user (rate-limited to 8s)."""

import json
import os
import re
import sys
import time

from buddy_client import default_device, load_config, send

COOLDOWN_PATH = os.path.expanduser("~/.config/claude-code-buddy-notify.last")
COOLDOWN_SECONDS = 8

PERMISSION_RE = re.compile(r"permission to use (.+?)[.\s]*$", re.IGNORECASE)


def should_run():
    try:
        last = float(open(COOLDOWN_PATH).read().strip())
        return (time.time() - last) >= COOLDOWN_SECONDS
    except Exception:
        return True


def mark_ran():
    try:
        with open(COOLDOWN_PATH, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def pretty_tool(name):
    """Shorten a tool name, e.g. 'mcp__claude_ai_Google_Drive__authenticate' -> 'authenticate'."""
    name = name.strip()
    if name.startswith("mcp__"):
        name = name.split("__")[-1] or name
    return name.replace("_", " ").strip()


def parse_message(message):
    """Return (title, subtitle) for a notification message."""
    msg = (message or "").strip()
    low = msg.lower()
    m = PERMISSION_RE.search(msg)
    if m:
        return "Approve?", pretty_tool(m.group(1))
    if "permission" in low or "approve" in low or "allow" in low:
        return "Approve?", "needs your ok"
    if "waiting" in low or "idle" in low or "input" in low:
        return "Your turn", "waiting for you"
    return "Heads up", msg


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}

    if not should_run():
        sys.exit(0)

    cfg = load_config()
    if not cfg.get("notify_enabled", True):
        sys.exit(0)

    dev = default_device(cfg)
    if not dev:
        sys.exit(0)

    title, subtitle = parse_message(event.get("message", "Claude needs you"))

    # Cooldown applies whether or not the device is reachable.
    mark_ran()
    send("/claude-code/notify", {
        "title": title,
        "subtitle": subtitle,
        "level": "attention",
        "sound": cfg.get("sounds_enabled", True),
    }, dev)


if __name__ == "__main__":
    main()
