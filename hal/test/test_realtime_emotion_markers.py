"""Inline expressions never enter spoken text, including split/incomplete chunks."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from hal import config
from hal.realtime.emotion_markers import EmotionMarkerStream, marker_instructions
from hal.realtime.models import TextOutput
from hal.realtime.orchestrator import EMOTION_TOOL, DELEGATE_TOOL, RealtimeOrchestrator

MARKER = '[HW:/emotion:{"emotion":"happy","intensity":0.8}]'


@pytest.mark.parametrize('cut', range(len(MARKER) + 1))
def test_every_split_is_removed_before_speech(cut):
    fire = Mock()
    parser = EmotionMarkerStream(['happy'], fire)
    text = parser.feed(MARKER[:cut]) + parser.feed(MARKER[cut:] + '[warm] Hello!')
    assert text == '[warm] Hello!'
    fire.assert_called_once_with('happy', 0.8)


def test_character_stream_and_duplicate_marker():
    fire = Mock()
    parser = EmotionMarkerStream(['happy'], fire)
    text = ''.join(parser.feed(c) for c in MARKER + 'Hello!' + MARKER)
    assert text == 'Hello!'
    fire.assert_called_once()
    parser.reset()
    parser.feed(MARKER)
    assert fire.call_count == 2


@pytest.mark.parametrize('payload', [
    '{"emotion":"happy","intensity":NaN}', '{"emotion":"happy","intensity":2}',
    '{"emotion":"happy","intensity":true}', '{"emotion":"sleep"}', 'null',
    '{"emotion":"happy","intensity":"0.8"}', '{bad}',
])
def test_invalid_marker_is_silent_and_never_executes(payload):
    fire = Mock()
    parser = EmotionMarkerStream(['happy'], fire)
    assert parser.feed('[HW:/emotion:' + payload + ']Hello') == 'Hello'
    fire.assert_not_called()


def test_partial_oversize_and_other_hw_are_never_spoken_or_executed():
    fire = Mock()
    parser = EmotionMarkerStream(['happy'], fire)
    assert parser.feed('Hi [HW:/emotion:{') == 'Hi '
    parser.reset()
    assert parser.feed('[HW:/emotion:' + 'x' * 1000 + ']Hello') == 'Hello'
    assert parser.feed('[HW:/servo/move:{}]Hi') == 'Hi'
    fire.assert_not_called()


def test_marker_fires_before_answer_is_delivered():
    fire = Mock()
    parser = EmotionMarkerStream(['happy'], fire)
    assert parser.feed(MARKER) == ''
    fire.assert_called_once()
    assert parser.feed('Hello') == 'Hello'


def test_stream_output_strips_before_consumers_and_does_not_ack(monkeypatch):
    monkeypatch.setattr(config, 'REALTIME_SESSION_MAX_TURNS', 0)
    orch = object.__new__(RealtimeOrchestrator)
    agent = Mock(execution_completed=True, execution_turn_id='turn', _emotion_markers_enabled=True)
    agent.receive.return_value = iter([TextOutput(text=MARKER[:12]),
                                      TextOutput(text=MARKER[12:] + 'Hello')])
    orch._agent = agent
    orch._skip_post_idle_recycle = False
    orch._consecutive_silent = 0
    orch._turns_since_recycle = 0
    orch._idle_reset_pending = False
    orch._fire_marker_emotion = Mock()
    assert list(orch.stream_output()) == [TextOutput(text='Hello')]
    orch._fire_marker_emotion.assert_called_once_with('happy', 0.8)
    agent.send.assert_not_called()


@pytest.mark.parametrize('native,voice,expression,model,enabled', [
    (False, None, True, 'gemini-3.8-live-extended-thinking', True),
    (True, None, True, 'gemini-3.8-live-extended-thinking', False),
    (False, 'Aoede', True, 'gemini-3.8-live-extended-thinking', False),
    (False, None, False, 'gemini-3.8-live-extended-thinking', False),
    (False, None, True, 'gemini-3.1-flash-live-preview', False),
])
def test_setup_gates_tool_and_prompt_together(monkeypatch, native, voice, expression, model, enabled):
    monkeypatch.setattr(config, 'REALTIME_NATIVE_AUDIO', native)
    monkeypatch.setattr(config, 'REALTIME_GEMINI_MODEL', model)
    orch = object.__new__(RealtimeOrchestrator)
    orch._expression_enabled = expression
    orch._voice_override = lambda: voice
    orch._tools = [DELEGATE_TOOL] + ([EMOTION_TOOL] if expression else [])
    with patch('hal.realtime.voice_agent.gemini_live.GeminiLiveAgent') as cls:
        agent = orch._make_agent('gemini', 'Original routing rules.')
    assert agent._emotion_markers_enabled is enabled
    kwargs = cls.call_args.kwargs
    names = [t['name'] for t in kwargs['tools']]
    assert ('express_emotion' in names) == (expression and not enabled)
    assert 'delegate_to_main' in names
    assert ('Physical expression through inline markers' in kwargs['config'].instructions) == enabled


def test_prompt_preserves_routing_and_replaces_tool_instruction():
    text = marker_instructions('Routing stays.\n* **express_emotion (only if the tool exists):** old tool rule')
    assert 'Routing stays.' in text
    assert 'old tool rule' not in text


@pytest.mark.parametrize('old_markers,native_voice', [(True, 'Kore'), (False, None)])
def test_mode_switch_rebuilds_even_with_same_voice(monkeypatch, old_markers, native_voice):
    import time
    monkeypatch.setattr(config, 'REALTIME_PROVIDER', 'gemini')
    monkeypatch.setattr(config, 'REALTIME_GEMINI_MODEL', 'gemini-3.8-live-extended-thinking')
    monkeypatch.setattr(config, 'REALTIME_NATIVE_AUDIO', False)
    monkeypatch.setattr(config, 'REALTIME_GEMINI_VOICE', 'Kore')
    orch = object.__new__(RealtimeOrchestrator)
    orch._expression_enabled = True
    orch._voice_override = lambda: native_voice
    orch._skip_post_idle_recycle = False
    orch._last_turn_monotonic = time.monotonic()
    orch._agent = SimpleNamespace(_config=SimpleNamespace(voice='Kore'),
                                 requires_fresh_session=False,
                                 _emotion_markers_enabled=old_markers)
    orch._rebuild_now = Mock(return_value=True)
    orch.prepare_turn()
    orch._rebuild_now.assert_called_once_with('gemini-expression-mode-change', discard_old_on_failure=True)


def test_interrupt_discards_partial_marker_before_next_answer(monkeypatch):
    from hal.realtime.models.output import InterruptedOutput
    monkeypatch.setattr(config, 'REALTIME_SESSION_MAX_TURNS', 0)
    orch = object.__new__(RealtimeOrchestrator)
    interrupted = InterruptedOutput(reason='server_interrupt')
    agent = Mock(execution_completed=True, execution_turn_id='next', _emotion_markers_enabled=True)
    agent.receive.return_value = iter([TextOutput(text=MARKER[:20], user_turn_id='old'),
                                      interrupted,
                                      TextOutput(text=MARKER + 'Hello', user_turn_id='next')])
    orch._agent = agent
    orch._skip_post_idle_recycle = False
    orch._consecutive_silent = 0
    orch._turns_since_recycle = 0
    orch._idle_reset_pending = False
    orch._fire_marker_emotion = Mock()
    assert list(orch.stream_output()) == [interrupted, TextOutput(text='Hello', user_turn_id='next')]
    orch._fire_marker_emotion.assert_called_once_with('happy', 0.8)
