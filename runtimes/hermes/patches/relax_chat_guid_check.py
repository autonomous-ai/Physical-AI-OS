#!/usr/bin/env python3
"""Relax the iMessage-only filter to check ONLY the payload `service` field, not the chat GUID prefix."""
import re
import sys
from pathlib import Path

TARGET = Path("/usr/local/lib/hermes-agent/gateway/platforms/bluebubbles.py")
MARKER = "_IMESSAGE_ONLY_FILTER_RELAXED"

if not TARGET.exists():
    print("NOT_FOUND", file=sys.stderr)
    sys.exit(2)

src = TARGET.read_text()
if MARKER in src:
    print("ALREADY_RELAXED")
    sys.exit(0)

prefix_block_re = re.compile(
    r'        _chat_guid_probe = \(\n[\s\S]*?\n        if _chat_guid_probe and not _chat_guid_probe\.startswith\("imessage;"\):\n            return web\.Response\(text="ok"\)\n',
    re.MULTILINE,
)
if not prefix_block_re.search(src):
    print("PREFIX_BLOCK_NOT_FOUND — layout differs; skipping relax", file=sys.stderr)
    sys.exit(3)

REPLACEMENT = f'        # {MARKER} — chat-guid prefix check removed. Rely on the `service`\n        # field check above; SMS traffic reliably carries service="SMS".\n'

new_src = prefix_block_re.sub(REPLACEMENT, src, count=1)
compile(new_src, str(TARGET), "exec")
TARGET.with_suffix(".py.bak.relax").write_text(src)
TARGET.write_text(new_src)
print("RELAXED")
