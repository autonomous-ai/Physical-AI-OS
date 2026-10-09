"""Real realtime execution must not inherit Deepgram or identity latency."""

from types import SimpleNamespace
from unittest.mock import Mock
import threading

import numpy as np
import pytest

from hal.drivers.voice._internal import device_voice_pipeline as pipeline_module
from hal.drivers.voice._internal import realtime_turn
from hal.drivers.voice._internal import turn_dispatch
from hal.drivers.voice._internal.device_realtime import DeviceRealtimeTurn
from hal.drivers.voice._internal.device_turn_queue import DeviceTurnQueue
from hal.drivers.voice._internal.harness_capture import Capture
from hal.realtime.models import TextOutput


PCM = np.full(160, 2000, dtype=np.int16).tobytes()


class _Session:
    def __init__(self, *, delay_start=False, delay_close=False):
        self.start_entered = threading.Event()
        self.start_release = threading.Event()
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        if not delay_start:
            self.start_release.set()
        if not delay_close:
            self.close_release.set()
        self.callback = None

    def start(self, callback):
        self.callback = callback
        self.start_entered.set()
        assert self.start_release.wait(2)
        return True

    def send_audio(self, pcm):
        pass

    def is_closed(self):
        return False

    def close(self):
        self.close_entered.set()
        assert self.close_release.wait(2)
        self.callback("Please describe the weather today", True)


class _Mic:
    def __init__(self, capture):
        self.capture = capture
        self.reads = 0
        self.recording = threading.Event()
        self.finish = threading.Event()

    def read(self, size):
        self.reads += 1
        if self.reads == 3:
            # Read 1 proves microphone readiness; read 2 supplies one complete
            # upload frame. Hold the next read until the test performs tap 2.
            self.recording.set()
            assert self.finish.wait(2)
            if not self.capture.cancelled.is_set():
                self.capture.finished.set()
        return PCM, False


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_PROVIDER", "gemini")
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_REQUIRE_TRANSCRIPT", True)
    monkeypatch.setattr(realtime_turn, "native_voice", lambda tts: None)
    monkeypatch.setattr(realtime_turn, "pending_main_question", lambda: None)
    monkeypatch.setattr(realtime_turn, "_reply_language_name", lambda: "English")
    monkeypatch.setattr(realtime_turn, "_WaitFiller", Mock())
    monkeypatch.setattr(pipeline_module.voice_metrics, "speech_end", lambda *a, **kw: "tap")
    dispatch = Mock()
    monkeypatch.setattr(pipeline_module, "dispatch_turn", dispatch)
    turns = []
    original_turn = pipeline_module._RecordedTurn

    class RecordedTurn(original_turn):
        def __init__(self, *args):
            super().__init__(*args)
            turns.append(self)

    monkeypatch.setattr(pipeline_module, "_RecordedTurn", RecordedTurn)
    queue = DeviceTurnQueue()
    sessions = []
    all_sessions = []
    captures = []
    uploaded = threading.Event()
    committed = threading.Event()
    replied = threading.Event()
    realtime = Mock(available=True, sample_rate=16000, execution_completed=False)
    realtime.wait_until_available.return_value = True
    realtime.append_audio.side_effect = lambda *a, **kw: uploaded.set()
    realtime.commit_audio.side_effect = lambda **kw: committed.set()
    realtime.stream_output.side_effect = lambda **kw: iter([
        TextOutput(text="Here is the answer to your request."),
    ])
    tts = Mock(_provider="test", last_spoken_text="")
    tts.speak.side_effect = lambda *a, **kw: (replied.set() or True)
    identity_entered = threading.Event()
    identity_release = threading.Event()
    identity_release.set()

    def identify(text, audio):
        identity_entered.set()
        assert identity_release.wait(2)
        return text, None, None

    decorator = SimpleNamespace(classify_wake_word=lambda text: (text, "voice"),
                                identify_and_decorate=identify)
    executor = DeviceRealtimeTurn(realtime=lambda: realtime, tts=lambda: tts,
                                   strip_markers=lambda text: text)
    pipeline = pipeline_module.DeviceVoicePipeline(
        queue, create_session=lambda: sessions.pop(0), convert=lambda data, rate: data,
        valid=lambda snapshot: True, set_capturing=lambda active: None,
        tts=tts, decorator=decorator, sensing_sender=None, noise_is_speech=lambda audio: True,
        stream_realtime=executor.stream,
    )

    def start(session):
        sessions.append(session)
        all_sessions.append(session)
        ticket = queue.reserve({"enabled": False, "generation": 1})
        assert ticket is not None
        capture = Capture(ticket.snapshot, cancelled=ticket.cancelled)
        mic = _Mic(capture)
        recorder = threading.Thread(target=pipeline.run_capture,
                                    args=(mic, 160, 16000, capture, ticket))
        captures.append((mic, recorder))
        recorder.start()
        assert mic.recording.wait(1)
        return ticket, capture, mic, recorder

    yield SimpleNamespace(**locals())
    queue.cancel_all()
    identity_release.set()
    for session in all_sessions:
        session.start_release.set()
        session.close_release.set()
    for mic, recorder in captures:
        mic.finish.set()
        recorder.join(2)
        assert not recorder.is_alive()
    assert queue.shutdown(2)


