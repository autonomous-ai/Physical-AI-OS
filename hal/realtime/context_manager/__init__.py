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

# PicoClaw, Codex and OpenCode reuse the OpenClaw layout; Claude Code changes only its skills dir.
CONTEXT_MANAGERS: dict[str, type[ContextManagerBase]] = {
    "openclaw": OpenClawContextManager,
    "hermes": HermesContextManager,
    "picoclaw": OpenClawContextManager,
    "codex": OpenClawContextManager,
    "claudecode": ClaudeCodeContextManager,
    "opencode": OpenClawContextManager,
}
