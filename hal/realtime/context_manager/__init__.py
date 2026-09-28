from hal.realtime.context_manager.base import ContextManagerBase
from hal.realtime.context_manager.claudecode import ClaudeCodeContextManager
from hal.realtime.context_manager.hermes import HermesContextManager
from hal.realtime.context_manager.openclaw import OpenClawContextManager

__all__ = [
    "CONTEXT_MANAGERS",
    "ContextManagerBase",
    "OpenClawContextManager",
    "HermesContextManager",
    "ClaudeCodeContextManager",
]

# Shared by realtime context and voice startup; runtime layouts live here.
# PicoClaw, Codex and OpenCode reuse the OpenClaw identity/memory layout.
# Claude Code inherits that layout and changes only its skills directory.
CONTEXT_MANAGERS: dict[str, type[ContextManagerBase]] = {
    "openclaw": OpenClawContextManager,
    "hermes": HermesContextManager,
    "picoclaw": OpenClawContextManager,
    "codex": OpenClawContextManager,
    "claudecode": ClaudeCodeContextManager,
    "opencode": OpenClawContextManager,
}
