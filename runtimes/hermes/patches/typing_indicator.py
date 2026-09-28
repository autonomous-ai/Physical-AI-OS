#!/usr/bin/env python3
"""Idempotently patch bluebubbles.py so an incoming message triggers a typing
indicator that pulses to the sender's iPhone every 3s until the LLM reply
lands. Isolated to bluebubbles.py so only iMessage is affected — Telegram /
Slack / Discord plugins do not run this code path."""
import ast
import re
import sys
from pathlib import Path

TARGET = Path("/usr/local/lib/hermes-agent/gateway/platforms/bluebubbles.py")
MARKER = "_TYPING_PULSE_APPLIED"

HELPER = '''
# _TYPING_PULSE_APPLIED — device-side patch injected by autonomous OS.
# Fires the BlueBubbles typing indicator repeatedly while the agent is
# generating a reply, so the sender sees "…" on their iPhone instead of a
# silent wait. iMessage clears the typing bubble every ~3s server-side, so
# we re-pulse at that cadence.
async def _typing_pulse_loop(self, chat_id, stop_event):
    import asyncio as _asyncio
    while not stop_event.is_set():
        try:
            await self.send_typing(chat_id)
        except Exception:
            pass
        try:
            await _asyncio.wait_for(stop_event.wait(), timeout=3.0)
        except _asyncio.TimeoutError:
            continue
        except Exception:
            break


async def _handle_with_typing(self, event, chat_id):
    import asyncio as _asyncio
    stop_event = _asyncio.Event()
    pulse_task = _asyncio.create_task(self._typing_pulse_loop(chat_id, stop_event))
    try:
        await self.handle_message(event)
    finally:
        stop_event.set()
        try:
            await _asyncio.wait_for(pulse_task, timeout=2.0)
        except Exception:
            pulse_task.cancel()
'''

if not TARGET.exists():
    print(f"NOT_FOUND: {TARGET}", file=sys.stderr)
    sys.exit(2)
src = TARGET.read_text()

if MARKER in src:
    print("ALREADY_PATCHED")
    sys.exit(0)

# Locate class name so we can attach the helper as methods (not module-level).
class_re = re.compile(r'class (\w+)\(.*?Platform.*?\):', re.MULTILINE)
m = class_re.search(src)
if not m:
    print("CLASS_ANCHOR_NOT_FOUND", file=sys.stderr)
    sys.exit(3)

# Inject helper as methods of the class: find the class body and append.
# Simpler: put the helper functions ABOVE the class as module-level, then
# monkey-patch onto the class at the bottom of the file.
inject_line_re = re.compile(
    r'(\n        task = asyncio\.create_task\(self\.handle_message\(event\)\)\n)'
)
if not inject_line_re.search(src):
    print("HANDLE_MESSAGE_ANCHOR_NOT_FOUND", file=sys.stderr)
    sys.exit(4)

# Add helper functions at module top-of-code (after imports)
lines = src.split('\n')
# AST end positions preserve parenthesized imports and future imports.
imports = [node for node in ast.parse(src).body
           if isinstance(node, (ast.Import, ast.ImportFrom))]
insert_at = max((node.end_lineno for node in imports), default=0)
new_lines = lines[:insert_at] + [HELPER] + lines[insert_at:]
src2 = '\n'.join(new_lines)

# At the end of the file, monkey-patch the class with our helpers.
class_name = m.group(1)
attach = f'''

# _TYPING_PULSE_APPLIED wiring — bind module-level helpers onto {class_name}
# so they read self.* and behave like real methods.
{class_name}._typing_pulse_loop = _typing_pulse_loop
{class_name}._handle_with_typing = _handle_with_typing
'''
src2 = src2.rstrip() + attach + '\n'

# Rewrite the create_task line to invoke our wrapper instead.
src2 = re.sub(
    r'(\n        )task = asyncio\.create_task\(self\.handle_message\(event\)\)\n',
    r'\1task = asyncio.create_task(self._handle_with_typing(event, session_chat_id))\n',
    src2,
    count=1,
)

# Atomic write.
compile(src2, str(TARGET), "exec")
tmp = TARGET.with_suffix('.py.tmp2')
tmp.write_text(src2)
tmp.replace(TARGET)
print("PATCHED")
