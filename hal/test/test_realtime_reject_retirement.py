"""A terminal manual Gemini rejection need not generate an unused ACK reply."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hal import config
from hal.realtime.config import GeminiConfig
from hal.realtime.models import FunctionCallOutput, TextOutput
from hal.realtime.models.signal import RejectSignal
from hal.realtime.orchestrator import RealtimeOrchestrator
from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent


def setup_orch(monkeypatch):
    monkeypatch.setattr(config, 'REALTIME_PROVIDER', 'gemini')
    monkeypatch.setattr(config, 'LIVE_MODE', False)
    monkeypatch.setattr(config, 'REALTIME_GEMINI_SESSION_RESUMPTION', False)
    monkeypatch.setattr(config, 'REALTIME_SESSION_MAX_TURNS', 0)
    agent = Mock(requires_fresh_session=True, execution_completed=False)
    agent._config = SimpleNamespace(session_resumption_enabled=False)
    agent.receive.return_value = iter([FunctionCallOutput(
        name='reject_turn', arguments='{}', call_id='reject-1', user_turn_id='ambient-1')])
    orch = object.__new__(RealtimeOrchestrator)
    orch._agent = agent
    orch._live_active = False
    orch._skip_post_idle_recycle = False
    orch._consecutive_silent = 0
    orch._turns_since_recycle = 0
    orch._idle_reset_pending = False
    return orch, agent


@pytest.mark.parametrize('provider,live,active,resume,session_resume,fresh,ack', [
    ('gemini', False, False, False, False, True, False),
    ('gemini', True, False, False, False, True, True),
    ('gemini', False, True, False, False, True, True),
    ('gemini', False, False, True, False, True, True),
    ('gemini', False, False, False, True, True, True),
    ('gemini', False, False, False, False, False, True),
    ('openai', False, False, False, False, True, True),
    ('pipecat_v1', False, False, False, False, True, True),
])
def test_rejection_signal_unchanged_and_only_manual_unresumable_gemini_skips_ack(
    monkeypatch, provider, live, active, resume, session_resume, fresh, ack,
):
    orch, agent = setup_orch(monkeypatch)
    monkeypatch.setattr(config, 'REALTIME_PROVIDER', provider)
    monkeypatch.setattr(config, 'LIVE_MODE', live)
    monkeypatch.setattr(config, 'REALTIME_GEMINI_SESSION_RESUMPTION', resume)
    orch._live_active = active
    agent._config.session_resumption_enabled = session_resume
    agent.requires_fresh_session = fresh
    assert list(orch.stream_output()) == [RejectSignal(user_turn_id='ambient-1')]
    result, = agent.send.call_args.args[0]
    assert result.call_id == 'reject-1'
    assert result.trigger_response is ack
    assert result.output == '{"result": "turn dropped"}'
    agent.end_turn.assert_called_once()


def test_late_reject_after_spoken_answer_keeps_existing_error_handling(monkeypatch):
    orch, agent = setup_orch(monkeypatch)
    agent.receive.return_value = iter([TextOutput(text='Hello!'), FunctionCallOutput(
        name='reject_turn', arguments='{}', call_id='late')])
    assert list(orch.stream_output()) == [TextOutput(text='Hello!')]
    result, = agent.send.call_args.args[0]
    assert result.trigger_response is True
    assert 'error' in result.output


@pytest.mark.parametrize('connect_fails', [False, True])
def test_real_provider_no_wire_ack_then_replace_before_capture(monkeypatch, connect_fails):
    orch, _ = setup_orch(monkeypatch)
    agent = GeminiLiveAgent(GeminiConfig(api_key='test', session_resumption_enabled=False), tools=[])
    session = SimpleNamespace(send_tool_response=AsyncMock())
    agent._session = session
    agent._pending_tool_calls.add('reject-1')
    agent._pending_tool_names['reject-1'] = 'reject_turn'
    agent.receive = Mock(return_value=iter([FunctionCallOutput(
        name='reject_turn', arguments='{}', call_id='reject-1')]))
    queued = []
    agent.send = lambda inputs: queued.extend(inputs)
    orch._agent = agent
    assert list(orch.stream_output()) == [RejectSignal()]
    # Already unsafe to reuse BEFORE the asynchronous sender handles the result.
    assert agent.requires_fresh_session
    asyncio.run(agent._async_send_input(queued[0]))
    session.send_tool_response.assert_not_awaited()
    assert agent._requires_fresh_session

    replacement = Mock(requires_fresh_session=False)
    if connect_fails:
        replacement.connect.side_effect = RuntimeError('offline')
    orch._context = Mock()
    orch._context.build_instructions.return_value = 'same context'
    orch._make_agent = Mock(return_value=replacement)
    orch._rebuild_lock = threading.Lock()
    orch._rebuild_done = threading.Event()
    orch._lifecycle_lock = threading.Lock()
    orch._started = threading.Event()
    orch._started.set()
    orch._disconnect_in_background = Mock()
    orch.prepare_turn()
    assert orch._agent is (None if connect_fails else replacement)
    replacement.connect.assert_called_once()
    orch._disconnect_in_background.assert_called_once_with(agent, 'gemini-unresolved-tool-call')
    if connect_fails:
        replacement.disconnect.assert_called_once()
    else:
        orch.append_audio([0.0])
        replacement.append_audio.assert_called_once()
