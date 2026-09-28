"""OpenClaw context manager — loads identity, memory, and skills from the OpenClaw workspace."""

import logging
import re
from pathlib import Path
from typing import override

import hal.config as app_config
from hal.realtime.context_manager.base import ContextManagerBase

logger = logging.getLogger(__name__)


def _first_sentence(text: str, cap: int = 150) -> str:
    """First sentence of a skill description, else hard-capped at `cap` chars."""
    text = " ".join(text.split())
    m = re.search(r"[.!?](\s|$)", text)
    s = text[: m.end()].strip() if m else text
    return s[:cap].rstrip()


class OpenClawContextManager(ContextManagerBase):
    """Context manager for the OpenClaw agent runtime."""

    FRONTMATTER_RE: re.Pattern[str] = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
    NAME_RE: re.Pattern[str] = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
    DESC_RE: re.Pattern[str] = re.compile(r"^description:\s*(.+)$", re.MULTILINE)

    # Relative to the workspace root; Claude Code overrides with .claude/skills.
    SKILLS_SUBDIR: tuple[str, ...] = ("skills",)

    @override
    def load_device_context(self) -> str:
        """Load SOUL.md, IDENTITY.md and USER.md, capped as a whole (SOUL survives truncation first)."""
        parts: list[str] = []
        for filename in ("SOUL.md", "IDENTITY.md", "USER.md"):
            path: Path = self._workspace / filename
            try:
                content: str = path.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(content)
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning("[realtime] Failed to read %s: %s", path, e)
        joined: str = "\n\n".join(parts)
        max_chars: int = app_config.REALTIME_IDENTITY_MAX_CHARS
        if len(joined) > max_chars:
            logger.warning(
                "[realtime] identity context truncated %d → %d chars "
                "(SOUL/IDENTITY/USER.md have grown — consider pruning USER.md)",
                len(joined), max_chars,
            )
            joined = joined[:max_chars]
        return joined

    @override
    def load_device_memory(self) -> list[str]:
        """Load root MEMORY.md, device summary, and recent memory files."""
        entries: list[str] = []

        if self._device_summary_path.exists():
            try:
                summary: str = self._device_summary_path.read_text(
                    encoding="utf-8"
                ).strip()
                if summary:
                    entries.append(f"[Previous summary]\n{summary}")
            except Exception as e:
                logger.warning("[realtime] Failed to read device summary: %s", e)

        total_chars: int = sum(len(entry) for entry in entries)

        # Curated long-term memory at the workspace root (not recoverable from device_summary.md).
        root_memory_path: Path = self._workspace / "MEMORY.md"
        try:
            root_memory: str = root_memory_path.read_text(encoding="utf-8").strip()
            if root_memory:
                root_entry: str = f"## Long-term memory\n\n{root_memory}"
                remaining = self._device_memory_max_chars - total_chars
                if remaining > 0:
                    if len(root_entry) > remaining:
                        logger.warning(
                            "[realtime] root MEMORY.md truncated %d → %d chars",
                            len(root_entry),
                            remaining,
                        )
                        root_entry = root_entry[:remaining]
                    entries.append(root_entry)
                    total_chars += len(root_entry)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("[realtime] Failed to read %s: %s", root_memory_path, e)

        memory_dir: Path = self._workspace / "memory"
        if not memory_dir.is_dir():
            return entries

        summary_mtime: float = (
            self._device_summary_path.stat().st_mtime
            if self._device_summary_path.exists()
            else 0.0
        )

        md_files: list[Path] = sorted(
            memory_dir.glob("*.md"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        for md_file in md_files:
            if md_file.stat().st_mtime <= summary_mtime:
                break
            try:
                content: str = md_file.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                entry: str = f"## {md_file.stem}\n\n{content}"
                if total_chars + len(entry) > self._device_memory_max_chars:
                    break
                entries.append(entry)
                total_chars += len(entry)
            except Exception as e:
                logger.warning("[realtime] Failed to read memory %s: %s", md_file, e)
        return entries

    @override
    def load_skills_catalog(self) -> str:
        """Parse SKILL.md frontmatter from all skills, return a markdown table."""
        skills_dir: Path = self._workspace.joinpath(*self.SKILLS_SUBDIR)
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
        """Summarize device memory files modified after the last device_summary.md."""
        if not self._summarizer:
            return
        memory_dir: Path = self._workspace / "memory"
        if not memory_dir.is_dir():
            return

        summary_mtime: float = (
            self._device_summary_path.stat().st_mtime
            if self._device_summary_path.exists()
            else 0.0
        )

        new_files: list[Path] = [
            f
            for f in sorted(memory_dir.glob("*.md"), key=lambda f: f.stat().st_mtime)
            if f.stat().st_mtime > summary_mtime
        ]
        if not new_files:
            return

        new_entries: list[str] = []
        total_chars: int = 0
        for md_file in new_files:
            try:
                content: str = md_file.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                entry: str = f"## {md_file.stem}\n\n{content}"
                remaining = self._device_memory_max_chars - total_chars
                if remaining <= 0:
                    break
                new_entries.append(f"...{entry[-remaining:]}")
                total_chars += len(new_entries[-1])
            except Exception as e:
                logger.warning("[realtime] Failed to read memory %s: %s", md_file, e)
        if not new_entries:
            return

        to_summarize: list[str] = []
        if self._device_summary_path.exists():
            try:
                existing: str = self._device_summary_path.read_text(
                    encoding="utf-8"
                ).strip()
                if existing:
                    to_summarize.append(f"[Previous summary]\n{existing}")
            except Exception:
                pass
        to_summarize.extend(new_entries)

        logger.info(
            "[realtime] Summarizing %d new device memory files...", len(new_files)
        )
        new_summary: str = self._summarizer.summarize(to_summarize)
        if new_summary:
            # Same write-time cap as base.py: re-fed every turn, so it compounds.
            if len(new_summary) > self._summary_max_chars:
                logger.warning(
                    "[realtime] device summary truncated %d → %d chars",
                    len(new_summary), self._summary_max_chars,
                )
                new_summary = new_summary[: self._summary_max_chars]
            self._device_summary_path.parent.mkdir(parents=True, exist_ok=True)
            _ = self._device_summary_path.write_text(
                new_summary + "\n", encoding="utf-8"
            )
            logger.info(
                "[realtime] Device memory summarization complete → device_summary.md"
            )
