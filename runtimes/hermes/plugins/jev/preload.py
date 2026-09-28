"""Load selected skills through Hermes without executing inline shell templates."""

import json

MAX_SKILL_RESPONSE = 128 * 1024


class PreloadError(ValueError):
    """Stable diagnostic codes, without logging skill contents or native errors."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def load_skill_context(name, task_id=None):
    from tools.skills_tool import skill_view

    # An older native API without preprocess=False must fail open, never retry
    # with preprocessing enabled: rendering a skill can execute shell snippets.
    raw = skill_view(name=name, task_id=task_id, preprocess=False)
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_SKILL_RESPONSE:
        raise PreloadError("skill_response_size", "invalid skill response size")
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get("success") is not True:
        raise PreloadError("skill_rejected", "native skill load rejected")
    content = result.get("content")
    if not isinstance(content, str) or not content.strip():
        raise PreloadError("skill_empty", "empty skill")
    if "!`" in content:
        raise PreloadError("skill_dynamic", "dynamic skill requires normal loading")
    # Keep the native result intact, including absolute skill_dir, linked files,
    # setup requirements and warnings; do not silently strip readiness metadata.
    context = (
        "Jev selected the following skill for this request. Its native skill_view "
        "result is already loaded below. Use these instructions to perform the task; "
        "do not call skill_view again just to read this same SKILL.md. "
        "Read linked references only when needed, using lookup_name exactly as skill_view name "
        "(including its category); bare names may collide. Platform rules, mandatory connectors, "
        "permissions and approval requirements remain authoritative. Loading a skill "
        "does not authorize actions. If the skill is unsuitable, use normal skill discovery.\n"
        + json.dumps({"lookup_name": name, "skill": result}, ensure_ascii=False)
    )

    # Match the active Hermes hook collector, including explicit smaller caps.
    # Never claim a preload succeeded if core would replace it with a file hint.
    cap = MAX_SKILL_RESPONSE
    try:
        from tools.hook_output_spill import get_spill_config
    except ModuleNotFoundError:
        pass  # Older Hermes injects context directly without a spill layer.
    else:
        config = get_spill_config()
        if config.get("enabled", True):
            cap = min(cap, int(config["max_chars"]))
    if len(context) > cap:
        raise PreloadError("skill_inline_budget", "skill context exceeds inline hook budget")
    return context
