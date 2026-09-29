"""Claude Code context manager — OpenClaw workspace layout, skills under .claude/skills."""

from hal.realtime.context_manager.openclaw import OpenClawContextManager


class ClaudeCodeContextManager(OpenClawContextManager):
    """Context manager for Claude Code; skills live in .claude/skills/<name>/, not workspace/skills."""

    SKILLS_SUBDIR: tuple[str, ...] = (".claude", "skills")
