"""Acoustic echo cancellation for the mic path (WebRTC AEC3).

Provider-independent: the reference is tapped at the TTS output stream, so it
covers synthesized speech, queued speech and realtime native audio alike.

Requires `aec-audio-processing` (SWIG binding over libwebrtc-audio-processing-2).
It is NOT a hal dependency — absent or unloadable, every entry point here
degrades to a no-op and the voice path behaves exactly as before.

Wiring:
    tts/service.py  _WatchedStream.write  → reference_write()
    voice_service.py mic open             → wrap_mic()
"""

import logging
import os
import threading
import time
from functools import lru_cache
from math import gcd

logger = logging.getLogger("hal.voice.aec")

FRAME_MS = 10  # APM's fixed frame size
# Mean square of an int16 frame at RMS 1000 — well over the ~6 room floor
# measured on this hardware, so a 'dry and loud' frame really is the mic
# hearing the speaker with no reference to cancel it against.
_DRY_LOUD_MSQ = 1000.0 ** 2
SUPPORTED_RATES = (8000, 16000, 32000, 48000)

_lock = threading.Lock()
_canceller = None
_reference = None
_unavailable_logged = False
_playback_rate = None


class EchoReference:
    """FIFO of audio handed to the speaker, drained by the canceller.

    Bounded to `max_ms`: older audio is past any useful alignment and letting it
    pile up drifts the reference out of step with the mic.
    """

    def __init__(self, rate: int, max_ms: int = 500):
        self._max_ms = max_ms
        self._rate = rate
        self._buffer = bytearray()
        self._max_bytes = int(rate * 2 * max_ms / 1000)
        self._lock = threading.Lock()
        self._last_write = 0.0
        self._resampler = None

    def write(self, pcm: bytes) -> None:
        with self._lock:
            self._append(pcm)

    def _append(self, pcm: bytes) -> None:
        self._buffer.extend(pcm)
        if len(self._buffer) > self._max_bytes:
            del self._buffer[: len(self._buffer) - self._max_bytes]
        self._last_write = time.monotonic()

    def write_samples(self, samples, rate: int) -> None:
        """Resample on one continuous sample clock, serialized with reset."""
        import numpy as np

        mono = np.asarray(samples, dtype=np.float32).reshape(-1)
        if not len(mono):
            return
        with self._lock:
            if rate != self._rate:
                if self._resampler is None or self._resampler.src_rate != rate:
                    self._resampler = _ReferenceResampler(rate, self._rate)
                mono = self._resampler.process(mono)
            else:
                self._resampler = None
            pcm = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)
            self._append(pcm.tobytes())

    def read(self, nbytes: int):
        """Take the next `nbytes` of played audio; zero-pad if it ran dry.

        Returns `(pcm, underran)`. The flag matters: an all-zero reference is
        ambiguous on its own — it means either a genuine silence in the reply
        (nothing to cancel, mic is clean) or a starved FIFO (echo present, no
        reference to cancel it with). Only the caller that knows which can make
        a safe decision, so say it here instead of guessing downstream.
        """
        with self._lock:
            if len(self._buffer) >= nbytes:
                out = bytes(self._buffer[:nbytes])
                del self._buffer[:nbytes]
                return out, False
            out = bytes(self._buffer) + b"\x00" * (nbytes - len(self._buffer))
            self._buffer.clear()
            return out, True

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._resampler = None

    def idle_for(self) -> float:
        """Seconds since the last speaker write (inf if nothing was ever played)."""
        with self._lock:
            if self._last_write <= 0.0:
                return float("inf")
            return time.monotonic() - self._last_write


