#!/usr/bin/env python3
"""Add an explicit SMS chat-GUID drop to the iMessage-only filter."""
import re
import sys
from pathlib import Path

TARGET = Path("/usr/local/lib/hermes-agent/gateway/platforms/bluebubbles.py")
MARKER = "_SMS_PREFIX_DROP_ADDED"

if not TARGET.exists():
    print("NOT_FOUND", file=sys.stderr)
    sys.exit(2)

src = TARGET.read_text()
if MARKER in src:
    print("ALREADY_ADDED")
    sys.exit(0)

ANCHOR = '''        if _svc and _svc != "imessage":
            return web.Response(text="ok")
'''

INSERTION = f'''        # {MARKER} — belt-and-braces SMS drop when the payload omits
        # `service` (BlueBubbles observed doing this for some carrier
        # threads). Only drop when the chat GUID EXPLICITLY names SMS;
        # ambiguous GUIDs pass through so real iMessage traffic is safe.
        _chat_guid_sms_probe = (
            record.get("chatGuid")
            or payload.get("chatGuid")
            or record.get("chat_guid")
            or ""
        )
        if not _chat_guid_sms_probe:
            _chats_sms_probe = record.get("chats") or []
            if _chats_sms_probe and isinstance(_chats_sms_probe[0], dict):
                _chat_guid_sms_probe = (
                    _chats_sms_probe[0].get("guid")
                    or _chats_sms_probe[0].get("chatGuid")
                    or ""
                )
        _chat_guid_sms_probe = str(_chat_guid_sms_probe or "").lower()
        if _chat_guid_sms_probe.startswith("sms;"):
            return web.Response(text="ok")
'''

if ANCHOR not in src:
    print("SERVICE_ANCHOR_NOT_FOUND — layout differs", file=sys.stderr)
    sys.exit(3)

new_src = src.replace(ANCHOR, ANCHOR + INSERTION, 1)
compile(new_src, str(TARGET), "exec")
TARGET.with_suffix(".py.bak.sms_prefix").write_text(src)
TARGET.write_text(new_src)
print("PATCHED")
