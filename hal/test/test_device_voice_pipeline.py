"""Real producer/worker synchronization with controlled network and PCM sources."""

from types import SimpleNamespace
from unittest.mock import Mock
import threading
import time

import pytest
import numpy as np

from hal.drivers.voice._internal.device_turn_queue import DeviceTurnQueue
from hal.drivers.voice._internal.harness_capture import Capture
from hal.drivers.voice._internal import device_voice_pipeline as module


class Session:
    def __init__(self, text, connect=None, send=None):
        self.text = text
        self.connect = connect
        self.send = send
        self.started = threading.Event()
        self.sent = []
        self.closed = 0
        self.callback = None

    def start(self, callback):
        self.callback = callback
        self.started.set()
        if self.connect is not None:
            assert self.connect.wait(2)
        return True

    def send_audio(self, pcm):
        if self.send is not None:
            assert self.send.wait(2)
        self.sent.append(pcm)

    def is_closed(self):
        return bool(self.closed)

    def close(self):
        self.closed += 1
        if self.callback:
            self.callback(self.text, True)


class Mic:
    def __init__(self, capture, frames):
        self.capture = capture
        self.frames = list(frames)
        self.proved = False

    def read(self, size):
        if not self.proved:
            self.proved = True
            return b"warmup", False
        frame = self.frames.pop(0)
        if not self.frames:
            self.capture.finished.set()
        return frame, False


@pytest.fixture
def setup_pipeline(monkeypatch):
    queue = DeviceTurnQueue()
    dispatches = []
    visuals = []
    sessions = []
    route = {"generation": 1}
    cues = []
    decorator = SimpleNamespace(
        classify_wake_word=lambda text: (text, "voice"),
        identify_and_decorate=lambda text, audio: (text, "known-user", "User"),
    )
    tts = SimpleNamespace(last_spoken_text="", play_device_capture_chime=lambda **kw: cues.append(kw))
    monkeypatch.setattr(module.voice_metrics, "speech_end", lambda *a, **kw: "turn-id")

    def dispatch(decorator, sender, text, audio, ser, result, **kwargs):
        dispatches.append((text, list(audio), list(ser), kwargs))

    monkeypatch.setattr(module, "dispatch_turn", dispatch)
    pipeline = module.DeviceVoicePipeline(
        queue, create_session=lambda: sessions.pop(0),
        convert=lambda data, rate: data,
        valid=lambda snapshot: snapshot["generation"] == route["generation"],
        set_capturing=visuals.append, tts=tts, decorator=decorator,
        sensing_sender=None, noise_is_speech=lambda pcm: True,
    )
    yield SimpleNamespace(queue=queue, pipeline=pipeline, dispatches=dispatches,
                          visuals=visuals, sessions=sessions, route=route, cues=cues)
    assert queue.shutdown(3)


def capture_turn(setup, frames):
    ticket = setup.queue.reserve({"enabled": False, "generation": 1,
                                  "deviceInputMode": "tap_to_talk"})
    assert ticket is not None
    capture = Capture(ticket.snapshot, cancelled=ticket.cancelled)
    accepted = setup.pipeline.run_capture(Mic(capture, frames), 1024, 16000, capture, ticket)
    return ticket, capture, accepted


def test_finish_before_connect_releases_capture_and_preserves_every_pcm_frame(setup_pipeline):
    setup = setup_pipeline
    connect = threading.Event()
    session = Session("Please play my favorite song", connect=connect)
    setup.sessions.append(session)
    ticket, capture, accepted = capture_turn(setup, [b"first", b"last"])
    try:
        assert accepted and capture.finished.is_set()
        assert session.started.wait(1)
        assert not ticket.done.is_set()
        assert setup.visuals == [True, False]
        assert setup.cues == [{"finished": False}, {"finished": True}]
        connect.set()
        assert ticket.done.wait(1)
        assert b"".join(session.sent) == b"firstlast"
        assert session.closed == 1
        assert [b"".join(frames) for frames in setup.dispatches[0][1:3]] == [b"firstlast", b"firstlast"]
        assert setup.dispatches[0][3]["event_type_override"] == "voice_command"
        assert setup.dispatches[0][3]["identity"] == (session.text, "known-user", "User")
        assert setup.visuals == [True, False]
    finally:
        connect.set()


