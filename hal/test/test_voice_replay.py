"""The offline replay walks audio the way the capture loop does."""

import importlib.util
from pathlib import Path

import numpy as np

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bench" / "voice_replay.py"
spec = importlib.util.spec_from_file_location("voice_replay", SCRIPT)
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)

RATE = replay.STT_RATE


class FakeGate(replay.Gate):
    """Loud = speech; Silero says yes; the guard reports the span it was given."""

    def __init__(self, voiced_ratio=0.9):
        self.voiced_ratio = voiced_ratio
        self.holdoff_calls = 0

    def entry(self, frame, rms):
        return rms >= 1000

    def holdoff(self, pcm):
        self.holdoff_calls += 1
        return True

    def metrics(self, pcm):
        # Like Silero, only the loud span counts, not the trailing silence.
        frames = pcm[: len(pcm) - len(pcm) % replay.FRAME_SAMPLES].reshape(-1, replay.FRAME_SAMPLES)
        loud = sum(1 for f in frames if replay.rms(f) >= 1000)
        return (0.95, 0.5, self.voiced_ratio, self.voiced_ratio, loud * replay.FRAME_MS / 1000.0)


def _signal(parts):
    """parts: (seconds, amplitude) → int16 samples at 16 kHz."""
    rng = np.random.default_rng(7)
    chunks = []
    for seconds, amplitude in parts:
        n = int(seconds * RATE)
        chunks.append((rng.standard_normal(n) * amplitude).clip(-32000, 32000).astype(np.int16))
    return np.concatenate(chunks)


def test_two_utterances_with_endpoint_timing():
    samples = _signal([(1.0, 20), (1.5, 8000), (2.0, 20), (0.3, 8000), (2.0, 20)])
    params = replay.Params(silence_s=1.0, endpoint_s=0.6, min_voiced_ms=500)
    gate = FakeGate()
    out = replay.segment(samples, gate, params)
    assert len(out) == 2
    first, second = out
    assert 0.9 <= first.start_s <= 1.1
    assert 2.4 <= first.end_s <= 2.6
    assert abs(first.decided_s - (first.end_s + 1.0)) < 1e-6
    assert abs(first.earliest_s - (first.end_s + 0.6)) < 1e-6
    assert first.guard == "speech" and first.reason == "silence_clock"
    # 0.3 s of sound is under the 500 ms voiced floor: the guard calls it noise.
    assert second.guard == "noise"
    assert gate.holdoff_calls == 2


def test_silence_only_yields_nothing():
    assert replay.segment(_signal([(3.0, 20)]), FakeGate(), replay.Params()) == []


def test_render_and_accepts_contract():
    out = replay.segment(_signal([(0.5, 20), (1.0, 8000), (1.5, 20)]), FakeGate(), replay.Params())
    text = replay.render(out, replay.Params(), 3.0)
    assert "1 utterance(s)" in text and "speech=1" in text
    # Silero's fail-open tuple is reported as not measured, never as noise.
    assert replay.accepts((1.0, 1.0, 1.0, 1.0, 0.0), replay.Params())[0] == "n/a"
