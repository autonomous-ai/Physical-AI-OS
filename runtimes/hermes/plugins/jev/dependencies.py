"""Bounded, one-hop prefetch of declared skill dependencies; never execute them."""

import json
import re
import time
from pathlib import PurePosixPath

from .preload import inline_budget, load_skill_context

MAX_DEPENDENCIES = 2
MAX_REFERENCES = 3
MAX_DECLARATIONS = 8


def condition_matches(condition, message):
    """Match one typed field in exactly one standalone OS context block."""
    if not isinstance(condition, dict) or set(condition) != {"context", "field", "equals"}:
        return False
    block, field = condition["context"], condition["field"]
    if not isinstance(block, str) or not re.fullmatch(r"[a-z_]{1,64}", block):
        return False
    if not isinstance(field, str) or not re.fullmatch(r"[a-z_]{1,64}", field):
        return False
    starts = list(re.finditer(r"(?m)^\[" + re.escape(block) + r":\s*(?=\{)", message))
    if len(starts) != 1:
        return False
    try:
        value, end = json.JSONDecoder().raw_decode(message, starts[0].end())
        if not message[end:].startswith("]") or not isinstance(value, dict):
            return False
        actual = value.get(field)
        if field not in value or type(condition["equals"]) not in (bool, str, int, float):
            return False
        expected = condition["equals"]
        return type(actual) is type(expected) and actual == expected
    except (ValueError, TypeError):
        return False


def reference_path(value):
    if not isinstance(value, str) or not value or len(value) > 192 or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        return None
    return value if path.suffix == ".md" and value != "SKILL.md" else None


def preload_dependencies(primary, context, catalog, eligible, message, task_id, deadline,
                         loader=load_skill_context):
    """Keep the accepted primary even if an optional group cannot be loaded.

    Metadata may prefetch useful supporting instructions, but does not choose
    the final action or bypass the primary skill's guards. No recursive walk.
    """
    stats = {"dependency_skills": 0, "dependency_files": 0, "dependency_skipped": 0}
    skill = next((s for s in catalog if isinstance(s, dict) and s.get("lookup_name", s.get("name")) == primary), {})
    declarations = skill.get("jev_preload", [])
    if not isinstance(declarations, list):
        return context, stats
    seen = {primary}
    for dependency in declarations[:MAX_DECLARATIONS]:
        if stats["dependency_skills"] >= MAX_DEPENDENCIES or time.monotonic() >= deadline:
            break
        if not isinstance(dependency, dict) or not condition_matches(dependency.get("when"), message):
            continue
        name, references = dependency.get("skill"), dependency.get("references", [])
        if not isinstance(name, str) or not re.fullmatch(r"[\w.-]{1,128}", name):
            stats["dependency_skipped"] += 1
            continue
        # Relative dependencies stay in the selected skill's namespace.
        # Native bare names never replace an OS dependency with a namesake.
        lookup = primary.rsplit("/", 1)[0] + "/" + name if "/" in primary else name
        if lookup in seen:
            continue
        seen.add(lookup)
        if (lookup not in eligible or not isinstance(references, list)
                or len(references) > MAX_REFERENCES
                or any(reference_path(ref) is None for ref in references)):
            stats["dependency_skipped"] += 1
            continue
        try:
            pieces = []
            for reference in [None, *dict.fromkeys(references)]:
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                piece = loader(lookup, task_id, file_path=reference)
                if not isinstance(piece, str) or not piece.strip():
                    raise ValueError()
                pieces.append(piece)
            addition = ("\n\nSupporting instructions prefetched from the primary skill's dependency metadata. "
                        "These files are already read for this turn. Use them only if the primary skill's "
                        "execution conditions hold; prefetch is not an instruction to run a supporting skill.\n"
                        + "\n\n".join(pieces))
            if time.monotonic() >= deadline or len(context) + len(addition) > inline_budget():
                raise ValueError()
            context += addition
            stats["dependency_skills"] += 1
            stats["dependency_files"] += len(pieces)
        except Exception:
            # An optional miss must never throw away a usable primary preload.
            stats["dependency_skipped"] += 1
    return context, stats
