"""Without a Google Search tool the model must delegate fresh facts, not guess them."""

from hal.realtime.context_manager import base


def _manager(tmp_path, monkeypatch, provider):
    from hal.realtime.context_manager.hermes import HermesContextManager

    manager = HermesContextManager(workspace_dir=str(tmp_path), provider=provider)
    monkeypatch.setattr(manager, "_load_system_prompt", lambda: "POLICY")
    monkeypatch.setattr(manager, "load_device_context", lambda: "IDENTITY")
    monkeypatch.setattr(manager, "load_skills_catalog", lambda: "")
    monkeypatch.setattr(manager, "load_device_memory", lambda: [])
    monkeypatch.setattr(manager, "load_realtime_memory", lambda: [])
    return manager


def test_search_off_appends_the_no_lookup_rule_after_routing(tmp_path, monkeypatch):
    monkeypatch.setattr(base.app_config, "REALTIME_GEMINI_GOOGLE_SEARCH", False)
    instructions = _manager(tmp_path, monkeypatch, "gemini").build_instructions()
    assert "# NO LIVE LOOKUPS IN THIS SESSION" in instructions
    assert "call `delegate_to_main` with the user's words" in instructions
    assert instructions.index("# NO LIVE LOOKUPS") > instructions.index("# Routing before speech")


def test_search_on_keeps_the_prompt_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(base.app_config, "REALTIME_GEMINI_GOOGLE_SEARCH", True)
    instructions = _manager(tmp_path, monkeypatch, "gemini").build_instructions()
    assert "# NO LIVE LOOKUPS" not in instructions


def test_other_providers_never_get_the_gemini_block(tmp_path, monkeypatch):
    monkeypatch.setattr(base.app_config, "REALTIME_GEMINI_GOOGLE_SEARCH", False)
    instructions = _manager(tmp_path, monkeypatch, "openai").build_instructions()
    assert "# NO LIVE LOOKUPS" not in instructions


def test_delegate_preamble_is_an_opt_in_override(tmp_path, monkeypatch):
    monkeypatch.setattr(base.app_config, "REALTIME_GEMINI_GOOGLE_SEARCH", True)
    monkeypatch.setattr(base.app_config, "REALTIME_DELEGATE_PREAMBLE", False)
    assert "# TASK ACKNOWLEDGEMENT" not in _manager(tmp_path, monkeypatch, "gemini").build_instructions()
    monkeypatch.setattr(base.app_config, "REALTIME_DELEGATE_PREAMBLE", True)
    instructions = _manager(tmp_path, monkeypatch, "gemini").build_instructions()
    assert "# TASK ACKNOWLEDGEMENT (overrides the silent-handoff rule)" in instructions
    assert "then call `delegate_to_main` in the SAME turn" in instructions
    assert instructions.index("# TASK ACKNOWLEDGEMENT") > instructions.index("# Routing before speech")
