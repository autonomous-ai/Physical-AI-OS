"""Wake idle time belongs to the user after an authorized reply drains."""

import pytest

from hal.drivers.voice._internal.wakeword_focus import WakeWordFocus
from hal.test.test_voice_metrics import kpi  # noqa: F401


def fixture():
    now = [0.0]
    pending = set()
    return WakeWordFocus(20, clock=lambda: now[0], pending_speech=pending.__contains__), now, pending


@pytest.mark.parametrize('owner', ['run:voice', 'interaction:voice'])
def test_long_realtime_reply_leaves_full_window_after_last_audio(owner):
    focus, now, pending = fixture()
    focus.begin('voice')
    now[0] = 25
    assert focus.is_active()  # Vision/grounding still working, past old deadline.
    focus.playback_finished()  # A filler ended, processing still owns the hold.
    pending.add(owner)
    focus.finish('voice')
    now[0] = 45
    assert focus.is_active()  # Synthesis and all queued chunks count too.
    pending.clear()
    focus.playback_finished()
    now[0] = 64.9
    assert focus.is_active()
    now[0] = 65
    assert not focus.is_active()


def test_main_terminal_waits_for_owned_playback_and_ignores_other_tts():
    focus, now, pending = fixture()
    focus.begin('voice')
    assert focus.activity('voice', 'main', 'start')
    focus.finish('voice')  # HTTP receipt is not task completion.
    now[0] = 40
    focus.playback_finished()  # Main filler cannot start idle timer.
    assert focus.is_active()
    pending.update(['run:main', 'run:web'])
    assert focus.activity('voice', 'main', 'end')
    now[0] = 60
    assert focus.is_active()
    pending.remove('run:main')
    focus.playback_finished()
    now[0] = 80
    assert not focus.is_active()  # Unrelated web playback cannot hold focus.
    pending.clear()
    focus.playback_finished()
    assert not focus.is_active()


@pytest.mark.parametrize('phase', ['start', 'end', 'cancel'])
def test_unknown_main_event_never_opens_wake(phase):
    focus, _, pending = fixture()
    pending.add('run:web')
    assert not focus.activity('unknown', 'web', phase)
    focus.playback_finished()
    assert not focus.is_active()


def test_cancelled_turn_cannot_be_revived_by_late_completion_or_start():
    focus, now, pending = fixture()
    focus.begin('old')
    focus.activity('old', 'main', 'start')
    focus.finish('old')
    focus.activity('old', 'main', 'cancel')
    assert not focus.begin('old')
    assert not focus.activity('old', 'main', 'start')
    now[0] = 50
    focus.activity('old', 'main', 'end')
    pending.clear()
    focus.playback_finished()
    assert not focus.is_active()
    focus.begin('new')
    assert focus.is_active()


def test_timeout_zero_and_lost_terminals_do_not_keep_wake_forever():
    disabled = WakeWordFocus(0)
    assert not disabled.begin('voice')
    assert not disabled.activity('voice', 'main', 'start')
    assert not disabled.is_active()
    focus, now, _ = fixture()
    focus.begin('voice')
    focus.activity('voice', 'main', 'start')
    focus.finish('voice')
    now[0] = 301
    assert not focus.is_active()
    assert not focus.activity('voice', 'main', 'end')


def test_silent_reply_starts_idle_at_completion_and_duplicates_do_not_extend():
    focus, now, _ = fixture()
    focus.begin('voice')
    now[0] = 10
    focus.finish('voice')
    now[0] = 20
    focus.finish('voice')
    focus.playback_finished()
    now[0] = 30
    assert not focus.is_active()


def test_mute_clear_ignores_late_tts_and_main():
    focus, _, pending = fixture()
    focus.begin('voice')
    focus.activity('voice', 'main', 'start')
    pending.add('run:main')
    focus.clear()
    pending.clear()
    focus.playback_finished()
    assert not focus.activity('voice', 'main', 'end')
    assert not focus.is_active()


@pytest.mark.parametrize('wake_enabled', [False, True])
@pytest.mark.parametrize('initial_focus', [False, True])
@pytest.mark.parametrize('noise', [False, True])
def test_capture_gate_and_reply_drain(monkeypatch, wake_enabled, initial_focus, noise):
    from unittest.mock import Mock, patch
    from hal.drivers.voice import voice_service as module
    from hal.drivers.voice._internal.realtime_turn import RealtimeTurnResult

    focus, now, pending = fixture()
    if initial_focus:
        focus.refresh()
    request = 'What am I wearing today?'
    service = Mock()
    service._running = False
    service._tts = None
    service._wakeword_focus = focus
    service._realtime.available = True
    service._realtime.rebuilding = False
    service._decorator.classify_wake_word.return_value = (request, 'voice')
    service._decorator.starts_with_wake_word.return_value = False
    service._decorator.identify_and_decorate.return_value = (request, None, None)
    stt = Mock()
    stt.is_closed.return_value = False
    monkeypatch.setattr(module.hal_config, 'REALTIME_ENABLED', True)
    monkeypatch.setattr(module.hal_config, 'WAKEWORD_ENABLED', wake_enabled)
    monkeypatch.setattr(module.voice_cfg, 'LIVE_MODE', False)
    expected = not wake_enabled or initial_focus

    def answer(*args, **kwargs):
        if noise:
            return RealtimeTurnResult(rejected=True)
        now[0] = 25
        assert focus.is_active() is (wake_enabled and initial_focus)
        pending.add('run:voice')
        return RealtimeTurnResult(handled=True, transcript='Your shirt is yellow.')

    with patch.object(module, 'finalize_session', return_value=(request, [], 2.0)), \
         patch.object(module, 'is_noise_turn', return_value=noise), \
         patch.object(module, 'run_realtime_turn', side_effect=answer) as realtime, \
         patch.object(module, '_WaitFiller'), \
         patch.object(module, 'dispatch_turn'), \
         patch.object(module, 'voice_metrics') as metrics, \
         patch.object(module.requests, 'post'):
        metrics.speech_end.return_value = 'voice'
        module.VoiceService._stream_session(service, Mock(), 320, 16000,
            preconnected_session=stt, harness_voice={'enabled': False, 'generation': 1})
    assert realtime.call_count == int(expected)
    now[0] = 50
    assert focus.is_active() is (wake_enabled and initial_focus and not noise)
    pending.clear()
    focus.playback_finished()
    if wake_enabled and initial_focus and not noise:
        now[0] = 69.9
        assert focus.is_active()
    now[0] = 70
    assert not focus.is_active()


