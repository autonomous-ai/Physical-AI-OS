#!/usr/bin/env python3
"""Idempotently patch /usr/local/lib/hermes-agent/gateway/platforms/bluebubbles.py
to strip Hermes internal markers (tool call syntax, turn notices, format-fail
prefixes) from the outgoing message body before it is sent to BlueBubbles.

Isolated to bluebubbles.py so ONLY iMessage channel is affected — Telegram,
Slack, Discord etc. call their own send() methods in their own plugin files
and are not touched.

Idempotent: reruns of this script no-op once the marker `_STRIP_HERMES_MARKERS_APPLIED`
is present."""
import ast
import re
import sys
from pathlib import Path

TARGET = Path("/usr/local/lib/hermes-agent/gateway/platforms/bluebubbles.py")
MARKER = "_STRIP_HERMES_MARKERS_APPLIED"

STRIP_FUNC = '''
# _STRIP_HERMES_MARKERS_APPLIED — device-side patch injected by autonomous OS.
# Removes Hermes internal formatting markers from outbound message text so end
# users never see raw tool-call syntax or turn-management notices in iMessage.
def _strip_hermes_internal_markers(text: str) -> str:
    """Strip Hermes-internal markup that leaks into LLM output.

    Handles:
      - `[HW:/<path>:{...}]`   tool-call markers (audio/play, emotion, etc.)
      - `[ignore]`              internal directive tag
      - `↪ Redirected current run...` full-line turn-redirect notice
      - `⚡ Interrupting current task...` full-line interrupt notice
      - `♻️ Recovered reply — the gateway restarted...` full-line recovery notice
      - `📬 No home channel is set for Bluebubbles...` + `Type /sethome ...`
        two-line home-channel nag pair (Hermes fires this if HOME_CHANNEL
        env is empty when a customer message arrives; presync now defaults
        it, but keep the strip as a belt-and-braces net for edge cases)
      - `(Response formatting failed, plain text:)` parser fallback prefix

    Returns the cleaned text with collapsed whitespace. An empty string result
    means the message was ENTIRELY internal noise — the caller should skip the
    send instead of delivering an empty bubble.
    """
    import re as _re
    if not text:
        return ""
    out = text
    # Bracketed tool call markers: `[HW:/audio/play:{"query":"..."}]`.
    # Non-greedy on the inner payload so multiple markers on one line each
    # match individually. Also cover the `[ignore]` and `[HW:...]` bare cases.
    out = _re.sub(r'\\[HW:/[^\\]]*(?:\\{[^{}]*(?:\\{[^{}]*\\}[^{}]*)*\\})?[^\\]]*\\]', '', out)
    out = _re.sub(r'\\[ignore\\]', '', out)
    # Full-line turn-management notices — bot should stay silent, not narrate.
    out = _re.sub(r'^\\s*↪\\s+Redirected current run.*$', '', out, flags=_re.MULTILINE)
    out = _re.sub(r'^\\s*⚡\\s+Interrupting current task.*$', '', out, flags=_re.MULTILINE)
    # `♻️ Recovered reply — the gateway restarted during delivery, so this may
    # be a duplicate:` — Hermes reruns a bot reply after a mid-turn restart
    # and prepends this notice. Users just see the reply twice; the notice
    # itself is diagnostic noise. Multi-line (the notice ends with a colon,
    # then more text follows on subsequent lines), so match to end of line.
    out = _re.sub(r'^\\s*♻️\\s+Recovered reply.*?:\\s*$', '', out, flags=_re.MULTILINE)
    # `📬 No home channel is set for Bluebubbles. A home channel is where
    # Hermes delivers cron job results and cross-platform messages.` — this
    # is diagnostic addressed to the operator, not something a shop customer
    # should ever read. Kill the whole two-line nag: the `📬 ...` line AND
    # the `Type /sethome ...` follow-up on the next line.
    out = _re.sub(r'^\\s*📬\\s+No home channel is set for.*$', '', out, flags=_re.MULTILINE)
    out = _re.sub(r'^\\s*Type\\s+/sethome\\s+to make this chat.*$', '', out, flags=_re.MULTILINE)
    # Format-parse fallback prefix — Hermes emits this before dumping raw text.
    out = _re.sub(r'\\(Response formatting failed, plain text:\\)?', '', out)
    # Collapse whitespace runs that the removals left behind.
    out = _re.sub(r'\\n{3,}', '\\n\\n', out)
    out = out.strip()
    return out
'''

# ---- 1. read current file ------------------------------------------------
if not TARGET.exists():
    print(f"NOT_FOUND: {TARGET}", file=sys.stderr)
    sys.exit(2)
src = TARGET.read_text()

if MARKER in src:
    print("ALREADY_PATCHED")
    sys.exit(0)

# ---- 2. locate the `async def send(` method's format_message line --------
# We inject a strip call right after `text = self.format_message(content)`.
send_line_re = re.compile(r'(\n        text = self\.format_message\(content\)\n)')
if not send_line_re.search(src):
    print("SEND_ANCHOR_NOT_FOUND — plugin layout changed, refusing to patch", file=sys.stderr)
    sys.exit(3)

# ---- 3. insert strip helper at module top-of-code (after imports) --------
# Find last `import` line, insert helper after that block.
lines = src.split('\n')
# AST end positions preserve parenthesized imports and future imports.
imports = [node for node in ast.parse(src).body
           if isinstance(node, (ast.Import, ast.ImportFrom))]
insert_at = max((node.end_lineno for node in imports), default=0)
if not insert_at:
    print("NO_IMPORTS_FOUND", file=sys.stderr)
    sys.exit(4)
new_lines = lines[:insert_at] + [STRIP_FUNC] + lines[insert_at:]
src2 = '\n'.join(new_lines)

# ---- 4. inject the strip call inside send() -----------------------------
# Match the exact line so we insert the strip call immediately after it, before
# the `if not text` guard. If the strip empties the text, the guard catches it
# and we return the same "requires text" error path — consistent with today.
patched = re.sub(
    r'(\n        text = self\.format_message\(content\)\n)',
    r'\1        text = _strip_hermes_internal_markers(text)\n',
    src2,
    count=1,
)
if patched == src2:
    print("STRIP_INJECTION_FAILED", file=sys.stderr)
    sys.exit(5)

# ---- 5. atomic write ----------------------------------------------------
compile(patched, str(TARGET), "exec")
tmp = TARGET.with_suffix('.py.tmp')
tmp.write_text(patched)
tmp.replace(TARGET)
print("PATCHED")
