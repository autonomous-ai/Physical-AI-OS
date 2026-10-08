#!/usr/bin/env python3
"""Replay recorded room audio through the lamp's own voice gates, offline.

    python3 scripts/bench/voice_replay.py room.wav
    python3 scripts/bench/voice_replay.py room.wav --silence 0.8 --rms 400 --json

Runs the entry gate (RMS + WebRTC VAD + Silero hold-off), the silence clock and
the noise guard exactly as HAL does on a capture, over a WAV (an AEC dump, an
uplink dump, or anything recorded in the room). It prints every utterance the
lamp would have opened, when it would have decided the user stopped, and what
the noise guard would have said. Change one threshold at a time and re-run on
the same file: that is how a timing change gets measured before it ships.

No STT runs here, so the "earliest decision" column assumes an STT final arrives
at the end of speech; the "decided" column is the pure silence clock.
"""

import argparse
import json
import os
import sys
import wave
from dataclasses import dataclass, asdict

import numpy as np

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STT_RATE = 16000
FRAME_MS = 64
FRAME_SAMPLES = STT_RATE * FRAME_MS // 1000


@dataclass
class Params:
    rms: float = 500.0           # HAL_VAD_THRESHOLD
    holdoff_s: float = 0.05      # HAL_SPEECH_HOLDOFF
    pre_roll_frames: int = 12    # HAL_PRE_ROLL_FRAMES
    silence_s: float = 1.0       # HAL_SILENCE_TIMEOUT
    endpoint_s: float = 0.6      # HAL_ENDPOINT_SILENCE_S (after an STT final)
    min_ratio: float = 0.45      # HAL_REALTIME_NOISE_SPEECH_RATIO
    min_voiced_ms: float = 160.0  # HAL_VOICE_NOISE_MIN_VOICED_MS
    max_session_s: float = 20.0  # HAL_MAX_SESSION_DURATION_S


@dataclass
class Utterance:
    start_s: float          # first speech frame (pre-roll excluded)
    end_s: float            # last frame above the RMS floor
    decided_s: float        # silence clock: end_s + silence_s
    earliest_s: float       # if an STT final landed at end_s: end_s + endpoint_s
    duration_s: float
    voiced_ratio: float
    voiced_ms: float
    guard: str              # "speech" | "noise" | "n/a"
    reason: str


class Gate:
    """The three decisions HAL makes about audio, so tests can fake them."""

    def entry(self, frame: np.ndarray, rms: float) -> bool:
        raise NotImplementedError

    def holdoff(self, pcm: np.ndarray) -> bool:
        raise NotImplementedError

    def metrics(self, pcm: np.ndarray):
        raise NotImplementedError


class RealGate(Gate):
    """HAL's own filters: WebRTC VAD at the entry, Silero for hold-off and guard."""

    def __init__(self, params: Params):
        sys.path.insert(0, REPO_ROOT)
        from hal.drivers.voice._internal import config as voice_cfg
        from hal.drivers.voice._internal.vad_filters import SileroVADFilter, WebRTCVADFilter

        self._params = params
        self._webrtc = WebRTCVADFilter(voice_cfg.WEBRTCVAD_AGGRESSIVENESS, np)
        self._silero = SileroVADFilter(voice_cfg.SILERO_MODEL_PATH, np)

    def entry(self, frame, rms):
        return rms >= self._params.rms and self._webrtc.is_speech(frame, STT_RATE)

    def holdoff(self, pcm):
        self._silero.reset_state()
        return self._silero.is_speech(pcm, STT_RATE)

    def metrics(self, pcm):
        self._silero.reset_state()
        return self._silero.speech_metrics(pcm, STT_RATE)


def rms(frame: np.ndarray) -> float:
    samples = frame.astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0


def accepts(metrics, params: Params) -> tuple[str, float, float]:
    """Mirror hal.drivers.voice._internal.noise_guard.accepts_speech_metrics."""
    if tuple(metrics) == (1.0, 1.0, 1.0, 1.0, 0.0):
        return "n/a", 1.0, 0.0
    _, _, _, span_ratio, span_seconds = metrics
    voiced_ms = span_ratio * span_seconds * 1000
    ok = span_ratio >= params.min_ratio and voiced_ms + 1e-6 >= params.min_voiced_ms
    return ("speech" if ok else "noise"), float(span_ratio), float(voiced_ms)


