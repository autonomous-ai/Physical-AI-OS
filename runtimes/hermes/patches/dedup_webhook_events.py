#!/usr/bin/env python3
"""Idempotently patch bluebubbles.py to subscribe ONLY to `new-message` events (not `updated-message`) so each inbound message triggers exactly one LLM turn."""
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