@pytest.mark.parametrize('native', [False, True])
def test_live_reply_drains_before_countdown(monkeypatch, kpi, native):
    from hal import config
    from hal.realtime.models.output import UserSpeechOutput, TextOutput, ExecutionOutput
    from hal.test.test_live_voice_metrics import _pump

    monkeypatch.setattr(config, 'WAKEWORD_ENABLED', True)
    focus, now, pending = fixture()
    def after_generation():
        now[0] = 35
        assert focus.is_active()
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id='u', transcript='Hello lamp'),
        TextOutput(text='Your shirt is yellow', user_turn_id='u'),
        ExecutionOutput(user_turn_id='u', execution_completed=True),
        after_generation,
    ], 'u', True)], native=native, focus=focus)
    now[0] = 54.9
    assert focus.is_active()
    now[0] = 55
    assert not focus.is_active()


@pytest.mark.parametrize('kind', ['interrupt', 'failed'])
def test_live_cancel_does_not_leave_processing_hold(monkeypatch, kpi, kind):
    from hal import config
    from hal.realtime.models.output import UserSpeechOutput, InterruptedOutput, ExecutionOutput
    from hal.test.test_live_voice_metrics import _pump

    monkeypatch.setattr(config, 'WAKEWORD_ENABLED', True)
    focus, now, _ = fixture()
    terminal = (InterruptedOutput(user_turn_id='u', reason='server_interrupt') if kind == 'interrupt'
                else ExecutionOutput(user_turn_id='u', execution_completed=False))
    def while_session_still_open():
        now[0] = 35
        assert not focus.is_active()
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id='u', transcript='Hello lamp'), terminal,
        while_session_still_open,
    ], 'u', False)], focus=focus)
    assert not focus.is_active()


def test_retained_main_queue_keeps_focus_across_native_stop():
    import threading
    from types import SimpleNamespace
    from hal.drivers.voice.tts.service import TTSService

    tts = object.__new__(TTSService)
    tts._pending_queue_lock = threading.Lock()
    tts._stop_event = threading.Event()
    tts._stop_event.set()
    tts._speaking = False
    tts._pending_queue = [SimpleNamespace(owner='run:main')]
    now = [0.0]
    focus = WakeWordFocus(20, clock=lambda: now[0], pending_speech=tts.has_followup_speech)
    focus.begin('voice')
    focus.activity('voice', 'main', 'start')
    focus.finish('voice')
    focus.activity('voice', 'main', 'end')
    now[0] = 40
    focus.playback_finished()  # Interrupted native callback, main resumes next.
    assert focus.is_active()
    tts._pending_queue.clear()
    focus.playback_finished()
    now[0] = 59
    assert focus.is_active()
    now[0] = 60
    assert not focus.is_active()



def test_silent_live_opener_retains_authorization_for_main_fallback(monkeypatch, kpi):
    from hal import config
    from hal.realtime.models.output import UserSpeechOutput
    from hal.test.test_live_voice_metrics import _pump
    from hal.test.test_live_opener import opener_context

    monkeypatch.setattr(config, 'WAKEWORD_ENABLED', True)
    focus, now, _ = fixture()
    opener = opener_context(kpi)
    _pump(monkeypatch, kpi, [([UserSpeechOutput(turn_id='u')], 'u', True)],
          focus=focus, opener=opener)
    assert not opener['consumed']
    now[0] = 30
    assert focus.is_active()
    assert focus.activity(opener['interaction_id'], 'main', 'start')
    focus.finish(opener['interaction_id'])
    now[0] = 50
    assert focus.is_active()
    focus.activity(opener['interaction_id'], 'main', 'end')
    now[0] = 69
    assert focus.is_active()
    now[0] = 70
    assert not focus.is_active()


@pytest.mark.parametrize('enabled', [False, True])
def test_main_activity_obeys_wake_config(monkeypatch, enabled):
    from hal import config
    from hal.drivers.voice.voice_service import VoiceService

    focus, now, _ = fixture()
    service = object.__new__(VoiceService)
    service._wakeword_focus = focus
    monkeypatch.setattr(config, 'WAKEWORD_ENABLED', enabled)
    focus.begin('voice')
    assert service.followup_activity('voice', 'main', 'start') is enabled
    assert not service.followup_activity('unknown', 'other', 'start')
    focus.finish('voice')
    now[0] = 25
    assert service.conversation_focus_active() is enabled


def test_cancel_removes_only_its_own_completed_idle_window():
    focus, now, _ = fixture()
    focus.begin('old')
    focus.activity('old', 'main', 'start')
    focus.finish('old')
    focus.activity('old', 'main', 'end')
    assert focus.is_active()
    focus.activity('old', 'main', 'cancel')
    assert not focus.is_active()
    focus.begin('new')
    focus.finish('new')
    focus.activity('old', 'main', 'cancel')
    assert focus.is_active()
    now[0] = 20
    assert not focus.is_active()
