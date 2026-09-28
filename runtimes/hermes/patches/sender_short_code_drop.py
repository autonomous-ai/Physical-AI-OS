#!/usr/bin/env python3
"""Idempotently patch bluebubbles.py so the webhook handler drops messages
whose SENDER address is a short code (3–6 digits, no `+`, no `@`) — the
telltale shape of a carrier / hotline SMS gateway.

Why this exists on top of imessage_only_service_filter.py:
BlueBubbles sometimes delivers a webhook payload WITHOUT the `service`
field. The existing filter checks `if _svc and _svc != "imessage"` — when
`_svc == ""` the condition short-circuits and lets the message through.
VinaPhone's 888 spam thread (2026-09-22) hit this exact gap: no `service`
key on the payload, no `sms;` prefix on the chat GUID, and the sender
"888" reached the agent, which happily replied to a spam bot.

Fix: after the two existing drops (service, sms chat_guid prefix), read
the sender/handle address (same fields the handler resolves later) and
drop if it looks like a short code — bare digits, length < 7. Real iMessage
handles are either an email (contains "@") or an E.164 phone number
(starts with "+", ≥ 8 digits with country code), so this can't false-positive
on a legitimate iPhone contact.

Isolated to bluebubbles.py — no other channel is touched."""
import sys
from pathlib import Path

TARGET = Path("/usr/local/lib/hermes-agent/gateway/platforms/bluebubbles.py")
MARKER = "_SENDER_SHORT_CODE_DROP_ADDED"

if not TARGET.exists():
    print("NOT_FOUND", file=sys.stderr)
    sys.exit(2)

src = TARGET.read_text()
if MARKER in src:
    print("ALREADY_PATCHED")
    sys.exit(0)

# Anchor: insert immediately after the SMS chat_guid prefix drop that
# imessage_only_service_filter.py / sms_prefix_drop.py wrote. That block
# ends with the RELAXED comment, which is a stable marker we already own.
# If either upstream patch failed to apply we exit early — no point
# hardening the third layer when the first two aren't there.
ANCHOR = "        # _IMESSAGE_ONLY_FILTER_RELAXED — chat-guid prefix check removed. Rely on the `service`\n        # field check above; SMS traffic reliably carries service=\"SMS\".\n"

INSERTION = '''
        # ''' + MARKER + '''
        # Belt-and-braces short-code sender drop. Covers the specific gap
        # where BlueBubbles omits the `service` field AND the chat GUID has
        # no explicit "sms;" prefix — the upstream service-check short-
        # circuits when `_svc` is empty, so a carrier hotline (888, 1091,
        # 18001091, 9209, …) would otherwise reach the agent.
        # Real iMessage handles always look like an email ("@" in address)
        # or an E.164 phone number ("+" prefix, with country code). Anything
        # that is pure digits with NO "+" prefix is a domestic SMS/short-code
        # address that iMessage never uses — drop it regardless of length,
        # so both 3-digit ("888") and longer hotlines ("18001091") are caught.
        _sender_probe = None
        _handle_probe = record.get("handle")
        if isinstance(_handle_probe, dict):
            _sender_probe = _handle_probe.get("address")
        if not _sender_probe:
            _sender_probe = (
                record.get("sender")
                or record.get("from")
                or record.get("address")
                or ""
            )
        _sender_probe = str(_sender_probe or "").strip()
        if (
            _sender_probe
            and "@" not in _sender_probe
            and not _sender_probe.startswith("+")
            and _sender_probe.isdigit()
        ):
            return web.Response(text="ok")
'''

if ANCHOR not in src:
    print("ANCHOR_NOT_FOUND", file=sys.stderr)
    sys.exit(3)

new_src = src.replace(ANCHOR, ANCHOR + INSERTION, 1)
compile(new_src, str(TARGET), "exec")
TARGET.with_suffix(".py.bak.short_code").write_text(src)
TARGET.write_text(new_src)
print("PATCHED")
