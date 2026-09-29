#!/usr/bin/env python3
"""Stop hook: push "Task Done" (and usage above threshold) to the device; rate-limited to 60s."""

import json
import os
import subprocess
import sys
import time

from buddy_client import (
    default_device, fetch_usage, get_token, load_config, send, time_left,
)

COOLDOWN_PATH = os.path.expanduser("~/.config/claude-code-buddy-done.last")
COOLDOWN_SECONDS = 60
DEFAULT_USAGE_THRESHOLD = 80
SECTION_DELAY = 5


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


def push_usage():
    """Fetch usage and push it if at/above threshold; runs in a detached child."""
    cfg = load_config()
    dev = default_device(cfg)
    if not dev:
        return

    token = get_token()
    if not token:
        return
    try:
        usage = fetch_usage(token)
    except Exception:
        return

    pct_5h = int(usage["five_hour"]["utilization"])
    pct_7d = int(usage["seven_day"]["utilization"])
    threshold = cfg.get("usage_threshold", DEFAULT_USAGE_THRESHOLD)
    if pct_5h < threshold and pct_7d < threshold:
        return

    time.sleep(SECTION_DELAY)
    send("/claude-code/usage", {
        "five_hour": pct_5h,
        "seven_day": pct_7d,
        "reset_5h": time_left(usage["five_hour"]["resets_at"]),
        "reset_7d": time_left(usage["seven_day"]["resets_at"]),
        "sound": cfg.get("sounds_enabled", True),
    }, dev)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--usage":
        push_usage()
        return

    try:
        json.load(sys.stdin)
    except Exception:
        pass

    if not should_run():
        return

    cfg = load_config()
    dev = default_device(cfg)
    if not dev:
        return

    if cfg.get("task_done_enabled", True):
        send("/claude-code/notify", {
            "title": "Task Done",
            "subtitle": "claude code",
            "level": "done",
            "sound": cfg.get("sounds_enabled", True),
        }, dev)

    mark_ran()

    # Detached child so the Stop hook doesn't block on the usage API.
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--usage"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