def test_upload_and_reply_precede_stt_connect_close_and_identity(setup):
    session = _Session(delay_start=True, delay_close=True)
    setup.identity_release.clear()
    ticket, capture, mic, recorder = setup.start(session)
    assert session.start_entered.wait(1)
    assert setup.uploaded.wait(1)
    assert not capture.finished.is_set()
    assert not setup.committed.is_set()
    assert setup.turns[0].partial == [""]
    mic.finish.set()
    assert setup.committed.wait(1)
    assert setup.replied.wait(1), "real realtime reply must not wait for STT text"
    recorder.join(1)
    assert not recorder.is_alive()
    assert not session.start_release.is_set()
    assert not setup.identity_entered.is_set()
    assert not ticket.done.is_set()
    session.start_release.set()
    assert session.close_entered.wait(1)
    assert not session.close_release.is_set()
    session.close_release.set()
    assert setup.identity_entered.wait(1)
    assert setup.replied.is_set()
    assert not ticket.done.is_set()
    setup.identity_release.set()
    assert ticket.done.wait(1)
    assert setup.dispatch.call_args.args[5].handled


def test_next_realtime_turn_starts_while_previous_stt_close_is_blocked(setup):
    first_session = _Session(delay_close=True)
    first, _, first_mic, _ = setup.start(first_session)
    first_mic.finish.set()
    assert setup.turns[0].realtime_done.wait(1)
    assert first_session.close_entered.wait(1)
    assert not first.done.is_set()
    setup.uploaded.clear()
    setup.committed.clear()
    setup.replied.clear()
    second, _, second_mic, _ = setup.start(_Session())
    assert setup.uploaded.wait(1)
    assert not first_session.close_release.is_set()
    assert setup.realtime.prepare_turn.call_count == 2
    second_mic.finish.set()
    assert setup.committed.wait(1)
    assert setup.replied.wait(1)
    assert not first.done.is_set()
    assert not second.done.is_set()
    assert setup.dispatch.call_count == 0
    first_session.close_release.set()
    assert first.done.wait(1) and second.done.wait(1)
    assert setup.dispatch.call_count == 2


def test_cancelled_capture_never_commits_and_retains_audio_until_upload_cleanup(setup):
    session = _Session(delay_close=True)
    ticket, capture, mic, recorder = setup.start(session)
    assert setup.uploaded.wait(1)
    turn = setup.turns[0]
    assert turn.audio
    capture.cancelled.set()
    mic.finish.set()
    recorder.join(1)
    assert not recorder.is_alive()
    assert turn.realtime_done.wait(1)
    assert session.close_entered.wait(1)
    assert not setup.committed.is_set()
    assert not ticket.done.is_set()
    assert turn.audio, "STT still owns the turn buffers while close is pending"
    setup.realtime.recover_session.assert_called_once_with(
        "device-turn-discarded", discard_old_on_failure=True,
    )
    session.close_release.set()
    assert ticket.done.wait(1)
    assert turn.audio == []
    setup.dispatch.assert_not_called()


@pytest.mark.parametrize("fallback", ["unavailable", "no_output", "upload_error"])
def test_final_noise_guard_prevents_main_fallback_after_stream_failure(setup, monkeypatch, fallback):
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_REQUIRE_SPEECH_ON_EMPTY_STT", True)
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_NOISE_GUARD_MAX_WORDS", 3)
    # A short fabricated STT word over non-speech must not become a command
    # merely because the earlier realtime attempt was unavailable or failed.
    monkeypatch.setattr(pipeline_module, "finalize_session",
                        lambda *args: ("you", [PCM], 0.04))
    setup.pipeline.noise_is_speech = lambda audio: False
    setup.pipeline.decorator.submit_speech_emotion_from_session = Mock()
    sender = Mock()
    setup.pipeline.sensing_sender = sender
    handoff = Mock()
    setup.pipeline.record_handoff = handoff
    dispatch = Mock(wraps=turn_dispatch.dispatch_turn)
    monkeypatch.setattr(pipeline_module, "dispatch_turn", dispatch)
    monkeypatch.setattr(turn_dispatch, "_take_vision_handoff", lambda **kwargs: ("", ""))
    monkeypatch.setattr(turn_dispatch, "_take_look_snapshot_marker", lambda: "")
    if fallback == "unavailable":
        setup.realtime.wait_until_available.return_value = False
    elif fallback == "no_output":
        setup.realtime.stream_output.side_effect = lambda **kwargs: iter([])
    else:
        setup.realtime.append_audio.side_effect = RuntimeError("provider upload failed")
    ticket, _, mic, _ = setup.start(_Session())
    mic.finish.set()
    assert ticket.done.wait(1)
    dispatch.assert_called_once()
    assert dispatch.call_args.args[5].route == realtime_turn.ROUTE_NOISE_DROPPED
    sender.send.assert_not_called()
    handoff.assert_not_called()
