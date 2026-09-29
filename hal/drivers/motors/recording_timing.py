"""Recording playback timing — the one place that decides when a frame plays."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from hal.drivers.motors.recording_stability import check_stable

logger = logging.getLogger("hal.motion.timing")

# Peak joint speed the STS3215 can actually deliver, in degrees/second.
SERVO_MAX_DPS = float(os.environ.get("HAL_SERVO_MAX_DPS", "250"))

def effective_max_dps(policy: Any = None) -> float:
    """The speed ceiling a replay must respect: hardware limit, then policy."""
    if SERVO_MAX_DPS <= 0 or policy is None:
        return SERVO_MAX_DPS
    from hal.safety.policy import cap_speed_dps

    return cap_speed_dps(policy, SERVO_MAX_DPS)


RECORDING_TIME_COLUMN = "timestamp"


def stretch_timeline(
    times: List[float], frames: List[Dict[str, float]], policy: Any = None
) -> List[float]:
    """Widen the gaps that demand more joint speed than the servo can deliver."""
    max_dps = effective_max_dps(policy)
    if max_dps <= 0:
        return times

    out = [times[0]]
    for i in range(1, len(frames)):
        authored_dt = max(times[i] - times[i - 1], 1e-3)
        peak_delta = max(
            (abs(frames[i][j] - frames[i - 1][j]) for j in frames[i]),
            default=0.0,
        )
        needed_dt = peak_delta / max_dps
        out.append(out[-1] + max(authored_dt, needed_dt))
    return out


def resample_recording(
    times: List[float],
    frames: List[Dict[str, float]],
    name: str,
    fps: float,
    policy: Any = None,
    geometry: Any = None,
) -> List[Dict[str, float]]:
    """Put frames on a playback loop's own 1/fps grid.

    Resampling only stretches time, never moves a joint, so checking the authored frames
    covers the played ones.
    """
    check_stable(frames, name, policy, geometry)
    stretched = stretch_timeline(times, frames, policy)
    duration = stretched[-1] - stretched[0]
    if duration <= 0:
        return frames

    joints = list(frames[0].keys())
    step = 1.0 / fps
    total = max(1, int(round(duration / step)))

    out: List[Dict[str, float]] = []
    src = 0
    for k in range(total + 1):
        t = stretched[0] + min(k * step, duration)
        while src < len(stretched) - 2 and stretched[src + 1] < t:
            src += 1
        span = stretched[src + 1] - stretched[src]
        p = 0.0 if span <= 0 else (t - stretched[src]) / span
        p = max(0.0, min(1.0, p))
        a, b = frames[src], frames[src + 1]
        out.append({j: a[j] + (b[j] - a[j]) * p for j in joints})

    authored = times[-1] - times[0]
    max_dps = effective_max_dps(policy)
    if max_dps > 0 and duration > authored * 1.01:
        logger.info(
            "recording %r stretched %.2fs -> %.2fs to stay under %.0f deg/s",
            name, authored, duration, max_dps,
        )
    return out
