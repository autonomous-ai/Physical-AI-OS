"""Exercise the paced writer against a deterministic, continuously draining sink."""

from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np

from hal.drivers.voice.tts import service as module


class Clock:
    now = 1.0

    def monotonic(self):
        return self.now


class Sink:
    def __init__(self, clock, samplerate, latency, **kwargs):
        self.clock = clock
        self.rate = samplerate
        self.latency = latency
        self.buffered = 0.0
        self.last = clock.now
        self.writes = []
        self.xruns = 0

    def start(self):
        pass

    def write(self, data):
        elapsed = self.clock.now - self.last
        underran = bool(self.writes) and elapsed > self.buffered + 1e-9
        self.xruns += int(underran)
        self.buffered = max(0.0, self.buffered - elapsed)
        duration = len(data) / self.rate
        # Blocking write waits for room, while hardware continues draining.
        wait = max(0.0, self.buffered + duration - self.latency)
        self.clock.now += wait
        self.buffered = self.buffered + duration - wait
        self.last = self.clock.now
        self.writes.append(data.copy())
        return underran


def _owner(monkeypatch, default_latency=0.0435):
    import hal.drivers.audio_route as route

    clock = Clock()
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(route, "stream_open_guard", nullcontext)
    owner = object.__new__(module.TTSService)
    owner._output_device = 4
    owner._stream = None
    owner._stream_rate = None
    owner._audio_written_fired = False
    owner._on_playback_audio = None
    sinks = []

    def open_stream(**kwargs):
        sink = Sink(clock, **kwargs)
        sinks.append(sink)
        return sink

    owner._sd = SimpleNamespace(
        OutputStream=open_stream,
        query_devices=lambda *args: {"default_high_output_latency": default_latency},
        PortAudioError=RuntimeError,
    )
    return owner, clock, sinks


def test_playback_survives_a_short_writer_scheduling_stall(monkeypatch, caplog):
    """A 70ms scheduling/AEC stall emptied the observed 43.5ms device buffer."""
    audio = np.arange(44100, dtype=np.float32).reshape(-1, 1)
    results = []
    for requested in (0.0, 0.120):
        monkeypatch.setattr(module, "TTS_OUTPUT_LATENCY_S", requested)
        owner, clock, sinks = _owner(monkeypatch)
        reference = []

        def aec_write(data, rate):
            reference.append(data.copy())
            clock.now += 0.002
            if len(reference) == 10:
                clock.now += 0.070

        monkeypatch.setattr(module.aec, "reference_write", aec_write)
        stream = owner._ensure_stream(44100)
        result = stream.write(audio)
        results.append((result, sinks[0].xruns))
        np.testing.assert_array_equal(np.concatenate(sinks[0].writes), audio)
        np.testing.assert_array_equal(np.concatenate(reference), audio)
        assert owner._write_started_ts is None
    assert results == [(True, 1), (False, 0)]
    assert "TTS output underflow during playback" in caplog.text
    assert "previous_aec_ms=72.0" in caplog.text


def test_does_not_shrink_a_bluetooth_devices_larger_buffer(monkeypatch):
    owner, _, sinks = _owner(monkeypatch, default_latency=0.250)
    stream = owner._ensure_stream(24000)
    assert sinks[0].latency == 0.250
    assert owner._ensure_stream(24000) is stream
    assert len(sinks) == 1


def test_latency_query_failure_still_uses_headroom(monkeypatch):
    owner, _, sinks = _owner(monkeypatch)
    owner._sd.query_devices = lambda *args: {}
    owner._ensure_stream(44100)
    assert sinks[0].latency == 0.120


def test_underflow_flags_accumulate_and_logs_are_rate_limited(monkeypatch, caplog):
    owner, clock, sinks = _owner(monkeypatch)
    stream = owner._ensure_stream(44100)
    flags = iter([True, True, True, False])
    sinks[0].write = lambda data: next(flags)
    monkeypatch.setattr(module.aec, "reference_write", lambda data, rate: None)
    frame = np.zeros((1764, 1), dtype=np.float32)
    assert stream.write(frame) is True
    assert "during playback" not in caplog.text
    assert stream.write(np.concatenate([frame, frame, frame])) is True
    assert stream._underflows == 2
    assert caplog.text.count("TTS output underflow during playback") == 1
    clock.now += 5.1
    sinks[0].write = lambda data: True
    stream.write(frame)
    assert caplog.text.count("TTS output underflow during playback") == 2


def test_keepalive_does_not_report_a_speech_underflow(monkeypatch, caplog):
    owner, _, sinks = _owner(monkeypatch)
    stream = owner._ensure_stream(44100)
    sinks[0].write = lambda data: True
    assert stream.write_untapped(np.zeros(882, dtype=np.float32)) is True
    assert stream._underflows == 0
    assert "during playback" not in caplog.text


