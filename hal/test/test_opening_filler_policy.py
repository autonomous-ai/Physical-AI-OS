"""Opening feedback must not leak into already-open automatic conversations."""

from unittest.mock import Mock

import pytest

from hal import presets
from hal.drivers.tracking import gaze
from hal.drivers.voice import voice_service as voice
from hal.drivers.voice._internal import realtime_turn, sensing_sender, turn_dispatch
from hal.drivers.voice._internal.wakeword_focus import WakeWordFocus
from hal.realtime.models import TextOutput


@pytest.mark.parametrize(
    'entry,active,wake,enabled,expire,suppressed',
    [
        (False, False, True, True, False, False),  # Spoken opener.
        (False, True, False, True, False, False),  # Gaze opened after entry.
        (True, True, False, True, False, True),  # Ordinary follow-up.
        (True, True, True, True, False, True),  # Repeating wake does not reopen.
        (True, True, False, True, True, True),  # Expiry during speech stays quiet.
        (True, True, False, False, False, False),  # Other profiles unchanged.
        (None, True, True, True, 'before', False),  # Expired window needs a new opener.
        (True, True, False, True, 'refresh', True),  # Gaze refresh does not reopen.
    ],
)
def test_capture_keeps_listening_but_only_openers_get_audio_feedback(
    monkeypatch, entry, active, wake, enabled, expire, suppressed,
):
    clock = [0.0]
    focus = WakeWordFocus(5, clock=lambda: clock[0])
    if active:
        focus.refresh()
    if expire == 'before':
        clock[0] = 6.0
    request = ('Hello Lamp, ' if wake else '') + 'can you tell me the weather tomorrow?'
    service = Mock()
    service._running = False
    service._tts = None
    service._wakeword_focus = focus
    service._realtime.available = True
    service._realtime.rebuilding = False
    service._decorator.starts_with_wake_word.side_effect = lambda t: t.lower().startswith('hello lamp')
    service._decorator.classify_wake_word.return_value = (request, 'voice_command' if wake else 'voice')
    service._decorator.identify_and_decorate.return_value = (request, None, None)
    stt = Mock()
    stt.is_closed.return_value = False
    service._stt.create_session.return_value = stt

    def start(callback):
        if expire is True:
            clock[0] = 6.0
        elif expire == 'refresh':
            focus.refresh()
        callback(request, False)
        callback(request, True)
        return True

    stt.start.side_effect = start
    monkeypatch.setattr(voice.hal_config, 'VOICE_OPENING_FILLERS_ONLY', enabled)
    monkeypatch.setattr(voice.hal_config, 'REALTIME_ENABLED', True)
    monkeypatch.setattr(voice.hal_config, 'WAKEWORD_ENABLED', True)
    monkeypatch.setattr(voice.voice_cfg, 'LIVE_MODE', False)
    monkeypatch.setattr(gaze, 'on_speech_end', lambda: None)
    monkeypatch.setattr(voice, 'finalize_session', lambda *a: (request, [], 2.0))
    monkeypatch.setattr(voice, 'is_noise_turn', lambda *a, **kw: False)
    realtime = Mock(return_value=realtime_turn.RealtimeTurnResult())
    dispatch, filler = Mock(), Mock()
    monkeypatch.setattr(voice, 'run_realtime_turn', realtime)
    monkeypatch.setattr(voice, 'dispatch_turn', dispatch)
    monkeypatch.setattr(voice, '_WaitFiller', Mock(return_value=filler))
    monkeypatch.setattr(voice, 'voice_metrics', Mock())
    monkeypatch.setattr(voice.requests, 'post', Mock())

    voice.VoiceService._stream_session(
        service, Mock(), 320, 16000,
        harness_voice={'enabled': False, 'generation': 1},
        wake_focus_at_entry=entry,
    )

    service._set_emotion_local.assert_any_call(presets.EMO_LISTENING)
    assert service._backchannel.on_partial.called is (not suppressed)
    realtime.assert_called_once()
    assert realtime.call_args.kwargs['suppress_auto_fillers'] is suppressed
    dispatch.assert_called_once()
    assert dispatch.call_args.kwargs['suppress_auto_fillers'] is suppressed
    assert filler.arm.called is (not suppressed)