def segment(samples: np.ndarray, gate: Gate, params: Params) -> list[Utterance]:
    """Walk 64 ms frames the way the capture loop does and return the utterances."""
    frame_s = FRAME_MS / 1000.0
    utterances: list[Utterance] = []
    n_frames = len(samples) // FRAME_SAMPLES
    i = 0
    while i < n_frames:
        # Idle: wait for the entry gate, then the hold-off.
        pre: list[np.ndarray] = []
        start_idx = None
        while i < n_frames:
            frame = samples[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES]
            i += 1
            if gate.entry(frame, rms(frame)):
                if start_idx is None:
                    start_idx = i - 1
                pre.append(frame)
                if len(pre) * frame_s >= params.holdoff_s:
                    if gate.holdoff(np.concatenate(pre)):
                        break
                    pre, start_idx = [], None
            else:
                pre, start_idx = [], None
        if start_idx is None:
            break
        # Capturing: the silence clock runs from the last loud frame.
        buffer = list(pre)
        last_speech_idx = i - 1
        end_reason = "silence_clock"
        while i < n_frames:
            frame = samples[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES]
            i += 1
            buffer.append(frame)
            if rms(frame) >= params.rms:
                last_speech_idx = i - 1
            elif (i - 1 - last_speech_idx) * frame_s > params.silence_s:
                break
            if (i - start_idx) * frame_s > params.max_session_s:
                end_reason = "max_duration"
                break
        else:
            end_reason = "end_of_file"
        pcm = np.concatenate(buffer)
        guard, ratio, voiced_ms = accepts(gate.metrics(pcm), params)
        end_s = (last_speech_idx + 1) * frame_s
        utterances.append(Utterance(
            start_s=round(start_idx * frame_s, 3),
            end_s=round(end_s, 3),
            decided_s=round(end_s + params.silence_s, 3),
            earliest_s=round(end_s + params.endpoint_s, 3),
            duration_s=round(end_s - start_idx * frame_s, 3),
            voiced_ratio=round(ratio, 3),
            voiced_ms=round(voiced_ms, 1),
            guard=guard,
            reason=end_reason,
        ))
        # HAL clears the look-back after a session; nothing carries over.
    return utterances


def load_wav(path: str) -> np.ndarray:
    """Mono int16 at 16 kHz, whatever the file holds."""
    with wave.open(path, "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit(f"{path}: {width * 8}-bit samples; only 16-bit PCM is supported")
    samples = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels)[:, 0]
    if rate != STT_RATE:
        from math import gcd

        import scipy.signal

        g = gcd(STT_RATE, rate)
        samples = scipy.signal.resample_poly(samples.astype(np.float32), STT_RATE // g, rate // g).astype(np.int16)
    return samples


def render(utterances: list[Utterance], params: Params, total_s: float) -> str:
    out = [f"{len(utterances)} utterance(s) in {total_s:.1f} s  "
           f"(rms≥{params.rms:.0f}, silence {params.silence_s:.2f} s, final+{params.endpoint_s:.2f} s, "
           f"guard ratio≥{params.min_ratio:.2f} voiced≥{params.min_voiced_ms:.0f} ms)", ""]
    header = f"{'#':>3} {'start':>8} {'end':>8} {'dur':>6} {'decided':>8} {'earliest':>9} {'voiced%':>8} {'voiced_ms':>9}  guard   end"
    out.append(header)
    out.append("-" * len(header))
    for n, u in enumerate(utterances, 1):
        out.append(f"{n:>3} {u.start_s:>8.2f} {u.end_s:>8.2f} {u.duration_s:>6.2f} {u.decided_s:>8.2f} "
                   f"{u.earliest_s:>9.2f} {u.voiced_ratio * 100:>7.0f}% {u.voiced_ms:>9.0f}  {u.guard:<7} {u.reason}")
    speech = sum(1 for u in utterances if u.guard == "speech")
    noise = sum(1 for u in utterances if u.guard == "noise")
    out.append("")
    out.append(f"guard: speech={speech} noise={noise} n/a={len(utterances) - speech - noise}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav")
    defaults = Params()
    ap.add_argument("--rms", type=float, default=defaults.rms, help="entry RMS floor (HAL_VAD_THRESHOLD)")
    ap.add_argument("--holdoff", type=float, default=defaults.holdoff_s, help="seconds of speech before Silero confirms (HAL_SPEECH_HOLDOFF)")
    ap.add_argument("--silence", type=float, default=defaults.silence_s, help="silence clock (HAL_SILENCE_TIMEOUT)")
    ap.add_argument("--endpoint", type=float, default=defaults.endpoint_s, help="silence after an STT final (HAL_ENDPOINT_SILENCE_S)")
    ap.add_argument("--min-ratio", type=float, default=defaults.min_ratio, help="noise guard voiced ratio (HAL_REALTIME_NOISE_SPEECH_RATIO)")
    ap.add_argument("--min-voiced-ms", type=float, default=defaults.min_voiced_ms, help="noise guard voiced time (HAL_VOICE_NOISE_MIN_VOICED_MS)")
    ap.add_argument("--json", action="store_true", help="one JSON object per utterance")
    args = ap.parse_args(argv)
    params = Params(rms=args.rms, holdoff_s=args.holdoff, silence_s=args.silence, endpoint_s=args.endpoint,
                    min_ratio=args.min_ratio, min_voiced_ms=args.min_voiced_ms)
    samples = load_wav(args.wav)
    utterances = segment(samples, RealGate(params), params)
    if args.json:
        for u in utterances:
            print(json.dumps(asdict(u)))
        return 0
    print(render(utterances, params, len(samples) / STT_RATE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
