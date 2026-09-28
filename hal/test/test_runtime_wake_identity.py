"""Voice restart restores runtime-specific names without a rename notification."""

import threading

import pytest

from hal import app_state
from hal.realtime.context_manager import CONTEXT_MANAGERS
from hal.drivers.voice._internal.speaker_decorate import SpeakerDecorator, merge_wake_words


@pytest.mark.parametrize('runtime,filename', [
    ('openclaw', 'IDENTITY.md'), ('hermes', 'SOUL.md'),
    ('picoclaw', 'IDENTITY.md'), ('codex', 'IDENTITY.md'),
    ('claudecode', 'IDENTITY.md'), ('opencode', 'IDENTITY.md'),
])
def test_voice_restart_loads_active_identity_and_preserves_aliases(tmp_path, monkeypatch, runtime, filename):
    monkeypatch.setattr(app_state._hal_config, 'AGENT_GATEWAY', runtime)
    monkeypatch.setattr(app_state._hal_config, 'ACTIVE_AGENT_WORKSPACE_DIR', str(tmp_path))
    other = 'SOUL.md' if filename == 'IDENTITY.md' else 'IDENTITY.md'
    (tmp_path / other).write_text('- **Name:** Stale\n')
    (tmp_path / filename).write_text('You are Lamp.\n- **Name:** Mini — companion\n')
    for _ in range(2):  # Re-create the startup aliases with no rename callback.
        name = app_state._read_agent_name()
        assert name == 'mini'
        decorator = object.__new__(SpeakerDecorator)
        decorator._wake_words_lock = threading.Lock()
        decorator._wake_words = merge_wake_words(
            ['hello lamp', 'hi autonomous'], app_state._build_wake_words(name))
        assert decorator.starts_with_wake_word('Hello mini, turn on the light')
        assert decorator.starts_with_wake_word('Hello lamp, can you hear me?')
        assert not decorator.starts_with_wake_word('Hello stale, can you hear me?')
    (tmp_path / filename).write_text('- **Name:** Luna\n')
    assert app_state._read_agent_name() == 'luna'


@pytest.mark.parametrize('runtime', list(CONTEXT_MANAGERS))
def test_missing_or_non_identity_prose_uses_device_fallback(tmp_path, monkeypatch, runtime):
    monkeypatch.setattr(app_state._hal_config, 'AGENT_GATEWAY', runtime)
    monkeypatch.setattr(app_state._hal_config, 'ACTIVE_AGENT_WORKSPACE_DIR', str(tmp_path))
    monkeypatch.setattr(app_state._hal_config, 'resolve_device_type', lambda: 'lamp')
    assert app_state._read_agent_name() == 'lamp'
    manager = CONTEXT_MANAGERS[runtime]
    path = tmp_path / manager.IDENTITY_NAME_FILE
    path.write_text('You are Mini. Example: **Name:** Wrong\n- **Name:**   \n')
    assert app_state._read_agent_name() == 'lamp'
    path.write_text('* **NAME:** Mini | companion\n')
    assert app_state._read_agent_name() == 'mini'


def test_hermes_prompt_uses_same_authoritative_name_source(tmp_path):
    (tmp_path / 'SOUL.md').write_text('You are Lamp.\n- **Name:** Mini\n')
    manager = CONTEXT_MANAGERS['hermes'](workspace_dir=str(tmp_path))
    instructions = manager.build_instructions()
    assert 'given name is the **Name:** value in SOUL.md' in instructions
    assert 'given name is the **Name:** value in IDENTITY.md' not in instructions


def test_realtime_and_voice_share_runtime_registry():
    from hal.realtime.enums import AgentGateway
    from hal.realtime.orchestrator import RealtimeOrchestrator

    assert RealtimeOrchestrator.CONTEXT_MANAGERS is CONTEXT_MANAGERS
    for gateway in AgentGateway:
        assert gateway in CONTEXT_MANAGERS
