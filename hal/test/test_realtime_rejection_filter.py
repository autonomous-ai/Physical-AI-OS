"""Regression coverage for the explicit AI rejection gate.

Empty output is not proof of rejection. Explicit rejection and a completed,
marker-only Gemini silence decision can suppress the main-agent fallback.
"""

from unittest import mock

import pytest
from hal.test.test_voice_metrics import kpi  # noqa: F401

import hal.config as hal_config
from hal.drivers.voice._internal.realtime_turn import (
    ROUTE_AI_REJECTED,
    ROUTE_NOISE_DROPPED,
    RealtimeTurnResult,
    run_realtime_turn,
    should_defer_speaker_id_prepass,
    should_arm_realtime_wait_filler,
    should_drop_downstream_turn,
    should_drop_realtime_rejection,
)
from hal.drivers.voice._internal.turn_dispatch import dispatch_turn
from hal.realtime.models import FunctionCallOutput
from hal.realtime.models.signal import RejectSignal
from hal.realtime.orchestrator import RealtimeOrchestrator


class _Agent:
    def __init__(self) -> None:
        self.sent = []
        self.end_turn_calls = 0

    def receive(self, stop_on_done=True):
        del stop_on_done
        yield FunctionCallOutput(
            name="reject_turn",
            arguments="{}",
            call_id="reject-1",
        )

    def send(self, inputs) -> None:
        self.sent.append(inputs)

    def end_turn(self) -> None:
        self.end_turn_calls += 1


def _orchestrator_for_reject() -> tuple[RealtimeOrchestrator, _Agent]:
    """Build just enough state to exercise stream_output without a provider."""
    agent = _Agent()
    orchestrator = object.__new__(RealtimeOrchestrator)
    orchestrator._agent = agent
    orchestrator._looked_this_turn = False
    orchestrator._skip_post_idle_recycle = False
    orchestrator._consecutive_silent = 0
    orchestrator._last_turn_monotonic = 0.0
    orchestrator._turns_since_recycle = 0
    orchestrator._idle_reset_pending = False
    return orchestrator, agent


