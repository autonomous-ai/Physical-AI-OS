"""Optional speech must not truncate an in-progress microphone capture."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hal.drivers import button_actions
from hal.drivers.voice.tts import service as tts_module
from hal.drivers.voice.tts.service import TTSService
from hal.test.test_turn_endpoint_capture import capture


@pytest.fixture
def tts(monkeypatch, tmp_path):
    service = object.__new__(TTSService)
    service._backend = SimpleNamespace(available=True)
    service._sd = object()
    service._lock = threading.Lock()
    service._input_capture_lock = threading.RLock()
    service._input_captures = set()
    service._input_capture_generation = 0
    service._stop_event = threading.Event()
    service._speaking = False
    service._interruptible = False
    service._last_spoken_text = ""
    service._realtime_feedback = False
    service._speaker_muted = lambda: False
    service._owner_suppressed = lambda _: False
    service._tts_cache_path = lambda _: tmp_path / "phrase.wav"
    service.stop = Mock()
    workers = []

    class Worker:
        def __init__(self, **kwargs):
            self.target = kwargs["target"]

        def start(self):
            workers.append(self.target)

    # Admission is real; no synthesis or hardware thread is allowed to run.
    monkeypatch.setattr(tts_module, "threading", SimpleNamespace(Thread=Worker, Lock=threading.Lock))
    service.workers = workers
    return service


@pytest.mark.parametrize("cached", [False, True])
def test_optional_speech_cannot_cut_capture_and_can_play_after_endpoint(monkeypatch, tts, cached):
    speak = tts.speak_cached if cached else tts.speak
    attempted = []

    def during_capture(elapsed):
        if elapsed == 1:
            attempted.append(speak("Hang on a bit.", interruptible=True))
            assert not tts.speaking
            assert not tts.workers

    def close():
        # The reservation must not hold up processing feedback during STT drain.
        assert not tts.input_capture_state[0]

    frames = [(1, True, "Help me"), (2, True, "check memory again"), (5, False, None)]
    with capture(monkeypatch, frames, tts=tts, on_read=during_capture, on_close=close) as result:
        assert attempted == [False]
        assert result.consumed == [1, 2, 5]
        assert result.dispatch.call_args.args[2] == "Help me check memory again"
        assert result.metrics.speech_end.call_args.args[0] != "tts_started"
    assert speak("Hang on a bit.", interruptible=True)
    assert len(tts.workers) == 1


def test_delayed_button_cue_does_not_stop_or_truncate_capture(monkeypatch, tts):
    monkeypatch.setattr(button_actions.state, "tts_service", tts)

    def during_capture(elapsed):
        if elapsed == 1:
            button_actions._announce_listening()

    frames = [(1, True, "Have you"), (2, True, "finished the task"), (5, False, None)]
    with capture(monkeypatch, frames, tts=tts, on_read=during_capture) as result:
        assert result.dispatch.call_args.args[2] == "Have you finished the task"
        assert result.consumed == [1, 2, 5]
    tts.stop.assert_not_called()
    assert not tts.workers


def test_filler_cannot_claim_speech_during_stt_connection(monkeypatch, tts):
    attempted = []

    def connect():
        attempted.append(tts.speak_cached("Hang on a bit.", interruptible=True))

    frames = [(1, True, "Help me check memory again"), (4, False, None)]
    with capture(monkeypatch, frames, tts=tts, on_connect=connect) as result:
        assert attempted == [False]
        result.stt.start.assert_called_once()
        assert result.dispatch.call_args.args[2] == "Help me check memory again"
    assert not tts.workers


def test_button_retry_expires_even_if_capture_finished_during_backoff(monkeypatch, tts):
    monkeypatch.setattr(button_actions.state, "tts_service", tts)
    speak = Mock(return_value=False)
    monkeypatch.setattr(tts, "speak_cached", speak)

    def backoff(_):
        token = tts.begin_input_capture()
        tts.end_input_capture(token)

    monkeypatch.setattr(button_actions, "time", SimpleNamespace(sleep=backoff))
    button_actions._announce_listening()
    speak.assert_called_once()


def test_delayed_button_worker_cannot_stop_speech_after_a_new_capture(monkeypatch, tts):
    monkeypatch.setattr(button_actions.state, "tts_service", tts)
    requested_state = tts.input_capture_state
    token = tts.begin_input_capture()
    tts.end_input_capture(token)
    button_actions._announce_listening(requested_state)
    tts.stop.assert_not_called()
    assert not tts.workers


@pytest.mark.parametrize("cached", [False, True])
def test_capture_starting_during_tts_admission_is_rechecked(monkeypatch, tts, cached):
    # Simulate the capture taking priority after the early request check but
    # before speech state is claimed. The second check must release the lock.
    def cache_path(_):
        tts.begin_input_capture()
        return SimpleNamespace(exists=lambda: False, name="cue")

    tts._tts_cache_path = cache_path
    speak = tts.speak_cached if cached else tts.speak
    assert not speak("I'm listening", interruptible=True)
    assert not tts.speaking
    assert not tts._lock.locked()
    assert not tts.workers


@pytest.mark.parametrize("kwargs", [
    {}, {"interruptible": True, "realtime_feedback": True},
    {"interruptible": True, "realtime_reply": True},
])
def test_capture_does_not_change_answer_admission(tts, kwargs):
    tts.begin_input_capture()
    assert tts.speak_cached("Here is the answer", **kwargs)
    assert tts.speaking
    assert len(tts.workers) == 1


def test_optional_request_does_not_stop_existing_speech_during_capture(tts):
    tts.begin_input_capture()
    tts._lock.acquire()
    tts._speaking = True
    tts._interruptible = True
    assert not tts.speak_cached("Hang on", interruptible=True)
    tts.stop.assert_not_called()


def test_capture_reservation_is_released_when_setup_raises(monkeypatch, tts):
    from hal.drivers.voice import voice_service as module

    monkeypatch.setattr(module.voice_cfg, "LIVE_MODE", False)
    monkeypatch.setattr(module, "read_voice_mode", Mock(side_effect=RuntimeError("mode unavailable")))
    service = SimpleNamespace(_tts=tts)
    with pytest.raises(RuntimeError, match="mode unavailable"):
        module.VoiceService._stream_session(service, None, 1024, 16000)
    assert not tts.input_capture_state[0]


@pytest.mark.parametrize("live,manual", [(True, None), (False, object())])
def test_live_and_manual_capture_do_not_reserve_optional_speech(monkeypatch, tts, live, manual):
    from hal.drivers.voice import voice_service as module

    monkeypatch.setattr(module.voice_cfg, "LIVE_MODE", live)

    def setup():
        assert not tts.input_capture_state[0]
        raise RuntimeError("stop at setup")

    monkeypatch.setattr(module, "read_voice_mode", setup)
    with pytest.raises(RuntimeError, match="stop at setup"):
        module.VoiceService._stream_session(SimpleNamespace(_tts=tts), None, 1024, 16000,
                                           manual_capture=manual)
    assert tts.input_capture_state == (False, 0)


def test_prerender_can_warm_cache_during_capture(monkeypatch, tts, tmp_path):
    tts.begin_input_capture()
    render = Mock()
    monkeypatch.setattr(tts, "_render_and_save_wav", render)
    assert tts.speak_cached("I'm listening", interruptible=True, prerender=True)
    render.assert_called_once()
    assert not tts.speaking
    assert not tts.workers
