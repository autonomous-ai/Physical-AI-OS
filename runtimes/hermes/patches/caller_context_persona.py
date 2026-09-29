#!/usr/bin/env python3
"""Idempotently patch Hermes gateway to inject BLUEBUBBLES_CALLER_CONTEXT as a LEGITIMATE per-channel system_prompt for the BlueBubbles platform."""
import re
import sys
from pathlib import Path

RUN_PY = Path("/usr/local/lib/hermes-agent/gateway/run.py")
BLUEBUBBLES_PY = Path("/usr/local/lib/hermes-agent/gateway/platforms/bluebubbles.py")
MARKER = "_BLUEBUBBLES_SYSTEM_PROMPT_INJECTION_APPLIED"

INJECTION = '''
        # ''' + MARKER + '''
        # Route the operator-configured BlueBubbles caller context into
        # Hermes's real system_prompt path so the LLM trusts it as a
        # legitimate persona instead of rejecting it as prompt injection
        # (as it does when the same text is prepended to user text in the
        # plugin). Applies to every BlueBubbles chat unless the operator
        # already pinned a per-chat channel_override for this chat_id.
        try:
            if str(getattr(platform, "value", platform)) == "bluebubbles":
                import os as _os
                _bb_ctx = _os.getenv("BLUEBUBBLES_CALLER_CONTEXT", "").strip()
                if _bb_ctx:
                    return _bb_ctx
        except Exception:
            pass
'''

if not RUN_PY.exists():
    print(f"NOT_FOUND: {RUN_PY}", file=sys.stderr)
    sys.exit(2)

run_src = RUN_PY.read_text()
if MARKER not in run_src:
    anchor_re = re.compile(
        r'(def _get_system_prompt_for_channel\([^)]*\)[^:]*:\n'
        r'(?:\s+"""[\s\S]*?"""\n)?)'
    )
    m = anchor_re.search(run_src)
    if not m:
        print("RUN_PY_ANCHOR_NOT_FOUND", file=sys.stderr)
        sys.exit(3)
    new_run = run_src[:m.end()] + INJECTION + run_src[m.end():]
    compile(new_run, str(RUN_PY), "exec")
    RUN_PY.with_suffix(".py.bak.persona").write_text(run_src)
    RUN_PY.write_text(new_run)
    print("RUN_PY_PATCHED")
else:
    print("RUN_PY_ALREADY_PATCHED")

if BLUEBUBBLES_PY.exists():
    bb_src = BLUEBUBBLES_PY.read_text()
    remove_re = re.compile(
        r'\n        # _CALLER_CONTEXT_APPLIED[\s\S]*?text = _caller_ctx \+ _nl \+ _nl \+ "Customer message: " \+ text\n',
        re.MULTILINE,
    )
    m = remove_re.search(bb_src)
    if m:
        compile(remove_re.sub("\n", bb_src, count=1), str(BLUEBUBBLES_PY), "exec")
        BLUEBUBBLES_PY.with_suffix(".py.bak.caller_removed").write_text(bb_src)
        BLUEBUBBLES_PY.write_text(remove_re.sub('\n', bb_src, count=1))
        print("BLUEBUBBLES_TEXT_PREFIX_REMOVED")
    else:
        remove_old_re = re.compile(
            r'\n        # _CALLER_CONTEXT_APPLIED[\s\S]*?text = "\[SYSTEM CONTEXT[\s\S]*?" \+ text\n',
            re.MULTILINE,
        )
        if remove_old_re.search(bb_src):
            compile(remove_old_re.sub("\n", bb_src, count=1), str(BLUEBUBBLES_PY), "exec")
            BLUEBUBBLES_PY.with_suffix(".py.bak.caller_removed").write_text(bb_src)
            BLUEBUBBLES_PY.write_text(remove_old_re.sub('\n', bb_src, count=1))
            print("BLUEBUBBLES_OLD_TEXT_PREFIX_REMOVED")
        else:
            print("BLUEBUBBLES_TEXT_PREFIX_ABSENT")
