"""Shared playback meter and expiring duck control for Hardware AEC live mode."""

import logging
import math
import re
import subprocess
import threading
import time

import numpy as np

from hal.drivers.voice._internal import config


def _enabled():
    # Profiles choose the canceller; no device-name checks or extra feature flag.
    return config.LIVE_MODE and not config.AEC_ENABLED


ENABLED = _enabled()
logger = logging.getLogger('hal.voice.live_playback')
_lock = threading.Lock()
_level = 0.0
_written = None
_duck_until = 0.0
_gain = 1.0
_target = 1.0
_ramp_remaining = 0
_ramp_step = 0.0
_last_log = float('-inf')
_mixer_gain = 1.0
_mixer_source = 'unknown'
_played_seconds = 0.0
_played_at = None


def record_written(frames, rate):
    """Count successful speaker writes, not generation or queue wait time."""
    global _played_seconds, _played_at
    if ENABLED and frames > 0 and rate > 0:
        with _lock:
            _played_seconds += frames / rate
            _played_at = time.monotonic()


def is_playing():
    """True only after a successful speaker write, not while TTS is pending."""
    with _lock:
        return (ENABLED and _played_at is not None
                and time.monotonic() - _played_at < 0.25)


def played_seconds():
    with _lock:
        return _played_seconds


def set_mixer_volume(percent, db=None, source='mixer'):
    """Meter actual mixer dB; percentages have device-specific mappings."""
    global _mixer_gain, _mixer_source
    if not ENABLED:
        return
    try:
        pct = float(percent)
        if not math.isfinite(pct) or not 0 <= pct <= 100:
            return
        decibels = float(db) if db is not None else 0.0
        if not math.isfinite(decibels):
            return
        gain = min(1.0, 10 ** (min(0.0, decibels) / 20.0))
    except (TypeError, ValueError, OverflowError):
        return
    with _lock:
        changed = abs(gain - _mixer_gain) > 0.00001 or source != _mixer_source
        _mixer_gain, _mixer_source = gain, source
    if changed:
        logger.info('[live-aec] playback mixer gain=%.5f volume=%.0f%% db=%.2f source=%s',
                    gain, pct, decibels, source)


def observe_mixer(control, output):
    """Consume existing amixer output on control/API threads, without I/O."""
    if not ENABLED:
        return False
    output = '\n'.join(line for line in output.splitlines() if 'Playback' in line)
    percentages = re.findall(r'\[(\d+)%\]', output)
    decibels = re.findall(r'\[(-?\d+(?:\.\d+)?)dB\]', output)
    if not percentages:
        return False
    set_mixer_volume(max(map(int, percentages)),
                     max(map(float, decibels)) if decibels else None)
    return True


def initialize_mixer():
    """Read softvol once during startup, never from capture/playback threads."""
    if not ENABLED:
        return
    try:
        result = subprocess.run(
            ['amixer', '-D', 'device_speaker', 'scontents'],
            capture_output=True, text=True, timeout=1.0, check=True,
        )
        if observe_mixer('Speaker', result.stdout):
            return
    except (OSError, subprocess.SubprocessError):
        pass
    logger.warning('[live-aec] mixer readback unavailable; using %s gain=%.5f',
                   _mixer_source, _mixer_gain)


def level():
    """Return post-duck, post-softvol normalized RMS; expire after 250 ms."""
    with _lock:
        return (_level * _mixer_gain if ENABLED and _written is not None
                and time.monotonic() - _written < 0.25 else 0.0)


def duck(active):
    """Refresh the capture loop's request; expire it if capture stops running."""
    global _duck_until
    if not ENABLED:
        return
    with _lock:
        _duck_until = time.monotonic() + 0.5 if active else 0.0


def snapshot():
    """Return bounded diagnostics without including audio or transcript data."""
    with _lock:
        now = time.monotonic()
        age = None if _written is None else max(0.0, now - _written)
        return {
            'enabled': ENABLED,
            'gain': _gain,
            'mixer_gain': _mixer_gain,
            'mixer_source': _mixer_source,
            'last_write_age_ms': None if age is None else round(age * 1000, 1),
            'level': _level * _mixer_gain if ENABLED and age is not None and age < 0.25 else 0.0,
            'duck': ENABLED and now < _duck_until,
        }


def playback(chunk, rate):
    """Meter PCM and apply continuous 15 ms down / 80 ms up gain ramps.

    Bytes contain mono int16 PCM; arrays preserve dtype and channel layout.
    The caller must send the returned PCM to both the speaker and AEC.
    """
    global _level, _written, _gain, _target, _ramp_remaining, _ramp_step, _last_log
    if not ENABLED:
        return chunk
    is_bytes = isinstance(chunk, (bytes, bytearray, memoryview))
    data = np.frombuffer(chunk, dtype=np.int16) if is_bytes else np.asarray(chunk)
    if not data.size or rate <= 0:
        return chunk
    # Convert before squaring to avoid int16 overflow at loud playback levels.
    values = data.astype(np.float32)
    scale = 32768.0 if data.dtype == np.int16 else 1.0
    with _lock:
        _written = time.monotonic()
        target = config.LIVE_DUCK_GAIN if _written < _duck_until else 1.0
        if target != _target:
            _target = target
            _ramp_remaining = max(1, int(rate * (0.015 if target < _gain else 0.08)))
            _ramp_step = (target - _gain) / _ramp_remaining
            if _written - _last_log >= 0.5:
                logger.info('[live-aec] playback gain %.3f -> %.3f level=%.4f',
                            _gain, target, float(np.sqrt(np.mean((values / scale) ** 2))))
                _last_log = _written
        ramp = min(len(data), _ramp_remaining)
        gains = np.full(len(data), target, dtype=np.float32)
        if ramp:
            gains[:ramp] = _gain + _ramp_step * np.arange(1, ramp + 1)
            _ramp_remaining -= ramp
            _gain = target if not _ramp_remaining else float(gains[ramp - 1])
        else:
            _gain = target
        if data.ndim > 1:
            gains = gains[:, None]
        result = (values * gains).astype(data.dtype)
        # Match the demo: echo estimation observes the PCM sent to the speaker,
        # including the duck ramp, rather than the unattenuated synthesis PCM.
        _level = float(np.sqrt(np.mean((result.astype(np.float32) / scale) ** 2)))
    return result.tobytes() if is_bytes else result


initialize_mixer()