def test_cold_reference_setup_happens_before_the_first_speaker_sample(monkeypatch):
    owner, clock, sinks = _owner(monkeypatch)
    stream = owner._ensure_stream(44100)
    preparations = []

    def prepare(rate):
        assert not sinks[0].writes
        assert owner._write_started_ts is None
        preparations.append(rate)
        clock.now += 0.400  # Model a slow first SciPy import/filter setup.

    owner._write_started_ts = None
    monkeypatch.setattr(module.aec, "prepare_reference", prepare)
    monkeypatch.setattr(module.aec, "reference_write", lambda data, rate: None)
    assert stream.write(np.zeros((44100, 1), dtype=np.float32)) is False
    assert preparations == [44100]
    assert sinks[0].xruns == 0
    assert owner._write_started_ts is None


def test_output_open_prepares_reference_without_playback(monkeypatch):
    owner, _, sinks = _owner(monkeypatch)
    rates = []
    monkeypatch.setattr(module.aec, "prepare_playback", rates.append)
    first = owner._ensure_stream(44100)
    assert rates == [44100]
    assert not sinks[0].writes
    assert owner._audio_written_fired is False
    assert owner._ensure_stream(44100) is first
    assert rates == [44100]


def test_stop_during_cold_setup_never_starts_cancelled_speech(monkeypatch):
    import threading

    owner, _, sinks = _owner(monkeypatch)
    owner._stop_event = threading.Event()
    stream = owner._ensure_stream(44100)
    monkeypatch.setattr(module.aec, "prepare_reference", lambda rate: owner._stop_event.set())
    stream.write(np.zeros((44100, 1), dtype=np.float32))
    assert not sinks[0].writes
    assert owner._audio_written_fired is False


def test_long_write_stops_between_slices_but_gesture_chime_can_still_play(monkeypatch):
    import threading

    owner, _, sinks = _owner(monkeypatch)
    owner._stop_event = threading.Event()
    stream = owner._ensure_stream(44100)
    monkeypatch.setattr(module.aec, "reference_write", lambda data, rate: owner._stop_event.set())
    stream.write(np.zeros((44100, 1), dtype=np.float32))
    assert len(sinks[0].writes) == 1
    assert len(sinks[0].writes[0]) == 1764
    stream.write(np.zeros((441, 1), dtype=np.float32), track_playback=False)
    assert len(sinks[0].writes) == 2


def test_device_failure_clears_watchdog_and_does_not_report_playback(monkeypatch):
    import pytest

    owner, _, sinks = _owner(monkeypatch)
    stream = owner._ensure_stream(44100)
    references = []
    monkeypatch.setattr(module.aec, "reference_write", lambda data, rate: references.append(data))

    def fail(data):
        assert owner._write_started_ts is not None
        raise RuntimeError("device disconnected")

    sinks[0].write = fail
    with pytest.raises(RuntimeError, match="device disconnected"):
        stream.write(np.zeros((1764, 1), dtype=np.float32))
    assert owner._write_started_ts is None
    assert owner._audio_written_fired is False
    assert references == []


def test_released_stream_does_not_replay_cancelled_audio(monkeypatch):
    owner, _, sinks = _owner(monkeypatch)
    stream = owner._ensure_stream(44100)
    references = []
    monkeypatch.setattr(module.aec, "reference_write", lambda data, rate: references.append(data))

    def release_during_write(data):
        owner._stream = None
        raise RuntimeError("stream was closed")

    sinks[0].write = release_during_write
    assert stream.write(np.zeros((44100, 1), dtype=np.float32)) is False
    assert owner._write_started_ts is None
    assert owner._audio_written_fired is False
    assert references == []


def test_watchdog_only_covers_the_device_write(monkeypatch):
    owner, _, sinks = _owner(monkeypatch)
    stream = owner._ensure_stream(44100)
    original_write = sinks[0].write
    phases = []

    def write(data):
        assert owner._write_started_ts is not None
        phases.append("device")
        return original_write(data)

    def observed(owner_id):
        assert owner._write_started_ts is None
        phases.append("metrics")

    def reference(data, rate):
        assert owner._write_started_ts is None
        phases.append("aec")

    owner._playback_owner = "test"
    owner._on_playback_audio = observed
    sinks[0].write = write
    monkeypatch.setattr(module.aec, "reference_write", reference)
    stream.write(np.zeros((3528, 1), dtype=np.float32))
    assert phases == ["device", "metrics", "aec", "device", "aec"]