class EchoCanceller:
    """Runs mic audio through WebRTC's APM with played audio as the reference.

    `process()` is the only hot path: it buffers to APM's fixed 10 ms frames and
    returns exactly as many samples as the caller asked for, so it drops into an
    existing read loop without changing framing.
    """

    def __init__(self, rate: int, reference: EchoReference, delay_ms: int,
                 noise_suppression: bool, dump_dir: str = ""):
        self._rate = rate
        self._reference = reference
        self._frame_bytes = int(rate * FRAME_MS / 1000) * 2
        self._pending = bytearray()
        self._out = bytearray()
        self._apm = None
        self._dump = None
        self._erle_log_at = 0.0
        self._erle_mic = 0.0
        self._erle_out = 0.0
        self._erle_frames = 0
        self._erle_dry = 0
        self._erle_dry_loud = 0
        # True when the mic frame just returned was NOT fully cancelled, so
        # callers can refuse to act on it. See uncancelled().
        self._uncancelled = True

        from aec_audio_processing import AudioProcessor

        self._apm = AudioProcessor(
            enable_aec=True,
            enable_ns=noise_suppression,
            enable_agc=False,  # AGC rides the gain up under the bot's own voice
            enable_vad=False,  # hal runs webrtcvad/silero/ten-vad for that
        )
        self._apm.set_stream_format(rate, 1)
        self._apm.set_reverse_stream_format(rate, 1)
        self._apm.set_stream_delay(delay_ms)
        logger.info(
            "AEC3 active: %dHz, %dms frames, delay hint %dms, ns=%s, reference %dms",
            rate, FRAME_MS, delay_ms, noise_suppression, reference._max_ms,
        )
        if dump_dir:
            self._open_dump(dump_dir, rate)

    def reset(self) -> None:
        """Drop buffered audio after a route change or stream reopen."""
        self._pending.clear()
        self._out.clear()
        self._reference.clear()

    def process(self, pcm: bytes) -> bytes:
        """Cancel the speaker signal out of `pcm`, returning the same byte count.

        Primes with up to one 10 ms frame of silence on the first call; from then
        on input and output stay length-for-length.
        """
        want = len(pcm)
        self._pending.extend(pcm)
        uncancelled = False
        while len(self._pending) >= self._frame_bytes:
            mic = bytes(self._pending[: self._frame_bytes])
            del self._pending[: self._frame_bytes]
            played, underran = self._reference.read(self._frame_bytes)
            uncancelled = uncancelled or underran
            self._apm.process_reverse_stream(played)
            cleaned = self._apm.process_stream(mic)
            self._out.extend(cleaned)
            self._accumulate_erle(mic, cleaned, played, underran)
            if self._dump:
                self._dump["mic"].writeframes(mic)
                self._dump["ref"].writeframes(played)
                self._dump["out"].writeframes(cleaned)
        self._uncancelled = uncancelled
        if len(self._out) < want:
            self._out[:0] = b"\x00" * (want - len(self._out))
        out = bytes(self._out[:want])
        del self._out[:want]
        return out

    def close(self) -> None:
        self._apm = None
        if self._dump:
            for handle in self._dump.values():
                handle.close()
            self._dump = None

    def _accumulate_erle(
        self, mic: bytes, cleaned: bytes, played: bytes, underran: bool
    ) -> None:
        """Log echo return loss enhancement while the speaker is actually active.

        This is the number that says whether the canceller is doing anything:
        0 dB means it is not.

        Frames whose reference came back all-zero are counted SEPARATELY rather
        than skipped. The reference is a FIFO drained by the mic loop, so it can
        run dry while the speaker is still playing — after an arecord overrun
        (AecStream returns early without draining it), or whenever the tap falls
        out of step with playback. Those frames get no cancellation at all, and
        skipping them silently meant ERLE only ever reported the frames that
        happened to line up: a mostly-dry run logged a healthy-looking number,
        or nothing at all when no frame aligned. `dry` with a loud mic is the
        echo leak, stated directly.
        """
        import numpy as np

        m = np.frombuffer(mic, dtype=np.int16).astype(np.float32)
        m_msq = float(np.mean(m ** 2))
        if underran or not any(played):
            if underran:
                self._erle_dry += 1
                if m_msq > _DRY_LOUD_MSQ:
                    self._erle_dry_loud += 1
        else:
            c = np.frombuffer(cleaned, dtype=np.int16).astype(np.float32)
            self._erle_mic += m_msq
            self._erle_out += float(np.mean(c ** 2))
            self._erle_frames += 1
        now = time.monotonic()
        # Flush on total frames, not cancelled frames: an all-dry window has
        # _erle_frames == 0 forever and would never report itself.
        if (self._erle_frames + self._erle_dry) >= 100 and now - self._erle_log_at > 2.0:
            if self._erle_out > 0 and self._erle_mic > 0:
                import math

                erle = "%.1f dB" % (10 * math.log10(self._erle_mic / self._erle_out))
            else:
                erle = "n/a"
            logger.info(
                "AEC ERLE %s over %d cancelled frames; reference UNDERRAN on %d "
                "frames (%d of them with a loud mic = uncancelled echo)",
                erle,
                self._erle_frames,
                self._erle_dry,
                self._erle_dry_loud,
            )
            self._erle_log_at = now
            self._erle_mic = self._erle_out = 0.0
            self._erle_frames = 0
            self._erle_dry = self._erle_dry_loud = 0

    def _open_dump(self, dump_dir: str, rate: int) -> None:
        import wave

        os.makedirs(dump_dir, exist_ok=True)
        self._dump = {}
        for name in ("mic", "ref", "out"):
            handle = wave.open(os.path.join(dump_dir, f"aec_{name}.wav"), "wb")
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            self._dump[name] = handle
        logger.info("AEC dumping mic/ref/out wavs to %s", dump_dir)