@pytest.mark.parametrize('suppressed', [False, True])
def test_realtime_still_commits_and_answers_without_wait_filler(monkeypatch, suppressed):
    monkeypatch.setattr(realtime_turn.hal_config, 'REALTIME_ENABLED', True)
    monkeypatch.setattr(realtime_turn.hal_config, 'REALTIME_NATIVE_AUDIO', False)
    monkeypatch.setattr(realtime_turn.hal_config, 'REALTIME_PROVIDER', 'gemini')
    monkeypatch.setattr(realtime_turn, 'gemini_needs_idle_workaround', lambda: False)
    monkeypatch.setattr(realtime_turn, '_reply_language_name', lambda: 'English')
    monkeypatch.setattr(realtime_turn, '_thinking_cue_start', Mock())
    monkeypatch.setattr(realtime_turn, '_thinking_cue_clear', Mock())
    realtime, tts, filler = Mock(available=True), Mock(), Mock()
    realtime.stream_output.return_value = iter([TextOutput(text='Tomorrow will be sunny.')])
    result = realtime_turn.run_realtime_turn(
        realtime, tts, lambda text: text, 'What is the weather like tomorrow?',
        [b'audio'], 1.0, wait_filler=filler, harness_followup=False,
        suppress_auto_fillers=suppressed,
    )
    assert result.handled
    realtime.commit_audio.assert_called_once_with()
    tts.speak.assert_called_once()
    assert filler.arm.called is (not suppressed)


@pytest.mark.parametrize('route', ['fallback', 'delegate', 'handled'])
@pytest.mark.parametrize('suppressed', [False, True])
def test_main_handoff_preserves_policy_in_actual_http_payload(monkeypatch, route, suppressed):
    request = 'Please check the weather tomorrow'
    decorator = Mock()
    decorator.classify_wake_word.return_value = (request, 'voice_followup')
    decorator.identify_and_decorate.return_value = (request, None, None)
    result = realtime_turn.RealtimeTurnResult(
        delegated=route == 'delegate', handled=route == 'handled',
        delegate_msg='Check weather' if route == 'delegate' else '',
        transcript='Sunny tomorrow.' if route == 'handled' else '',
    )
    post = Mock(return_value=Mock(status_code=200, json=lambda: {'data': {'run_id': 'main'}}))
    monkeypatch.setattr(sensing_sender.requests, 'post', post)
    monkeypatch.setattr(turn_dispatch, '_take_vision_handoff', lambda **kw: ('', ''))
    monkeypatch.setattr(turn_dispatch, '_take_look_snapshot_marker', lambda: '')
    monkeypatch.setattr(turn_dispatch, 'voice_metrics', Mock())
    turn_dispatch.dispatch_turn(
        decorator, sensing_sender.SensingSender(), request, [], [], result,
        interaction_id='turn', suppress_auto_fillers=suppressed,
    )
    post.assert_called_once()
    payload = post.call_args.kwargs['json']
    assert payload.get('suppress_auto_fillers', False) is suppressed
    assert payload['interaction_id'] == 'turn'
    assert payload['type'] == ('voice_agent_handled' if route == 'handled' else 'voice_followup')
    assert payload['voice_turn_type'] == 'voice_followup'


