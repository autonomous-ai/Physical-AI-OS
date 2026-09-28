#!/usr/bin/env python3
"""Extend the BlueBubbles system_prompt injection so it reads the caller
context from a file when the env var is empty. systemd EnvironmentFile does
not support multi-line values, so a multi-paragraph shop prompt saved
through the UI never lands in $BLUEBUBBLES_CALLER_CONTEXT — presync writes
it to ~/.hermes/bluebubbles_caller_context.txt instead, and this patch
teaches the injection to pick it up from there.

Idempotent: does nothing if the fallback marker is already present."""
import re
import sys
from pathlib import Path

TARGET = Path("/usr/local/lib/hermes-agent/gateway/run.py")
NEW_MARKER = "_BB_CALLER_CTX_FILE_FALLBACK"

if not TARGET.exists():
    print("NOT_FOUND", file=sys.stderr)
    sys.exit(2)

src = TARGET.read_text()
if NEW_MARKER in src:
    print("ALREADY_PATCHED")
    sys.exit(0)

OLD = '''        try:
            if str(getattr(platform, "value", platform)) == "bluebubbles":
                import os as _os
                _bb_ctx = _os.getenv("BLUEBUBBLES_CALLER_CONTEXT", "").strip()
                if _bb_ctx:
                    return _bb_ctx
        except Exception:
            pass'''

NEW = '''        try:
            if str(getattr(platform, "value", platform)) == "bluebubbles":
                # ''' + NEW_MARKER + '''
                # Env var wins when present (single-line prompts, tests),
                # but the UI-saved multi-line prompt lives in a file because
                # systemd EnvironmentFile refuses multi-line values.
                import os as _os
                _bb_ctx = _os.getenv("BLUEBUBBLES_CALLER_CONTEXT", "").strip()
                if not _bb_ctx:
                    try:
                        with open("/root/.hermes/bluebubbles_caller_context.txt", encoding="utf-8") as _f:
                            _bb_ctx = _f.read().strip()
                    except Exception:
                        _bb_ctx = ""
                if _bb_ctx:
                    return _bb_ctx
        except Exception:
            pass'''

if OLD not in src:
    print("OLD_INJECTION_NOT_FOUND", file=sys.stderr)
    sys.exit(3)

compile(src.replace(OLD, NEW, 1), str(TARGET), "exec")
TARGET.with_suffix(".py.bak.fallback").write_text(src)
TARGET.write_text(src.replace(OLD, NEW, 1))
print("PATCHED")