class AecStream:
    """Mic stream wrapper that cancels echo out of every read.

    Wraps any object with the `sd.InputStream` read contract (also satisfied by
    ArecordStream): `read(frames) -> (int16 ndarray, overflowed)`.
    """

    def __init__(self, inner, canceller: EchoCanceller, tail_s: float, np):
        self._inner = inner
        self._canceller = canceller
        self._tail_s = tail_s
        self._np = np
        self._bypassed = True

    def __enter__(self):
        self._inner = self._inner.__enter__()
        self._canceller.reset()
        return self

    def __exit__(self, *args):
        return self._inner.__exit__(*args)

    def read(self, frames):
        data, overflowed = self._inner.read(frames)
        if overflowed:
            if self._canceller is not None:
                self._canceller._uncancelled = True
            return data, overflowed
        # Skip the APM entirely when nothing has played recently — echo only
        # exists near playback, and this is a 24/7 loop on an 8-core A55.
        idle = self._canceller._reference.idle_for()
        if idle > self._tail_s:
            if not self._bypassed:
                # Logged because `active()` keeps returning True here: callers
                # that relax an echo defence stay armed while the mic is running
                # raw. If this fires while TTS is still speaking, the reference
                # tap stopped early and the leak is downstream of it.
                logger.info(
                    "AEC bypassed — no speaker write for %.1fs (tail %.1fs)",
                    idle, self._tail_s,
                )
                self._canceller.reset()
                self._bypassed = True
            self._canceller._uncancelled = True
            return data, overflowed
        if self._bypassed:
            logger.info("AEC engaged — speaker active again")
        self._bypassed = False
        try:
            cleaned = self._canceller.process(data.tobytes())
        except Exception as e:
            # Mark the frame raw before handing it back. The bypass branch above
            # does this; forgetting it here left `_uncancelled` holding whatever
            # the PREVIOUS frame set, so a failure right after a successful frame
            # published full speaker bleed as "cancelled" — and the live uplink
            # gate, whose only echo defence is that flag, would send the
            # device's own voice up as the user's.
            self._canceller._uncancelled = True
            logger.warning("AEC process failed, passing mic through: %s", e)
            return data, overflowed
        return (
            self._np.frombuffer(cleaned, dtype=self._np.int16).reshape(frames, -1),
            overflowed,
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


def configure(rate: int) -> bool:
    """Build the canceller for a mic sample rate. Safe to call repeatedly.

    Returns False when AEC is off, the rate is unsupported, or the native
    binding is missing — callers then use the raw mic.
    """
    global _canceller, _reference, _unavailable_logged

    from hal.drivers.voice._internal import config as voice_cfg

    if not voice_cfg.AEC_ENABLED:
        return False
    if rate not in SUPPORTED_RATES:
        logger.warning("AEC disabled: %dHz not supported by the APM %s", rate, SUPPORTED_RATES)
        return False
    with _lock:
        if _canceller is not None and _canceller._rate == rate:
            return True
        if _canceller is not None:
            _canceller.close()
            _canceller = None
        try:
            _reference = EchoReference(rate, voice_cfg.AEC_REF_MS)
            _canceller = EchoCanceller(
                rate,
                _reference,
                voice_cfg.AEC_DELAY_MS,
                voice_cfg.AEC_NOISE_SUPPRESSION,
                voice_cfg.AEC_DUMP_DIR,
            )
        except Exception as e:
            _reference = None
            _canceller = None
            if not _unavailable_logged:
                _unavailable_logged = True
                logger.warning(
                    "AEC unavailable (%s) — mic runs uncancelled. Install with: "
                    "uv pip install aec-audio-processing",
                    e,
                )
            return False
        # The speaker can be initialized before the microphone. Prepare its
        # reference conversion before capture admits the first user turn.
        if _playback_rate is not None:
            prepare_reference(_playback_rate)
    return True


def active() -> bool:
    """Whether cancellation is actually running — configured AND binding present.

    Callers that relax an echo defence must gate on this, not on AEC_ENABLED:
    with the binding missing every entry point is a no-op and the mic still
    carries full speaker bleed.
    """
    return _canceller is not None


def uncancelled() -> bool:
    """Whether the mic frame just read went through WITHOUT real cancellation.

    True when the reference FIFO underran, the stream was bypassed, or the mic
    overran — in all three the frame still carries full speaker bleed. Any
    defence that a loudspeaker can trip (the live uplink gate in `cancelled`
    mode) must skip such a frame, or it is deciding on echo. Measured on
    lamp-ee17 25/08/2026: the reference underran on 86% of processed frames
    during a reply.
    """
    return _canceller is None or _canceller._uncancelled


def reference_idle_for() -> float:
    """Seconds since anything was last handed to the speaker (inf if never).

    The only signal that separates "the APM was bypassed because nothing is
    playing, so the frame is clean" from "the APM was starved while the speaker
    was live, so the frame is dirty" — `uncancelled()` is True in BOTH, and is
    the ordinary state of a quiet conversation.
    """
    ref = _reference
    if ref is None or _canceller is None:
        return float("inf")
    return ref.idle_for()


def wrap_mic(mic_ctx, rate: int, np):
    """Return `mic_ctx` wrapped with echo cancellation, or unchanged when off."""
    if not configure(rate):
        return mic_ctx
    from hal.drivers.voice._internal import config as voice_cfg

    return AecStream(mic_ctx, _canceller, voice_cfg.AEC_TAIL_S, np)


@lru_cache(maxsize=32)
def _reference_resample_filter(up: int, down: int, dtype: str):
    """Cache SciPy's default polyphase FIR for a reduced ratio and dtype.

    Speaker writes may be only a few milliseconds long. Rebuilding the same
    Kaiser filter for every write puts avoidable work on the playback thread.
    Keep the unscaled coefficients; the streaming resampler applies the
    upsampling gain once when it creates its per-stream state.
    """
    import numpy as np
    import scipy.signal

    max_rate = max(up, down)
    half_len = 10 * max_rate
    coefficients = scipy.signal.firwin(
        2 * half_len + 1, 1.0 / max_rate, window=("kaiser", 5.0)
    ).astype(np.dtype(dtype))
    coefficients.setflags(write=False)
    return coefficients


class _ReferenceResampler:
    """Causal polyphase FIR with chunk-independent timing and bounded history.

    Restarting resample_poly at every speaker write rounds up each chunk and
    pads each boundary with zeros. Here both the FIR history and rational
    sample phase survive writes. The short FIR delay is constant; it cannot
    accumulate into the render/capture clock drift caused by chunk rounding.
    """

    def __init__(self, src_rate: int, dst_rate: int):
        import numpy as np

        self.src_rate = src_rate
        g = gcd(src_rate, dst_rate)
        self._up, self._down = dst_rate // g, src_rate // g
        self._filter = _reference_resample_filter(
            self._up, self._down, np.dtype(np.float32).str,
        ) * self._up
        self._history_needed = (len(self._filter) - 1 + self._up - 1) // self._up
        self._history = np.empty(0, dtype=np.float32)
        self._start = self._received = self._emitted = 0

    def process(self, samples):
        import numpy as np
        from scipy.signal import upfirdn

        if not len(samples):
            return np.empty(0, dtype=np.float32)
        buf = np.concatenate((self._history, samples))
        self._received += len(samples)
        end = (self._received * self._up + self._down - 1) // self._down
        # _start is always a multiple of down: each local convolution lands
        # on the same global output grid, including fractional rate ratios.
        base = self._start * self._up // self._down
        filtered = upfirdn(self._filter, buf, up=self._up, down=self._down)
        out = filtered[self._emitted - base:end - base].copy()
        self._emitted = end
        start = max(0, self._received - self._history_needed)
        start = start // self._down * self._down
        self._history = buf[start - self._start:].copy()
        self._start = start
        return out


def prepare_playback(rate: int) -> None:
    """Remember the output rate and prime AEC outside the first speech write.

    Either device may open first: configure() completes preparation when the
    microphone opens later. No reference samples or playback events are emitted.
    """
    global _playback_rate
    _playback_rate = rate
    prepare_reference(rate)


def prepare_reference(rate: int) -> None:
    """Prime optional resampling before playback, without publishing audio.

    Importing SciPy and designing the first filter after a speaker write can
    starve the next write. Call before the first audio is handed to the device;
    reference_write still handles an unprepared or subsequently changed route.
    """
    canceller = _canceller
    if _reference is None or canceller is None or rate == canceller._rate:
        return
    started = time.monotonic()
    try:
        import numpy as np

        dst = canceller._rate
        g = gcd(dst, rate)
        _reference_resample_filter(dst // g, rate // g, np.dtype(np.float32).str)
    except Exception as e:
        logger.debug("AEC reference preparation skipped: %s", e)
    finally:
        elapsed_ms = (time.monotonic() - started) * 1000
        if elapsed_ms >= 50:
            logger.info("AEC reference preparation: source_rate=%d elapsed_ms=%.1f", rate, elapsed_ms)


def reference_write(samples, rate: int) -> None:
    """Record float32 audio on its way to the speaker. Never raises.

    Called from the TTS output stream write, i.e. at playback rate — which is
    the timing the mic sees. Tapping at synthesis instead would be wrong: TTS
    renders a sentence far faster than real time.
    """
    ref = _reference
    if ref is None or _canceller is None:
        return
    try:
        ref.write_samples(samples, rate)
    except Exception as e:
        logger.debug("AEC reference write skipped: %s", e)


def reset() -> None:
    """Drop canceller state — call after an audio route change."""
    if _canceller is not None:
        _canceller.reset()
