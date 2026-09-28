#!/usr/bin/env python3
"""Idempotently patch bluebubbles.py to subscribe ONLY to `new-message` events
(not `updated-message`) so each inbound message triggers exactly one LLM turn.

Root cause of duplicate replies: the plugin previously registered for both
`new-message` AND `updated-message`. BlueBubbles fires both events for a
single inbound message (the `updated-message` fires seconds later when
delivery / read state changes), and the webhook handler treats both as new
inbound messages — so the LLM runs twice, produces two independent replies,
and both land on the sender's iPhone. Observed on intern-v2 20.159:
`Thắng mấy bàn á` → 2 BLUEBUBBLES turns (5.5s and 20.0s) → 2 different
replies both delivered.

We do NOT touch `_MESSAGE_EVENTS` (the accept-set on the webhook handler)
because the plugin also uses `updated-message` for outbound bookkeeping in
scenarios BlueBubbles calls it back; subscribing off the wire is enough to
stop the extra fire from ever arriving here.

Isolated to bluebubbles.py — Telegram / Slack / Discord plugins do not
touch this code path."""
import re
import sys
from pathlib import Path

TARGET = Path("/usr/local/lib/hermes-agent/gateway/platforms/bluebubbles.py")
MARKER = "_WEBHOOK_DEDUP_APPLIED"

if not TARGET.exists():
    print(f"NOT_FOUND: {TARGET}", file=sys.stderr)
    sys.exit(2)
src = TARGET.read_text()

if MARKER in src:
    print("ALREADY_PATCHED")
    sys.exit(0)

# Match the events list in the webhook registration payload.
# Original: "events": ["new-message", "updated-message"],
# Use a standalone marker so inline closing braces remain executable.
new_src, n = re.subn(
    r'("events":\s*)\[\s*"new-message"\s*,\s*"updated-message"\s*\]',
    r'\1["new-message"]',
    src,
    count=1,
)
if n != 1:
    print("ANCHOR_NOT_FOUND — events-list layout differs from expected", file=sys.stderr)
    sys.exit(3)

new_src += '\n# ' + MARKER + '\n'
compile(new_src, str(TARGET), 'exec')

tmp = TARGET.with_suffix('.py.tmp3')
tmp.write_text(new_src)
tmp.replace(TARGET)
print("PATCHED")
