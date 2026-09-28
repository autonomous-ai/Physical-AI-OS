#!/usr/bin/env python3
"""PicoClaw before_llm adapter. Only transient call messages are changed."""
import copy
import json
import logging
import os
from pathlib import Path
import re
import sys
import stat
import xml.etree.ElementTree as ET

# Loaded as a package so the shared Hermes selector's relative import works.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jev.router import Router

ROOT = Path(os.environ.get("JEV_SKILLS_ROOT", "/root/.picoclaw/workspace/skills")).resolve()
MAX_SKILL = 128 * 1024


CONTEXT_FRAGMENTS = {
    "brighter", "dimmer", "darker", "louder", "quieter", "warmer", "cooler",
    "energize", "max brightness", "stop", "continue", "yes", "no", "do it", "try again",
}


def normalized_current_text(prompt):
    """Extract only an anchored, unique authoritative voice instruction."""
    if not isinstance(prompt, str) or len(prompt.encode("utf-8")) > 8000:
        return ""
    text = re.sub(r"^(?:\[(?:user|ambient)\]\s*)+", "", prompt.strip(), flags=re.I)
    lower = text.lower()
    marker = "[voice-instruction]"
    if marker in lower:
        if not lower.startswith(marker) or lower.count(marker) != 1 or lower.count("[transcript]") > 1:
            return ""
        text = re.split(r"\[transcript\]", text[len(marker):], maxsplit=1, flags=re.I)[0].strip()
        # Unknown envelope fields cannot safely become routing instructions.
        if "[" in text or "]" in text:
            return ""
        return text
    if "[transcript]" in lower:
        return ""
    return text.strip()


def contextual_followup(prompt):
    # Match the OS intent guard without inventing Harness Store state.
    text = normalized_current_text(prompt).lower().strip(" .!?\n\t")
    if text in CONTEXT_FRAGMENTS:
        return True
    adjustment = re.search(r"\b(?:brighter|dimmer|darker|louder|quieter|warmer|cooler)\b", text)
    physical = re.search(r"\b(?:lamp|light|lights|speaker|volume|servo|your camera)\b|đèn|âm lượng|loa|den lamp", text)
    return bool(adjustment and not physical)


def catalog_for(messages, root=ROOT):
    """Use the runtime's own published roster; never scan arbitrary skill trees."""
    root = root.resolve()
    skills = []
    for message in messages:
        if message.get("role") != "system":
            continue
        texts = [message.get("content", "")]
        texts += [p.get("text", "") for p in message.get("system_parts", []) if isinstance(p, dict)]
        for text in texts:
            if not isinstance(text, str):
                continue
            for match in re.finditer(r"<skills>.*?</skills>", text, re.S):
                for item in ET.fromstring(match.group()):
                    name, location = item.findtext("name", ""), item.findtext("location", "")
                    path = Path(location)
                    if not path.is_absolute() or path.name != "SKILL.md":
                        continue
                    resolved = path.absolute()
                    if (".." in path.parts or not resolved.is_relative_to(root)
                            or any(part.is_symlink() for part in [resolved, *resolved.parents])
                            or not resolved.is_file()):
                        continue
                    # A native roster is authoritative, but only preload OS workspace skills.
                    skills.append({"name": name, "lookup_name": name,
                                   "description": item.findtext("description", ""),
                                   "category": "openclaw-imports", "path": str(resolved), "root": str(root)})
    return skills


