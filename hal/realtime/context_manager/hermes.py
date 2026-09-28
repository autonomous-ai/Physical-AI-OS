"""Hermes context manager — loads identity, memory, and skills from the Hermes workspace.

Hermes workspace layout:
- SOUL.md (personality/tone)
- memories/USER.md (user preferences)
- memories/MEMORY.md (agent-curated notes, 2200 char limit)
- skills/*/SKILL.md (same format as OpenClaw)
"""

import logging
import re
from pathlib import Path
from typing import override

from hal.realtime.context_manager.base import ContextManagerBase

logger = logging.getLogger(__name__)


def _first_sentence(text: str, cap: int = 150) -> str:
    """First sentence of a skill description (cheap token cut for the catalog).

    Collapses newlines, cuts at the first sentence terminator (. ! ?) followed
    by whitespace, else hard-caps at `cap` chars. Keeps the catalog
    declaration-driven while dropping the verbose main-agent-only tail.
    """
    text = " ".join(text.split())
    m = re.search(r"[.!?](\s|$)", text)
    s = text[: m.end()].strip() if m else text
    return s[:cap].rstrip()


class HermesContextManager(ContextManagerBase):
    """Context manager for the Hermes agent runtime.

    Reads SOUL.md and USER.md for identity, MEMORY.md for device memory,
    and skills/*/SKILL.md for the skill catalog.
    """

    IDENTITY_NAME_FILE = "SOUL.md"

    FRONTMATTER_RE: re.Pattern[str] = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
    NAME_RE: re.Pattern[str] = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
    DESC_RE: re.Pattern[str] = re.compile(r"^description:\s*(.+)$", re.MULTILINE)

    @override
    def load_device_context(self) -> str:
        """Load SOUL.md and USER.md from the workspace."""
        parts: list[str] = []
        for path in (
            self._workspace / "SOUL.md",
            self._workspace / "memories" / "USER.md",
        ):
            try:
                content: str = path.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(content)
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning("[realtime] Failed to read %s: %s", path, e)
        return "\n\n".join(parts)

    @override
    def load_device_memory(self) -> list[str]:
        """Load MEMORY.md from the workspace memories directory."""
        memory_path: Path = self._workspace / "memories" / "MEMORY.md"
        try:
            content: str = memory_path.read_text(encoding="utf-8").strip()
            if content:
                return [content]
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("[realtime] Failed to read %s: %s", memory_path, e)
        return []

    @override
    def load_skills_catalog(self) -> str:
        """Parse SKILL.md frontmatter from the workspace skills directory."""
        skills_dir: Path = self._workspace / "skills"
        if not skills_dir.is_dir():
            return ""

        rows: list[tuple[str, str]] = []
        for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
            try:
                text: str = skill_md.read_text(encoding="utf-8")
                fm_match = self.FRONTMATTER_RE.match(text)
                if not fm_match:
                    continue
                frontmatter: str = fm_match.group(1)
                name_match = self.NAME_RE.search(frontmatter)
                desc_match = self.DESC_RE.search(frontmatter)
                name: str = (
                    name_match.group(1).strip() if name_match else skill_md.parent.name
                )
                desc: str = _first_sentence(desc_match.group(1)) if desc_match else ""
                if name:
                    rows.append((name, desc))
            except Exception as e:
                logger.warning("[realtime] Failed to parse %s: %s", skill_md, e)

        if not rows:
            return ""

        lines: list[str] = ["| Skill | Description |", "|-------|-------------|"]
        for name, desc in rows:
            lines.append(f"| {name} | {desc} |")
        return "\n".join(lines)

    @override
    def summarize_device_memory(self) -> None:
        """No-op — Hermes manages memory within a 2200 char limit, no summarization needed."""
