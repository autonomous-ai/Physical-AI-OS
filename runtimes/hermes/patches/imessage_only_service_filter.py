#!/usr/bin/env python3
"""Idempotently patch bluebubbles.py so the webhook handler only processes iMessage (not carrier SMS) messages."""
import re
import sys
from pathlib import Path

TARGET = Path("/usr/local/lib/hermes-agent/gateway/platforms/bluebubbles.py")
MARKER = "_IMESSAGE_ONLY_FILTER_APPLIED"

if not TARGET.exists():
    print("NOT_FOUND", file=sys.stderr)
    sys.exit(2)

src = TARGET.read_text()
if MARKER in src:
    print("ALREADY_PATCHED")
    sys.exit(0)

ANCHOR = '''        if is_from_me:
            return web.Response(text="ok")
'''

INSERTION = '''
        # ''' + MARKER + '''
        # Drop non-iMessage traffic (SMS from carriers, verification codes,
        # OTP hotlines) so the agent doesn't try to hold a "conversation"
        # with automated senders. Check both the payload's `service` field
        # (BlueBubbles's own marker) and the chat GUID prefix as a fallback.
        _svc = str(
            record.get("service")
            or record.get("chatStyle")
            or ""
        ).lower()
        if _svc and _svc != "imessage":
            return web.Response(text="ok")
        _chat_guid_probe = (
            record.get("chatGuid")
            or payload.get("chatGuid")
            or record.get("chat_guid")
            or ""
        )
        if not _chat_guid_probe:
            _chats_probe = record.get("chats") or []
            if _chats_probe and isinstance(_chats_probe[0], dict):
                _chat_guid_probe = (
                    _chats_probe[0].get("guid")
                    or _chats_probe[0].get("chatGuid")
                    or ""
                )
        _chat_guid_probe = str(_chat_guid_probe or "").lower()
        if _chat_guid_probe and not _chat_guid_probe.startswith("imessage;"):
            return web.Response(text="ok")
'''

if ANCHOR not in src:
    print("ANCHOR_NOT_FOUND", file=sys.stderr)
    sys.exit(3)

new_src = src.replace(ANCHOR, ANCHOR + INSERTION, 1)
compile(new_src, str(TARGET), "exec")
TARGET.with_suffix(".py.bak.imsg_only").write_text(src)
TARGET.write_text(new_src)
print("PATCHED")
