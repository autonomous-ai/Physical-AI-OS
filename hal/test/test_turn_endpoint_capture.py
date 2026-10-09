"""Exercise hands-free endpoint policy through the real microphone capture loop."""
from contextlib import contextmanager
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest


@contextmanager
def capture(monkeypatch, frames, *, realtime=False, enabled=True, detector=None, legacy_limit=20,
            tts=None, on_read=None, on_close=None, on_connect=None, on_realtime=None,
            wake_enabled=False, focus=None, transcripts_final=True, close_transcript=None,
            on_prepare=None, on_drain=None, pending_cue=None, realtime_available=True,
            silence_vad=False, speech_windows=None, configure_service=None):
    """Feed (elapsed seconds, speech energy, final transcript) without hardware."""
    from hal.drivers.voice import voice_service as module

    clock = [1000.0]
    service = Mock()
    service._running = True
    service._stt_drain_worker = None
    service._stt_drain_future = None
    service._automatic_reply_lock = threading.Lock()
    service._automatic_reply_stop = None
    service._automatic_reply_cancelled_at = None
    service._np = np
    service._tts = Mock(last_spoken_text="")
    service._tts_is_speaking.return_value = False
    if tts is not None:
        service._tts = tts
        service._tts_is_speaking.side_effect = lambda: tts.speaking
    service._music_is_playing.return_value = False
    service._turn_detector = detector
    service._silence_window_is_speech.side_effect = speech_windows
    service._realtime.available = realtime_available
    service._realtime.rebuilding = False
    service._realtime.sample_rate = 16000
    if on_prepare is not None:
        service._realtime.prepare_turn.side_effect = on_prepare
    service._wakeword_focus.is_active.side_effect = focus or (lambda: False)
    service._decorator.starts_with_wake_word.return_value = False
    service._decorator.matches_wake_word_loosely.return_value = False
    service._decorator.classify_wake_word.side_effect = lambda text: (text, "voice")
    service._decorator.identify_and_decorate.side_effect = lambda text, *a, **kw: (text, None, None)
    stt = Mock()
    stt.is_closed.return_value = False
    def close():
        if on_close:
            on_close()
        if on_drain:
            on_drain(stt, service)
        if close_transcript:
            stt._on_transcript_cb(close_transcript, True)

    stt.close.side_effect = close
    if on_connect is not None:
        service._stt.create_session.return_value = stt

        def start(callback):
            stt._on_transcript_cb = callback
            on_connect()
            return True

        stt.start.side_effect = start

        class ConnectWorker:
            def __init__(self, target, **_):
                self.target = target

            def start(self):
                self.target()

            def is_alive(self):
                return False

        monkeypatch.setattr(module, "threading", SimpleNamespace(Thread=ConnectWorker, Event=threading.Event))
    if configure_service is not None:
        configure_service(service)
    remaining = iter(frames)
    consumed = []

    def read(_):
        try:
            elapsed, speech, transcript = next(remaining)
        except StopIteration:
            pytest.fail("Capture read beyond its expected endpoint")
        clock[0] = 1000.0 + elapsed
        consumed.append(elapsed)
        if on_read is not None:
            on_read(elapsed)
        if transcript:
            text, final = transcript if isinstance(transcript, tuple) else (transcript, transcripts_final)
            stt._on_transcript_cb(text, final)
        return np.full((1024, 1), 10000 if speech else 0, dtype=np.int16), False

    mic = Mock()
    mic.read.side_effect = read
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))
    monkeypatch.setattr(module.hal_config, "WAKEWORD_ENABLED", wake_enabled)
    monkeypatch.setattr(module.hal_config, "REALTIME_ENABLED", realtime)
    for name, value in {
        "LIVE_MODE": False, "TURN_END_ENABLED": enabled,
        "SILENCE_VAD_ENABLED": silence_vad, "SILENCE_VAD_WINDOW_FRAMES": 3,
        "MAX_SESSION_DURATION_S": legacy_limit,
        "TURN_END_MAX_DURATION_S": 180, "TURN_END_FALLBACK_S": 2.5,
        "TURN_END_MAX_PAUSE_S": 6.0,
        "SILENCE_TIMEOUT_S": 2.5, "ENDPOINT_SILENCE_S": 0.8,
    }.items():
        monkeypatch.setattr(module.voice_cfg, name, value)
    with patch.object(module, "turn_should_close", return_value=True,
                      side_effect=module.turn_should_close if silence_vad else None), \
         patch.object(module, "dispatch_turn") as dispatch, \
         patch.object(module, "run_realtime_turn", side_effect=on_realtime,
                      return_value=module.RealtimeTurnResult()) as rt, \
         patch.object(module, "voice_metrics") as metrics, \
         patch.object(module, "build_turn_context", return_value="test context"), \
         patch.object(module, "_WaitFiller") as wait_filler, \
         patch.object(module.requests, "post"), \
         patch("hal.drivers.tracking.gaze.on_speech_end"):
        module.VoiceService._stream_session(
            service, mic, 1024, 16000,
            preconnected_session=stt if on_connect is None else None, harness_voice={"enabled": False},
            pending_listening_cue_id=pending_cue,
        )
        yield SimpleNamespace(service=service, stt=stt, dispatch=dispatch,
                              realtime=rt, metrics=metrics, consumed=consumed,
                              wait_filler=wait_filler)