def test_reject_tool_emits_the_only_drop_signal(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_SESSION_MAX_TURNS", 0)
    orchestrator, agent = _orchestrator_for_reject()

    assert list(orchestrator.stream_output()) == [RejectSignal()]
    assert agent.end_turn_calls == 1
    assert agent.sent[0][0].output == '{"result": "turn dropped"}'


def test_only_an_explicit_reject_tool_result_drops_dispatch(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_AI_REJECT_FILTER", True)
    assert should_drop_realtime_rejection(
        RealtimeTurnResult(route=ROUTE_AI_REJECTED, rejected=True)
    )
    assert not should_drop_realtime_rejection(RealtimeTurnResult())


def test_noise_guard_drop_is_terminal_even_with_a_fabricated_transcript():
    assert should_drop_downstream_turn(RealtimeTurnResult(route=ROUTE_NOISE_DROPPED))


class _RejectingRealtime:
    available = True

    def flush_output(self) -> None:
        pass

    def commit_audio(self) -> None:
        pass

    def stream_output(self):
        yield RejectSignal()


def test_realtime_turn_preserves_the_explicit_rejection(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(hal_config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(
        "hal.drivers.voice._internal.realtime_turn._thinking_cue_start", lambda: None
    )
    monkeypatch.setattr(
        "hal.drivers.voice._internal.realtime_turn._thinking_cue_clear", lambda: None
    )
    result = run_realtime_turn(
        _RejectingRealtime(),
        None,
        lambda text: text,
        "you.",
        [object()],
        1.0,
    )
    assert result.route == ROUTE_AI_REJECTED
    assert result.rejected
    assert not result.handled
    assert not result.delegated


def test_filter_can_be_disabled_without_changing_the_result(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_AI_REJECT_FILTER", False)
    assert not should_drop_realtime_rejection(
        RealtimeTurnResult(route=ROUTE_AI_REJECTED, rejected=True)
    )


def test_short_ambiguous_transcripts_do_not_arm_an_audible_filler(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_NOISE_GUARD_MAX_WORDS", 3)
    assert not should_arm_realtime_wait_filler("o")
    assert not should_arm_realtime_wait_filler("you.")
    assert not should_arm_realtime_wait_filler("Yeah, exactly")
    assert should_arm_realtime_wait_filler("Do you like me?")


def test_short_transcript_defers_speaker_id_until_after_ai_verdict(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(hal_config, "REALTIME_AI_REJECT_FILTER", True)
    assert should_defer_speaker_id_prepass("o")
    assert should_defer_speaker_id_prepass("you.")
    assert not should_defer_speaker_id_prepass("Do you like me?")


class _Decorator:
    def classify_wake_word(self, combined):
        return combined, "voice"

    def identify_and_decorate(self, final_text, audio_buffer):
        return final_text, "leo", "Leo"

    def submit_speech_emotion_from_session(self, buf, user=None):
        pass


class _Sender:
    def __init__(self) -> None:
        self.sent = []

    def send(self, msg, **kwargs) -> None:
        self.sent.append((msg, kwargs))


def test_explicit_reject_does_not_send_the_transcript_to_os(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_AI_REJECT_FILTER", True)
    sender = _Sender()
    with mock.patch(
        "hal.drivers.voice._internal.turn_dispatch._take_vision_handoff",
        return_value=("", ""),
    ):
        dispatch_turn(
            _Decorator(),
            sender,
            "you.",
            [],
            [],
            RealtimeTurnResult(route=ROUTE_AI_REJECTED, rejected=True),
        )
    assert sender.sent == []


def test_noise_guard_drop_does_not_send_a_fabricated_transcript_to_os():
    sender = _Sender()
    with mock.patch(
        "hal.drivers.voice._internal.turn_dispatch._take_vision_handoff",
        return_value=("", ""),
    ):
        dispatch_turn(
            _Decorator(),
            sender,
            "take.",
            [],
            [],
            RealtimeTurnResult(route=ROUTE_NOISE_DROPPED),
        )
    assert sender.sent == []


def test_disabling_filter_restores_normal_main_agent_fallback(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_AI_REJECT_FILTER", False)
    sender = _Sender()
    with mock.patch(
        "hal.drivers.voice._internal.turn_dispatch._take_vision_handoff",
        return_value=("", ""),
    ):
        dispatch_turn(
            _Decorator(),
            sender,
            "you.",
            [],
            [],
            RealtimeTurnResult(route=ROUTE_AI_REJECTED, rejected=True),
        )
    assert sender.sent[0][0] == "you."


def test_live_late_reject_cancels_generated_text_without_success(monkeypatch):
    from hal.realtime.models import TextOutput

    monkeypatch.setattr(hal_config, "LIVE_MODE", True)
    monkeypatch.setattr(hal_config, "REALTIME_SESSION_MAX_TURNS", 0)
    orchestrator, agent = _orchestrator_for_reject()
    text = TextOutput(text="Rất tiếc, đã xảy ra một lỗi hệ thống.", user_turn_id="user-1")
    agent.receive = lambda **kwargs: iter([
        text, FunctionCallOutput(name="reject_turn", arguments="{}",
                                 call_id="reject-1", user_turn_id="user-1"),
        TextOutput(text="Unwanted acknowledgement.", user_turn_id="user-1"),
    ])
    agent.execution_completed = True
    assert list(orchestrator.stream_output()) == [text, RejectSignal(user_turn_id="user-1")]
    assert not orchestrator.execution_completed
    assert agent.end_turn_calls == 1
    assert agent.sent[0][0].output == '{"result": "turn dropped"}'


def test_manual_late_reject_preserves_existing_output_policy(monkeypatch):
    from hal.realtime.models import TextOutput

    monkeypatch.setattr(hal_config, "LIVE_MODE", False)
    monkeypatch.setattr(hal_config, "REALTIME_SESSION_MAX_TURNS", 0)
    orchestrator, agent = _orchestrator_for_reject()
    text = TextOutput(text="A real answer.")
    agent.receive = lambda **kwargs: iter([
        text, FunctionCallOutput(name="reject_turn", arguments="{}", call_id="reject-1"),
    ])
    assert list(orchestrator.stream_output()) == [text]
    assert agent.end_turn_calls == 1
    assert "error" in agent.sent[0][0].output


@pytest.mark.parametrize('parts,completed,expected', [
    (['<no ', 'speech>'], True, True),
    ([' <NO SPEECH>\n<no speech> '], True, True),
    (['<no speech>'], False, False),
    ([], True, False),
    (['<no spe'], True, False),
    (['<no speech>Sorry, a system error occurred.'], True, False),
    (['The marker is <no speech>.'], True, False),
    (['<no speech>Hello.'], True, False),
])
def test_only_complete_marker_only_output_is_intentional_silence(monkeypatch, parts, completed, expected):
    from hal.realtime.models import TextOutput

    monkeypatch.setattr(hal_config, 'REALTIME_PROVIDER', 'gemini')
    monkeypatch.setattr(hal_config, 'REALTIME_SESSION_MAX_TURNS', 0)
    orchestrator, agent = _orchestrator_for_reject()
    agent.execution_completed = completed
    agent.receive = lambda **kwargs: iter(TextOutput(text=p) for p in parts)
    list(orchestrator.stream_output())
    assert orchestrator.intentional_silence is expected
    # A later failed/empty receive must not inherit the previous decision.
    agent.execution_completed = False
    agent.receive = lambda **kwargs: iter(())
    list(orchestrator.stream_output())
    assert not orchestrator.intentional_silence


def test_marker_after_tool_does_not_discard_a_real_task(monkeypatch):
    from hal.realtime.models import TextOutput

    monkeypatch.setattr(hal_config, 'REALTIME_PROVIDER', 'gemini')
    monkeypatch.setattr(hal_config, 'REALTIME_SESSION_MAX_TURNS', 0)
    orchestrator, agent = _orchestrator_for_reject()
    agent.execution_completed = True
    agent.receive = lambda **kwargs: iter([
        FunctionCallOutput(name='express_emotion', arguments='{}', call_id='emotion'),
        TextOutput(text='<no speech>'),
    ])
    monkeypatch.setattr(orchestrator, '_handle_emotion_call', lambda *a, **kw: None)
    list(orchestrator.stream_output())
    assert not orchestrator.intentional_silence


@pytest.mark.parametrize('completed,filter_enabled,native', [
    (True, True, False), (False, True, False),
    (True, False, False), (True, True, True),
])
def test_silent_marker_replay_dispatch_and_kpi(monkeypatch, kpi, completed, filter_enabled, native):
    """Replay the split marker observed on lamp-4ace, not a text-length guess."""
    from hal.drivers.voice.voice_service import VoiceService
    from hal.realtime.models import TextOutput
    from hal.telemetry import voice_metrics

    monkeypatch.setattr(hal_config, 'REALTIME_ENABLED', True)
    monkeypatch.setattr(hal_config, 'REALTIME_PROVIDER', 'gemini')
    monkeypatch.setattr(hal_config, 'REALTIME_NATIVE_AUDIO', native)
    monkeypatch.setattr(hal_config, 'REALTIME_AI_REJECT_FILTER', filter_enabled)
    monkeypatch.setattr(hal_config, 'REALTIME_SESSION_MAX_TURNS', 0)
    monkeypatch.setattr('hal.drivers.voice._internal.realtime_turn._thinking_cue_start', lambda: None)
    monkeypatch.setattr('hal.drivers.voice._internal.realtime_turn._thinking_cue_clear', lambda: None)
    orchestrator, agent = _orchestrator_for_reject()
    agent.execution_completed = completed
    agent.receive = lambda **kwargs: iter([
        TextOutput(text='<no '), TextOutput(text='speech>'),
    ])

    class Replay(_RejectingRealtime):
        def stream_output(self):
            yield from orchestrator.stream_output()
            self.execution_completed = orchestrator.execution_completed
            self.intentional_silence = orchestrator.intentional_silence

    iid = voice_metrics.speech_end('smart_turn')
    tts = mock.Mock()
    tts.provider = 'elevenlabs'
    result = run_realtime_turn(Replay(), tts, VoiceService.strip_rt_markers,
                               'on check', [object()], 2.69, interaction_id=iid)
    sender = _Sender()
    with mock.patch('hal.drivers.voice._internal.turn_dispatch._take_vision_handoff', return_value=('', '')):
        dispatch_turn(_Decorator(), sender, 'on check', [], [], result, interaction_id=iid)
    tts.speak.assert_not_called()
    tts.speak_queue.assert_not_called()
    kpi.close_all()
    row = kpi.of(voice_metrics.EVENT_INTERACTION)[-1]['params']
    if completed and filter_enabled and not native:
        assert result.rejected and result.route == ROUTE_AI_REJECTED
        assert not result.execution_completed
        assert not sender.sent
        assert not row['eligible'] and row['exclusion_reason'] == 'rejected_non_user'
    else:
        assert not result.rejected or not filter_enabled
        assert sender.sent  # A failed receive keeps the original fallback.
        assert row['eligible']


def test_interrupted_marker_is_not_an_intentional_silent_completion(monkeypatch):
    from hal.realtime.models import TextOutput
    from hal.realtime.models.output import InterruptedOutput

    monkeypatch.setattr(hal_config, 'REALTIME_PROVIDER', 'gemini')
    monkeypatch.setattr(hal_config, 'REALTIME_SESSION_MAX_TURNS', 0)
    orchestrator, agent = _orchestrator_for_reject()
    agent.execution_completed = True
    agent.receive = lambda **kwargs: iter([
        TextOutput(text='<no speech>'), InterruptedOutput(reason='server_interrupt'),
    ])
    list(orchestrator.stream_output())
    assert not orchestrator.intentional_silence