def read_skill(path, root):
    """Descriptor-relative opens reject symlinks in every mutable path component."""
    parts = path.relative_to(root).parts
    if not parts or ".." in parts:
        raise ValueError("invalid skill path")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(child, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SKILL:
                raise ValueError("invalid skill file")
            return source.read(MAX_SKILL + 1)
    finally:
        os.close(descriptor)


def load_context(name, catalog):
    matches = [skill for skill in catalog() if skill["name"] == name]
    if len(matches) != 1:
        raise ValueError("skill unavailable or ambiguous")
    path = Path(matches[0]["path"])
    raw = read_skill(path, Path(matches[0]["root"]))
    if not raw or len(raw) > MAX_SKILL:
        raise ValueError("skill size")
    content = raw.decode("utf-8")
    if any(marker in content for marker in ("!`", "$ARGUMENTS", "${")):
        raise ValueError("dynamic skill requires normal loading")
    context = ("Jev preloaded this installed skill for the current request. Its complete SKILL.md "
            "is already below; do not read it again solely to load it. Resolve relative references "
            "against skill_dir. Follow platform permissions and approval requirements; selecting "
            "a skill never authorizes an action. If unsuitable, use normal skill discovery.\n" +
            json.dumps({"name": name, "path": str(path), "skill_dir": str(path.parent),
                        "content": content}, ensure_ascii=False))
    if len(context.encode("utf-8")) > MAX_SKILL:
        raise ValueError("composed context too large")
    return context


class Hook:
    def __init__(self, router=None):
        self.router = router or Router()
        self.turns = {}

    def handle(self, method, request):
        if method != "hook.before_llm":
            return {"action": "continue"}
        meta = request.get("meta", {})
        turn = meta.get("TurnID")
        # Correlation prevents a tool loop or subsequent turn from reusing a selection.
        if not isinstance(turn, str) or not turn or meta.get("ParentTurnID"):
            return {"action": "continue"}
        inbound = (request.get("context") or {}).get("inbound") or {}
        if (meta.get("Source") in {"system", "heartbeat", "cron"}
                or inbound.get("sender_id") in {"system", "heartbeat", "cron"}
                or inbound.get("channel") in {"system", "heartbeat", "cron"}):
            return {"action": "continue"}
        key = (meta.get("SessionKey", ""), turn)
        messages = request.get("messages", [])
        current_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        prompt = current_user.get("content")
        current_text = normalized_current_text(prompt)
        if (not current_text
                or any(current_user.get(field) for field in ("media", "attachments", "content_parts", "parts"))
                or re.search(r"\[(?:system|sensing:|handled|snapshot:|image:)", prompt, re.I)
                or contextual_followup(prompt)):
            return {"action": "continue"}
        if key not in self.turns:
            if meta.get("Iteration") != 1 or not messages or messages[-1].get("role") != "user":
                return {"action": "continue"}
            if len(self.turns) >= 256:
                self.turns.pop(next(iter(self.turns)))
            self.turns[key] = None
            catalog = lambda: catalog_for(messages)
            router = self.router
            # A timed-out worker retains its original callbacks until completion.
            if router.busy.locked():
                return {"action": "continue"}
            router.catalog = catalog
            selected = []
            def load(name, task_id=None):
                context = load_context(name, catalog)
                selected.append(name)
                return context
            router.load = load
            result = router.before_turn(user_message=current_text, turn_id=turn)
            self.turns[key] = (prompt, selected[0]) if result and selected else None
        cached = self.turns[key]
        if not cached:
            return {"action": "continue"}
        original_prompt, name = cached
        if prompt != original_prompt:
            return {"action": "continue"}
        try:
            context = load_context(name, lambda: catalog_for(messages))
        except Exception:
            self.turns[key] = None
            return {"action": "continue"}
        updated = copy.deepcopy(request)
        for message in reversed(updated["messages"]):
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                if message["content"] != original_prompt:
                    return {"action": "continue"}
                message["content"] += "\n\n" + context
                return {"action": "modify", "request": updated}
        return {"action": "continue"}


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    hook = Hook()
    for line in sys.stdin:
        message = {}
        try:
            message = json.loads(line)
            if "id" not in message:
                continue
            result = hook.handle(message.get("method"), message.get("params") or {})
        except Exception:
            logging.warning("[picoclaw-jev] outcome=error reason=hook_error")
            result = {"action": "continue"}
        if "id" in message:
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