def test_incomplete_pause_keeps_one_session_and_merges_final_segments(monkeypatch):
    detector = Mock(failed=False)
    detector.submit.return_value = True
    detector.poll.side_effect = [False, True]
    frames = [
        (1, True, "Please arrange the trip"),
        (4, False, None),
        (5, True, "and reserve a hotel"),
        (8, False, None),
    ]
    with capture(monkeypatch, frames, detector=detector) as result:
        assert result.consumed == [1, 4, 5, 8]
        result.service._stt.create_session.assert_not_called()
        result.stt.close.assert_called_once()
        assert result.stt.send_audio.call_count == 4
        result.dispatch.assert_called_once()
        assert result.dispatch.call_args.args[2] == "Please arrange the trip and reserve a hotel"
        assert detector.submit.call_count == 2
        tokens = [call.args[0] for call in detector.submit.call_args_list]
        assert tokens[0] != tokens[1]
        assert result.metrics.speech_end.call_args.args[0] == "smart_turn"


def test_unchanged_final_refreshes_model_with_recorded_quiet_tail(monkeypatch):
    detector = Mock(failed=False)
    detector.submit.return_value = True
    detector.poll.side_effect = [False, True]
    words = "Help me check what we talked about today."
    frames = [(1, True, (words, False))]
    frames += [(2 + i / 10, False, None) for i in range(7)]
    frames += [(2.7, False, (words, True))]
    with capture(monkeypatch, frames, detector=detector) as result:
        assert result.consumed[-1] == 2.7
        assert detector.submit.call_count == 2
        first, refreshed = detector.submit.call_args_list
        assert first.args[0] != refreshed.args[0]
        # The new snapshot extends past four post-RMS frames, unlike the clipped PCM before.
        assert len(first.args[1]) == 2 * 2048
        assert len(refreshed.args[1]) == len(frames) * 2048
        result.stt.close.assert_called_once()
        result.dispatch.assert_called_once()
        assert result.dispatch.call_args.args[2] == words
        assert result.metrics.speech_end.call_args.args[0] == "smart_turn"


@pytest.mark.parametrize("legacy_limit", [20, 30])
def test_recognized_request_can_continue_for_two_minutes(monkeypatch, legacy_limit):
    frames = [(1, True, "Please plan a trip"), (21, True, None),
              (31, True, None), (121, True, "and include a hotel"), (124, False, None)]
    with capture(monkeypatch, frames, legacy_limit=legacy_limit) as result:
        assert result.consumed[-1] == 124
        result.dispatch.assert_called_once()
        assert result.dispatch.call_args.args[2] == "Please plan a trip and include a hotel"
        assert result.metrics.speech_end.call_args.args[0] == "turn_fallback"


@pytest.mark.parametrize("realtime", [False, True])
def test_hard_capture_limit_discards_unfinished_request(monkeypatch, realtime):
    frames = [(1, True, "Please plan a trip"), (121, True, "and"), (181, True, None)]
    with capture(monkeypatch, frames, realtime=realtime) as result:
        assert result.consumed[-1] == 181
        result.dispatch.assert_not_called()
        result.realtime.assert_not_called()
        result.service._realtime.commit_audio.assert_not_called()
        result.stt.close.assert_called_once()
        if realtime:
            result.service._realtime.append_audio.assert_called()
            result.service._realtime.discard_open_activity.assert_called_once()
        else:
            result.service._realtime.discard_open_activity.assert_not_called()


def test_disabling_endpoint_preserves_legacy_duration_limit(monkeypatch):
    with capture(monkeypatch, [(1, True, "Please plan a trip"), (21, True, None)], enabled=False) as result:
        assert result.consumed == [1, 21]
        assert result.metrics.speech_end.call_args.args[0] == "max_duration"
        result.dispatch.assert_called_once()