@pytest.mark.parametrize('initial_focus', [False, True])
def test_vad_latches_focus_before_gaze_opens_or_refreshes(monkeypatch, initial_focus):
    import numpy as np
    from hal import app_state

    focus = WakeWordFocus(5)
    if initial_focus:
        focus.refresh()
    service = Mock()
    service._running = True
    service._np = np
    service._wakeword_focus = focus
    service._tts_is_speaking.return_value = False
    service._music_is_playing.return_value = False
    service._backchannel.self_audio_active = False
    service._hardware_aec_live_entry.return_value = False
    service._vad_entry_is_speech.return_value = True
    service._silero_vad = None
    service._stream_session.return_value = True  # Exit after the first handoff.
    monkeypatch.setattr(voice.hal_config, 'WAKEWORD_ENABLED', True)
    monkeypatch.setattr(voice.hal_config, 'VOICE_INPUT_MODE', 'automatic')
    monkeypatch.setattr(voice.voice_cfg, 'STT_KEEPALIVE', False)
    monkeypatch.setattr(voice.voice_cfg, 'LIVE_MODE', False)
    monkeypatch.setattr(voice.voice_cfg, 'SPEECH_HOLDOFF_S', 0)
    monkeypatch.setattr(voice, 'read_voice_mode', lambda: {'enabled': False, 'generation': 1})
    gaze_start = Mock(side_effect=lambda: (focus.refresh(), True)[1])
    monkeypatch.setattr(gaze, 'on_speech_start', gaze_start)
    pending = Mock(return_value='gaze-listening')
    monkeypatch.setattr(app_state, 'show_listening_pending_cue', pending)
    mic = Mock()
    mic.read.return_value = (np.full((320, 1), 800, np.int16), False)

    voice.VoiceService._vad_loop(service, mic, 320, 16000)

    gaze_start.assert_called_once()
    pending.assert_called_once()
    assert focus.is_active()
    service._stream_session.assert_called_once()
    call = service._stream_session.call_args
    assert call.kwargs['wake_focus_at_entry'] is initial_focus
    assert call.kwargs['pending_listening_cue_id'] == 'gaze-listening'
    assert call.kwargs['speech_pre_buffer']


@pytest.mark.parametrize('mode', ['manual', 'harness', 'live'])
def test_existing_explicit_and_live_routes_do_not_inherit_followup_suppression(monkeypatch, mode):
    import threading
    from types import SimpleNamespace

    request = 'Please tell me the weather tomorrow'
    focus = WakeWordFocus(5)
    focus.refresh()
    service = Mock()
    service._running = mode != 'live'
    service._tts = None
    service._wakeword_focus = focus
    service._tts_is_speaking.return_value = False
    service._music_is_playing.return_value = False
    service._decorator.starts_with_wake_word.return_value = False
    service._decorator.classify_wake_word.return_value = (request, 'voice')
    service._decorator.identify_and_decorate.return_value = (request, None, None)
    service._try_live_opener.return_value = (False, False)
    snapshot = {'enabled': mode == 'harness', 'generation': 1, 'focusAvailable': True}
    if mode == 'manual':
        snapshot['deviceInputMode'] = 'tap_to_talk'
    capture = None
    if mode != 'live':
        # Admission observes an unfinished capture; the next poll receives a tap.
        capture = SimpleNamespace(cancelled=threading.Event(), finished=Mock())
        capture.finished.is_set.side_effect = [False, True]
    stt = Mock()
    stt.is_closed.return_value = False
    stt.close.side_effect = lambda: stt._on_transcript_cb(request, True)
    monkeypatch.setattr(voice.hal_config, 'VOICE_OPENING_FILLERS_ONLY', True)
    monkeypatch.setattr(voice.hal_config, 'VOICE_INPUT_MODE', 'tap_to_talk' if mode == 'manual' else 'automatic')
    monkeypatch.setattr(voice.hal_config, 'REALTIME_ENABLED', True)
    monkeypatch.setattr(voice.hal_config, 'WAKEWORD_ENABLED', True)
    monkeypatch.setattr(voice.voice_cfg, 'LIVE_MODE', mode == 'live')
    monkeypatch.setattr(voice, 'read_voice_mode', lambda: snapshot)
    monkeypatch.setattr(gaze, 'on_speech_end', lambda: None)
    monkeypatch.setattr(voice, 'finalize_session', lambda *a: (request, [], 2.0))
    monkeypatch.setattr(voice, 'is_noise_turn', lambda *a, **kw: False)
    monkeypatch.setattr(voice, 'voice_metrics', Mock())
    monkeypatch.setattr(voice.requests, 'post', Mock())
    dispatch, realtime = Mock(), Mock()
    monkeypatch.setattr(voice, 'dispatch_turn', dispatch)
    monkeypatch.setattr(voice, 'run_realtime_turn', realtime)

    voice.VoiceService._stream_session(
        service, Mock(), 320, 16000, preconnected_session=stt,
        harness_voice=snapshot, manual_capture=capture, wake_focus_at_entry=True,
    )

    dispatch.assert_called_once()
    assert dispatch.call_args.kwargs['suppress_auto_fillers'] is False
    realtime.assert_not_called()
    if mode != 'live':
        service._backchannel.on_partial.assert_not_called()
    else:
        service._try_live_opener.assert_called_once()
