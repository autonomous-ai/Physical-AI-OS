"""Pin Gemini's filler/handoff contract; model compliance needs device trials."""

from hal.realtime.constants import RESOURCES_DIR


PROMPT = (RESOURCES_DIR / "system_prompt_gemini.md").read_text(encoding="utf-8")


def test_acknowledgment_never_replaces_same_turn_delegation():
    assert "your first response MUST be the actual `delegate_to_main` function call" in PROMPT
    assert "MUST call `delegate_to_main` in the SAME turn" in PROMPT
    assert "Do not speak an acknowledgment or call `express_emotion` before this handoff" in PROMPT
    assert "Emit only the handoff function call, with no spoken audio or text before or after it" in PROMPT
    assert "never end the turn after the acknowledgment alone" in PROMPT
    assert "never say the action is done, music is playing, or a reminder is set" in PROMPT
    for obsolete_rule in (
        "Call the tool OR speak", "No filler or meta-commentary",
        "DELEGATE (empty voice output)", "MUST delegate with blank voice",
        "your spoken output must be completely blank",
    ):
        assert obsolete_rule not in PROMPT
    delegation_examples = [line for line in PROMPT.splitlines()
                           if line.startswith("User:") and "delegate_to_main(" in line]
    assert delegation_examples
    assert all("blank voice" not in line for line in delegation_examples)


def test_music_example_requires_tool_call_before_receipt():
    example = next(line for line in PROMPT.splitlines()
                   if line.startswith('User: "Play something light'))
    assert "FIRST call `delegate_to_main" in example
    assert "with blank spoken output" in example
    assert 'delegate_to_main(message="Play something light, don\'t make it too loud")' in example
    assert "in the SAME turn" in example
    assert "never stop there or claim music is already playing" in example


def test_rejection_stays_silent_even_when_delegation_allows_acknowledgment():
    assert "For `reject_turn`, voice and text MUST remain completely blank" in PROMPT
    assert "rejection never gets an acknowledgment" in PROMPT
    assert "`reject_turn` when available + completely blank voice/text; no acknowledgment" in PROMPT


def test_direct_answer_completion_is_conditional_and_requires_fulfilled_intent():
    assert "only if `complete_response` is available" in PROMPT
    assert "fully fulfills the user's intent through speech alone" in PROMPT
    assert "MUST call `complete_response` in the SAME turn" in PROMPT
    assert "answered conversation, knowledge, public search, or a `look` question" in PROMPT
    assert "No such tool available → do not call or imitate it" in PROMPT
    assert "call it after speaking the answer" in PROMPT


def test_unresolved_actions_cannot_be_marked_as_direct_answer_completion():
    assert "Never call it for an action request, a filler acknowledgment, a promise" in PROMPT
    assert "an apology or system-error reply, or any unresolved work" in PROMPT
    assert "those require `delegate_to_main` with the user's request instead" in PROMPT
    assert "Do not call `complete_response` after delegation or rejection" in PROMPT
    assert 'call `delegate_to_main(message="Play a song")`, never `complete_response`' in PROMPT


def test_account_access_is_main_agent_owned_not_a_persona_answer():
    assert "You do not know which services the owner has linked" in PROMPT
    assert "Every request to check or read email" in PROMPT
    assert "determine account/connector access or connection status MUST delegate" in PROMPT
    assert '"your email" is an account request, not a question about your persona' in PROMPT
    assert "The main agent checks the actual linked state" in PROMPT
    assert 'Account ownership, email, and connector access questions follow the delegation rule above' in PROMPT


def test_email_acknowledgment_cannot_claim_or_deny_access():
    assert "Never claim that an account is connected or disconnected" in PROMPT
    assert "that you have or lack access" in PROMPT
    assert "that you do not have an email account" in PROMPT
    assert 'Never use "I\'m a desk lamp"' in PROMPT
    assert "do not append an access claim, refusal, setup advice, or a second answer" in PROMPT
    assert "do not use `complete_response` for an access denial" in PROMPT


def test_email_examples_delegate_the_original_request():
    for request in ("Check your email", "Is my Gmail connected?", "Read my latest email"):
        example = next(line for line in PROMPT.splitlines()
                       if line.startswith(f'User: "{request}"'))
        assert f'delegate_to_main(message="{request}")' in example
        assert "blank spoken output" in example


def test_gemini_routing_policy_follows_memory_context(tmp_path, monkeypatch):
    from hal.realtime.context_manager.hermes import HermesContextManager

    for provider in ('gemini', 'openai'):
        manager = HermesContextManager(workspace_dir=str(tmp_path), provider=provider)
        monkeypatch.setattr(manager, '_load_system_prompt', lambda: 'POLICY')
        monkeypatch.setattr(manager, 'load_device_context', lambda: 'IDENTITY')
        monkeypatch.setattr(manager, 'load_skills_catalog', lambda: '')
        monkeypatch.setattr(manager, 'load_device_memory', lambda: ['MEMORY'])
        monkeypatch.setattr(manager, 'load_realtime_memory', lambda: ['I can help with that.'])
        instructions = manager.build_instructions()
        assert 'I can help with that.' in instructions
        if provider == 'gemini':
            assert instructions.index('# Routing before speech') > instructions.index('# REALTIME MEMORY')
            assert 'actual delegate_to_main function call' in instructions
            assert 'For greetings answer directly' in instructions
            assert 'for visual questions use look' in instructions
        else:
            assert '# Routing before speech' not in instructions