@pytest.mark.parametrize("enabled", [False, True])
def test_loud_non_speech_reaches_endpoint_after_completed_silero_window(monkeypatch, enabled):
    detector = Mock(failed=False)
    detector.submit.return_value = True
    detector.poll.return_value = True
    frames = [(1, True, "Do you think it will rain today?"),
              (1.064, True, None), (1.128, True, None),
              (4, True, None), (4.064, True, None), (4.128, True, None)]

    def before_classification(_):
        # Even after the silence deadline, partial loud windows cannot submit
        # a semantic endpoint until the classifier has rejected the full window.
        detector.submit.assert_not_called()

    with capture(monkeypatch, frames, enabled=enabled, detector=detector,
                 silence_vad=True, speech_windows=[True, False],
                 on_read=before_classification) as result:
        assert result.consumed == [frame[0] for frame in frames]
        assert result.service._silence_window_is_speech.call_count == 2
        result.dispatch.assert_called_once()
        assert result.dispatch.call_args.args[2] == frames[0][2]
        assert result.metrics.speech_end.call_args.args[0] == (
            "smart_turn" if enabled else "silence_clock"
        )
        assert detector.submit.call_count == int(enabled)


def test_loud_continuing_speech_refreshes_clock_before_noise_can_endpoint(monkeypatch):
    detector = Mock(failed=False)
    detector.submit.return_value = True
    detector.poll.return_value = True
    frames = [(1, True, "Please plan a trip"),
              (1.064, True, None), (1.128, True, None),
              (4, True, "and reserve a hotel"),
              (4.064, True, None), (4.128, True, None),
              (7, True, None), (7.064, True, None), (7.128, True, None)]
    with capture(monkeypatch, frames, detector=detector, silence_vad=True,
                 speech_windows=[True, True, False],
                 on_read=lambda _: detector.submit.assert_not_called()) as result:
        assert result.consumed == [frame[0] for frame in frames]
        assert result.service._silence_window_is_speech.call_count == 3
        detector.submit.assert_called_once()
        result.dispatch.assert_called_once()
        assert result.dispatch.call_args.args[2] == "Please plan a trip and reserve a hotel"
        assert result.metrics.speech_end.call_args.args[0] == "smart_turn"


def test_noise_endpoint_latency_is_bounded_by_existing_classifier_window(monkeypatch):
    endpoints = []
    for noisy in (False, True):
        detector = Mock(failed=False)
        detector.submit.return_value = True
        detector.poll.return_value = True
        frames = [(i * 0.064, i <= 3 or noisy,
                   "Do you think it will rain today?" if i == 1 else None)
                  for i in range(1, 46)]
        with capture(monkeypatch, frames, detector=detector, silence_vad=True,
                     speech_windows=[True] + [False] * 14) as result:
            result.dispatch.assert_called_once()
            detector.submit.assert_called_once()
            endpoints.append(result.consumed[-1])
    # Deterministic capture-clock measurement, excluding model inference time:
    # last speech = 192ms; quiet endpoints at 2752ms, noise at 2880ms.
    assert endpoints == pytest.approx([2.752, 2.880])
    assert 0 <= endpoints[1] - endpoints[0] <= 3 * 0.064


@pytest.mark.parametrize("followup", [False, True])
def test_confirmed_partial_wake_survives_final_name_correction(monkeypatch, followup):
    """An admitted Lamp -> lamb correction must not become ambient at dispatch."""
    words = "Hello lamb, what is two plus two?"

    def configure(service):
        service._decorator.starts_with_wake_word.side_effect = lambda text: text.lower().startswith("hello lamp")
        service._decorator.matches_wake_word_loosely.return_value = True

    frames = [(1, True, ("Hello Lamp", False)),
              (2, True, (words, True)), (5, False, None)]
    with capture(monkeypatch, frames, wake_enabled=True, focus=lambda: followup,
                 configure_service=configure) as result:
        result.dispatch.assert_called_once()
        assert result.dispatch.call_args.args[2] == words
        assert result.dispatch.call_args.kwargs["event_type_override"] == "voice_command"


@pytest.mark.parametrize("partial,loose", [(None, True), ("Hello Lamp", False)])
def test_unconfirmed_name_does_not_gain_command_route(monkeypatch, partial, loose):
    def configure(service):
        service._decorator.starts_with_wake_word.side_effect = lambda text: text.lower().startswith("hello lamp")
        service._decorator.matches_wake_word_loosely.return_value = loose

    frames = [(1, True, (partial, False) if partial else None),
              (2, True, ("Hello Laura, what is two plus two?", True)),
              (5, False, None)]
    with capture(monkeypatch, frames, wake_enabled=True,
                 configure_service=configure) as result:
        result.dispatch.assert_not_called()