def test_next_capture_opens_while_first_connects_and_dispatch_stays_fifo(setup_pipeline):
    setup = setup_pipeline
    connect = threading.Event()
    first_session = Session("This is the first complete request", connect=connect)
    second_session = Session("This is the second complete request")
    setup.sessions.extend([first_session, second_session])
    first, _, _ = capture_turn(setup, [b"one"])
    try:
        second, _, accepted = capture_turn(setup, [b"two"])
        assert accepted
        assert setup.visuals == [True, False, True, False]
        assert setup.queue.reserve({}) is None
        assert setup.dispatches == []
        connect.set()
        assert first.done.wait(1) and second.done.wait(1)
        assert [entry[0] for entry in setup.dispatches] == [first_session.text, second_session.text]
        assert first_session.sent == [b"one"] and second_session.sent == [b"two"]
    finally:
        connect.set()


def test_slow_upload_does_not_delay_local_stop(setup_pipeline):
    setup = setup_pipeline
    send = threading.Event()
    session = Session("A request with slow network upload", send=send)
    setup.sessions.append(session)
    ticket, _, accepted = capture_turn(setup, [b"one", b"two"])
    try:
        assert accepted and not ticket.done.is_set()
        assert setup.visuals == [True, False]
        send.set()
        assert ticket.done.wait(1)
        assert b"".join(session.sent) == b"onetwo"
    finally:
        send.set()


def test_slow_realtime_preserves_ready_stop_latency_and_fifo(setup_pipeline):
    setup = setup_pipeline
    entered = threading.Event()
    respond = threading.Event()
    realtime_inputs = []

    def realtime_turn(**kwargs):
        realtime_inputs.append(kwargs["combined"])
        assert kwargs["valid"]() and not kwargs["cancelled"].is_set()
        entered.set()
        assert respond.wait(2)
        return module.RealtimeTurnResult(handled=True, route="realtime_handled")

    setup.pipeline.realtime_turn = realtime_turn
    first_session = Session("First complete request for realtime")
    second_session = Session("Second complete request for realtime")
    setup.sessions.extend([first_session, second_session])
    first, _, accepted = capture_turn(setup, [b"first"])
    try:
        assert accepted and entered.wait(1)
        started = time.perf_counter()
        second, _, accepted = capture_turn(setup, [b"second"])
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(f"Next local ready/stop while realtime blocked: {elapsed_ms:.2f} ms")
        assert accepted and not respond.is_set()
        assert setup.cues == [dict(finished=False), dict(finished=True)] * 2
        assert setup.visuals == [True, False, True, False]
        assert setup.dispatches == []
        assert setup.queue.reserve({}) is None
        respond.set()
        assert first.done.wait(1) and second.done.wait(1)
        assert realtime_inputs == [first_session.text, second_session.text]
        assert [entry[0] for entry in setup.dispatches] == realtime_inputs
    finally:
        respond.set()


@pytest.mark.parametrize("cancel", ["privacy", "route"])
def test_cancel_while_realtime_pending_cannot_dispatch(setup_pipeline, cancel):
    setup = setup_pipeline
    setup.sessions.append(Session("Do not forward this cancelled request"))

    def realtime_turn(**kwargs):
        if cancel == "privacy":
            kwargs["cancelled"].set()
        else:
            setup.route["generation"] += 1
        return module.RealtimeTurnResult(delegated=True, delegate_msg="cancelled")

    setup.pipeline.realtime_turn = realtime_turn
    ticket, _, accepted = capture_turn(setup, [b"audio"])
    assert accepted and ticket.done.wait(1)
    assert setup.dispatches == []


