#!/usr/bin/env python3
"""Idempotently patch bluebubbles.py so the webhook handler ONLY processes
messages whose transport is iMessage — SMS from carriers (VinaPhone hotline
888, Google verification codes, banking OTPs, government notices, …) reach
Messages.app on macOS through the same inbox and would otherwise trigger
the agent, creating reply loops with automated SMS senders that keep
answering back ("Cu phap khong hop le, QK vui long lien he 18001091" →
bot replies → 888 auto-replies → bot replies again).

Filter: check the message payload's `service` field (BlueBubbles inserts
either "iMessage" or "SMS"). Also check the chat GUID prefix
(iMessage;-;… vs SMS;-;…) as a belt-and-braces fallback for payloads that
omit `service`. Both are compared case-insensitively.

Isolated to bluebubbles.py — no other channel is touched."""
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

# Anchor: insert right after the isFromMe drop block. The isFromMe check
# already returns early for self-messages; we mirror that pattern for
# non-iMessage traffic. This keeps the drop close to other early-exit
# filters, before tapback / attachment / handle-resolution work.
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