@pytest.mark.parametrize("outcome,event", [
    (module.RealtimeTurnResult(handled=True, transcript="Answered", route="realtime_handled"), "voice_agent_handled"),
    (module.RealtimeTurnResult(delegated=True, delegate_msg="Run the task", route="delegated"), "voice_command"),
    (module.RealtimeTurnResult(route="realtime_unavailable"), "voice_command"),
    (module.RealtimeTurnResult(route="realtime_cancelled"), None),
])
def test_finalizer_routes_realtime_result_without_duplicate_main_reply(
        setup_pipeline, monkeypatch, outcome, event):
    from hal.drivers.voice._internal.turn_dispatch import dispatch_turn

    setup = setup_pipeline
    sender = Mock()
    setup.pipeline.sensing_sender = sender
    setup.pipeline.decorator.submit_speech_emotion_from_session = Mock()
    setup.pipeline.realtime_turn = lambda **kwargs: outcome
    monkeypatch.setattr(module, "dispatch_turn", dispatch_turn)
    setup.sessions.append(Session("Please handle this complete request"))
    ticket, _, accepted = capture_turn(setup, [b"audio"])
    assert accepted and ticket.done.wait(1)
    if event is None:
        sender.send.assert_not_called()
    else:
        sender.send.assert_called_once()
        assert sender.send.call_args.kwargs["event_type"] == event
        assert sender.send.call_args.kwargs["voice_turn_type"] == "voice_command"


@pytest.mark.parametrize("cancel_kind", ["privacy", "route", "shutdown"])
def test_cancel_pending_connect_closes_late_session_without_dispatch(setup_pipeline, cancel_kind):
    setup = setup_pipeline
    connect = threading.Event()
    session = Session("This must never be dispatched", connect=connect)
    setup.sessions.append(session)
    ticket, capture, _ = capture_turn(setup, [b"private"])
    try:
        assert session.started.wait(1)
        if cancel_kind == "privacy":
            setup.queue.cancel_all()
        elif cancel_kind == "route":
            setup.route["generation"] = 2
            setup.queue.cancel_invalid(setup.pipeline.valid)
        else:
            assert not setup.queue.shutdown(0)
        assert capture.cancelled.is_set()
        connect.set()
        assert ticket.done.wait(1)
        assert session.closed == 1
        assert session.sent == [] and setup.dispatches == []
    finally:
        connect.set()


def test_generation_change_during_identity_prevents_dispatch(setup_pipeline):
    setup = setup_pipeline
    session = Session("Please tell me the weather tomorrow")
    setup.sessions.append(session)

    def identify(text, audio):
        setup.route["generation"] = 2
        return text, "user", "User"

    setup.pipeline.decorator.identify_and_decorate = identify
    ticket, _, _ = capture_turn(setup, [b"pcm"])
    assert ticket.done.wait(1)
    assert setup.dispatches == []


def test_pcm_memory_limit_discards_instead_of_dispatching_truncated_request(setup_pipeline):
    setup = setup_pipeline
    setup.pipeline.max_duration = 0.01  # 320 bytes at 16 kHz PCM16.
    session = Session("Truncated text must not be delivered")
    setup.sessions.append(session)
    ticket, capture, accepted = capture_turn(setup, [b"x" * 321, b"tail"])
    assert not accepted and capture.cancelled.is_set()
    assert ticket.done.wait(1)
    assert setup.dispatches == []
    assert setup.cues == [{"finished": False}]


def test_stale_reservation_cannot_open_mic_or_session(setup_pipeline):
    setup = setup_pipeline
    ticket = setup.queue.reserve({"generation": 1})
    capture = Capture(ticket.snapshot, cancelled=ticket.cancelled)
    setup.queue.cancel_all()
    mic = Mic(capture, [b"private"])
    assert not setup.pipeline.run_capture(mic, 1024, 16000, capture, ticket)
    assert not mic.proved
    assert setup.cues == []


def test_session_creation_failure_releases_reserved_capacity(setup_pipeline):
    setup = setup_pipeline
    # Empty fake provider fails synchronously before a worker is started.
    ticket, capture, accepted = capture_turn(setup, [b"unused"])
    assert not accepted and capture.cancelled.is_set()
    assert ticket.done.wait(1)
    assert setup.queue.pending == 0
    assert setup.cues == [] and setup.dispatches == []


def test_noise_guard_result_is_preserved_without_speaker_identity(setup_pipeline, monkeypatch):
    setup = setup_pipeline
    monkeypatch.setattr(module.hal_config, "REALTIME_REQUIRE_SPEECH_ON_EMPTY_STT", True)
    monkeypatch.setattr(module.hal_config, "REALTIME_NOISE_GUARD_MAX_WORDS", 3)
    setup.pipeline.noise_is_speech = lambda pcm: False
    setup.pipeline.decorator.identify_and_decorate = lambda *args: pytest.fail("noise speaker ID")
    setup.sessions.append(Session("Hello"))
    results = []
    monkeypatch.setattr(module, "dispatch_turn", lambda *args, **kw: results.append(args[5]))
    ticket, _, _ = capture_turn(setup, [b"noise"])
    assert ticket.done.wait(1)
    assert results[0].route == module.ROUTE_NOISE_DROPPED


@pytest.mark.parametrize("rate", [16000, 44100, 48000])
def test_ten_ms_reads_resample_only_full_frames_and_trim_speaker_silence(setup_pipeline, rate):
    from hal.drivers.voice._internal.audio_dsp import resample_to_stt

    setup = setup_pipeline
    setup.sessions.append(Session("Please retain all audio from this explicit request"))
    chunks = [np.full((rate // 100, 1), 2000 if i == 0 else 0, dtype=np.int16)
              for i in range(40)]
    converted_sizes = []

    def convert(data, native_rate):
        converted_sizes.append(len(data))
        return resample_to_stt(data, native_rate, 16000, np)

    setup.pipeline.convert = convert
    setup.pipeline.on_frame = lambda data: bool(np.any(data))
    ticket = setup.queue.reserve({"enabled": False, "generation": 1})
    capture = Capture(ticket.snapshot, cancelled=ticket.cancelled)
    mic = Mic(capture, chunks)
    reads = []
    original_read = mic.read
    mic.read = lambda size: (reads.append(size), original_read(size))[1]
    frame_size = int(rate * 0.064)
    assert setup.pipeline.run_capture(mic, frame_size, rate, capture, ticket)
    assert ticket.done.wait(2)
    assert set(reads) == {rate // 100}
    assert converted_sizes == [frame_size] * 6 + [rate // 100 * 40 - frame_size * 6]
    native = np.concatenate(chunks)
    expected = b"".join(resample_to_stt(native[i:i + frame_size], rate, 16000, np)
                        for i in range(0, len(native), frame_size))
    # SER/upload retain the full request; speaker identity trims silence after
    # the first 64 ms speech frame instead of smearing speech across 320 ms.
    assert b"".join(setup.dispatches[0][2]) == expected
    assert len(setup.dispatches[0][1]) == 5


def test_music_starting_after_local_stop_does_not_discard_submitted_turn(setup_pipeline):
    setup = setup_pipeline
    connect = threading.Event()
    setup.sessions.append(Session("Please stop that music now", connect=connect))
    music = [False]
    setup.pipeline.capture_valid = lambda snapshot: not music[0]
    ticket, _, accepted = capture_turn(setup, [b"pcm"])
    try:
        assert accepted
        music[0] = True
        connect.set()
        assert ticket.done.wait(1)
        assert len(setup.dispatches) == 1
    finally:
        connect.set()


def test_speaker_identity_failure_keeps_transcript(setup_pipeline):
    setup = setup_pipeline
    setup.sessions.append(Session("Please keep this valid request"))

    def failed_identity(*args):
        raise RuntimeError("identity unavailable")

    setup.pipeline.decorator.identify_and_decorate = failed_identity
    ticket, _, _ = capture_turn(setup, [b"pcm"])
    assert ticket.done.wait(1)
    assert setup.dispatches[0][3]["identity"] == ("Please keep this valid request", None, None)
