"""Safety policy layer: parse SAFETY.md bounds and expose pure, deterministic gate functions.

Invariant: a declared bound is enforced, an absent one is pass-through (never invent a limit).
Unknown schema majors and out-of-range bounds fail boot. See robots/contract/SAFETY-SPEC.md.
"""
from __future__ import annotations

import logging
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import time as dtime
from typing import Optional, Tuple

from hal.clock import device_now

logger = logging.getLogger("hal.safety")

# ABI tag: fields are only added within a major; an unknown major fails loud.
SCHEMA_NAMESPACE = "autonomous.safety"
SUPPORTED_SCHEMA_MAJORS = frozenset({1})

_RE_SCHEMA = re.compile(r"^schema:\s*(\S+)\s*$", re.MULTILINE)
_RE_SCHEMA_VERSION = re.compile(r"^" + re.escape(SCHEMA_NAMESPACE) + r"\.v(\d+)$")

MAX_CHANNEL = 255  # 8-bit per-channel RGB ceiling
MAX_VOLUME_PCT = 100  # speaker volume is a percentage, the same scale /audio/volume takes


@dataclass(frozen=True)
class QuietHours:
    """A daily window (may wrap midnight); `max_brightness` is the in-window LED ceiling (light only)."""
    start: dtime
    end: dtime
    max_brightness: Optional[int] = None


@dataclass(frozen=True)
class MotionBounds:
    # deg/s ceiling, enforced by stretching move duration (target still reached). None = undeclared.
    max_speed: Optional[int] = None
    # Stop/release/zero/hold are recovery actions: never gated or refused.
    stop_always: bool = False
    # Max CoG offset (mm) from the base axis; needs ROBOT.md `urdf_ref`. None = undeclared.
    max_cog_offset_mm: Optional[int] = None


@dataclass(frozen=True)
class ThermalBounds:
    # SoC °C at/above which motion is stopped; use the board's own critical trip point.
    max_temp_c: int
    # Hysteresis clear threshold; defaults to max_temp_c - 10.
    resume_temp_c: int


@dataclass(frozen=True)
class SafetyPolicy:
    schema: str
    # 0-255 LED ceiling. None = pass-through (light fail-safe).
    max_brightness: Optional[int] = None
    light_quiet: Optional[QuietHours] = None   # nightly reduced LED ceiling
    audio_quiet: Optional[QuietHours] = None    # nightly window: suppress loud audio
    # 0-100 % all-day speaker ceiling. None = pass-through.
    max_volume: Optional[int] = None
    motion: Optional[MotionBounds] = None
    thermal: Optional[ThermalBounds] = None


def extract_front_matter(text: str) -> str:
    """Return the YAML front-matter block (between the first two '---' fences)."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    return m.group(1) if m else ""


def validate_schema(front_matter: str) -> str:
    """Validate the `schema:` ABI tag and return it; raises ValueError on missing/unknown major."""
    m = _RE_SCHEMA.search(front_matter)
    if not m:
        raise ValueError(
            f"SAFETY.md front matter is missing 'schema:' "
            f"(expected '{SCHEMA_NAMESPACE}.v<major>')"
        )
    schema = m.group(1)
    v = _RE_SCHEMA_VERSION.match(schema)
    if not v:
        raise ValueError(
            f"SAFETY.md schema '{schema}' is not a valid '{SCHEMA_NAMESPACE}.v<major>' tag"
        )
    major = int(v.group(1))
    if major not in SUPPORTED_SCHEMA_MAJORS:
        raise ValueError(
            f"SAFETY.md schema '{schema}' has major v{major}; this runtime supports "
            f"majors {sorted(SUPPORTED_SCHEMA_MAJORS)}"
        )
    return schema


def _section_body(front_matter: str, key: str) -> str:
    """Body of a top-level `key:` section (flow or block style), or '' if absent."""
    flow = re.search(
        r"^" + re.escape(key) + r":[ \t]*\{(.*?)\}[ \t]*$",
        front_matter, re.MULTILINE | re.DOTALL,
    )
    if flow:
        return flow.group(1)
    block = re.search(
        r"^" + re.escape(key) + r":[ \t]*\n((?:[ \t]+.*\n?)*)",
        front_matter, re.MULTILINE,
    )
    return block.group(1) if block else ""


def _int_field(body: str, name: str) -> Optional[int]:
    m = re.search(r"\b" + re.escape(name) + r":\s*(\d+)", body)
    return int(m.group(1)) if m else None


def _validate_brightness(val: Optional[int], where: str) -> Optional[int]:
    if val is not None and not (0 <= val <= MAX_CHANNEL):
        raise ValueError(f"SAFETY.md {where} {val} out of range 0–{MAX_CHANNEL}")
    return val


def _validate_volume(val: Optional[int], where: str) -> Optional[int]:
    if val is not None and not (0 <= val <= MAX_VOLUME_PCT):
        raise ValueError(f"SAFETY.md {where} {val} out of range 0–{MAX_VOLUME_PCT}")
    return val


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    hi, mi = int(h), int(m)
    if not (0 <= hi <= 23 and 0 <= mi <= 59):
        raise ValueError(f"SAFETY.md quiet_hours time '{s}' is not a valid HH:MM")
    return dtime(hour=hi, minute=mi)


def _parse_quiet_hours(section_body: str, *, with_brightness: bool) -> Optional[QuietHours]:
    """Parse `quiet_hours: { start: "HH:MM", end: "HH:MM"[, max_brightness: N] }`, or None."""
    m = re.search(r"quiet_hours:\s*\{([^}]*)\}", section_body)
    if not m:
        return None
    body = m.group(1)
    ms = re.search(r'start:\s*"?(\d{1,2}:\d{2})"?', body)
    me = re.search(r'end:\s*"?(\d{1,2}:\d{2})"?', body)
    if not ms or not me:
        raise ValueError("SAFETY.md quiet_hours requires both 'start' and 'end' (HH:MM)")
    mb = _validate_brightness(_int_field(body, "max_brightness"), "quiet_hours.max_brightness") if with_brightness else None
    return QuietHours(start=_parse_hhmm(ms.group(1)), end=_parse_hhmm(me.group(1)), max_brightness=mb)


def _parse_motion(motion_body: str) -> Optional[MotionBounds]:
    """Parse the `motion:` section into MotionBounds, or None if it declares no bounds."""
    max_speed = _int_field(motion_body, "max_speed")
    if max_speed is not None and max_speed <= 0:
        raise ValueError(f"SAFETY.md motion.max_speed {max_speed} must be > 0 (deg/s)")
    stop_always = bool(re.search(r"\bstop_always:\s*true\b", motion_body))
    max_cog = _int_field(motion_body, "max_cog_offset_mm")
    if max_cog is not None and max_cog <= 0:
        raise ValueError(
            f"SAFETY.md motion.max_cog_offset_mm {max_cog} must be > 0 (mm)"
        )
    if max_speed is None and not stop_always and max_cog is None:
        return None
    return MotionBounds(
        max_speed=max_speed, stop_always=stop_always, max_cog_offset_mm=max_cog
    )


def _parse_thermal(thermal_body: str) -> Optional[ThermalBounds]:
    """Parse the `thermal:` section into ThermalBounds, or None without `max_temp_c`."""
    max_temp = _int_field(thermal_body, "max_temp_c")
    if max_temp is None:
        return None
    if max_temp <= 0:
        raise ValueError(f"SAFETY.md thermal.max_temp_c {max_temp} must be > 0 (°C)")
    resume = _int_field(thermal_body, "resume_temp_c")
    if resume is None:
        resume = max_temp - 10
    if resume >= max_temp:
        raise ValueError(
            f"SAFETY.md thermal.resume_temp_c {resume} must be < max_temp_c {max_temp}"
        )
    return ThermalBounds(max_temp_c=max_temp, resume_temp_c=resume)


def parse_safety(text: str) -> SafetyPolicy:
    """Parse SAFETY.md text into a SafetyPolicy; raises on a bad schema or out-of-range bound."""
    fm = extract_front_matter(text)
    schema = validate_schema(fm)
    # Drop full-line comments so commented-out placeholders aren't read as bounds.
    fm = "\n".join(ln for ln in fm.splitlines() if not ln.lstrip().startswith("#"))
    light_body = _section_body(fm, "light")
    audio_body = _section_body(fm, "audio")
    # Base ceilings exclude fields nested in quiet_hours.
    light_base_body = re.sub(r"quiet_hours:\s*\{[^}]*\}", "", light_body)
    audio_base_body = re.sub(r"quiet_hours:\s*\{[^}]*\}", "", audio_body)
    return SafetyPolicy(
        schema=schema,
        max_brightness=_validate_brightness(_int_field(light_base_body, "max_brightness"), "light.max_brightness"),
        light_quiet=_parse_quiet_hours(light_body, with_brightness=True),
        audio_quiet=_parse_quiet_hours(audio_body, with_brightness=False),
        max_volume=_validate_volume(_int_field(audio_base_body, "max_volume"), "audio.max_volume"),
        motion=_parse_motion(_section_body(fm, "motion")),
        thermal=_parse_thermal(_section_body(fm, "thermal")),
    )


def _now() -> dtime:
    """Device-local wall-clock time in the current timezone (tests pass `now`)."""
    return device_now().time()


def in_window(window: QuietHours, now: dtime) -> bool:
    """True if `now` is inside the window, handling wrap past midnight (e.g. 22:00->07:00)."""
    if window.start <= window.end:
        return window.start <= now < window.end
    return now >= window.start or now < window.end


def active_max_brightness(policy: Optional[SafetyPolicy], now: Optional[dtime] = None) -> Optional[int]:
    """The LED ceiling in effect now (lowered inside the light quiet window); None = pass-through."""
    if policy is None:
        return None
    if now is None:
        now = _now()
    base = policy.max_brightness
    q = policy.light_quiet
    if q is not None and q.max_brightness is not None and in_window(q, now):
        return q.max_brightness if base is None else min(base, q.max_brightness)
    return base


def clamp_brightness(policy: Optional[SafetyPolicy], value: int, now: Optional[dtime] = None) -> int:
    """Clamp a 0-255 brightness to the ceiling in effect now; pass-through without one."""
    ceiling = active_max_brightness(policy, now)
    return value if ceiling is None else min(value, ceiling)


def clamp_color(
    policy: Optional[SafetyPolicy], color: Tuple[int, int, int], now: Optional[dtime] = None
) -> Tuple[int, int, int]:
    """Scale (r,g,b) so the brightest channel respects the ceiling, preserving hue.

    Example: ceiling 180 -> (255,0,0) becomes (180,0,0).
    """
    ceiling = active_max_brightness(policy, now)
    if ceiling is None:
        return color
    r, g, b = color
    peak = max(r, g, b)
    if peak <= ceiling:
        return color
    scale = ceiling / peak
    return (round(r * scale), round(g * scale), round(b * scale))


def audio_quiet_now(policy: Optional[SafetyPolicy], now: Optional[dtime] = None) -> bool:
    """True inside the declared audio quiet-hours window (suppress music)."""
    if policy is None or policy.audio_quiet is None:
        return False
    if now is None:
        now = _now()
    return in_window(policy.audio_quiet, now)


def max_volume_pct(policy: Optional[SafetyPolicy]) -> Optional[int]:
    """The declared speaker ceiling (%), or None."""
    return policy.max_volume if policy is not None else None


def clamp_volume(policy: Optional[SafetyPolicy], value: int) -> int:
    """Clamp a requested speaker volume (%) to the declared ceiling (0-100 scale clamp always applies)."""
    value = max(0, min(MAX_VOLUME_PCT, value))
    ceiling = max_volume_pct(policy)
    return value if ceiling is None else min(value, ceiling)


def cap_speed_dps(policy: Optional[SafetyPolicy], requested: float) -> float:
    """Speed to use for streaming followers (deg/s): `requested`, capped by `motion.max_speed`."""
    if policy is None or policy.motion is None or policy.motion.max_speed is None:
        return requested
    return min(float(requested), float(policy.motion.max_speed))


def min_move_duration(
    policy: Optional[SafetyPolicy],
    target: dict,
    current: dict,
    requested: float,
) -> float:
    """Duration for a move, stretched so the fastest joint stays within motion.max_speed.

    target/current: {joint: degrees}; joints missing from `current` are ignored.
    """
    if policy is None or policy.motion is None or policy.motion.max_speed is None:
        return requested
    max_delta = 0.0
    for joint, tgt in target.items():
        cur = current.get(joint)
        if cur is None:
            continue
        max_delta = max(max_delta, abs(float(tgt) - float(cur)))
    needed = max_delta / policy.motion.max_speed
    return max(requested, needed)


def thermal_over(policy: Optional[SafetyPolicy], temp_c: Optional[float], was_over: bool) -> bool:
    """Hysteresis gate for SoC over-temperature: trip at `max_temp_c`, clear at `resume_temp_c`.

    Returns False when monitoring is off or temp is unreadable.
    """
    if policy is None or policy.thermal is None or temp_c is None:
        return False
    t = policy.thermal
    if was_over:
        return temp_c > t.resume_temp_c
    return temp_c >= t.max_temp_c


_THERMAL_ZONE = "/sys/class/thermal/thermal_zone0/temp"


def read_soc_temp_c(path: str = _THERMAL_ZONE) -> Optional[float]:
    """SoC temperature in °C from the kernel thermal zone, or None (never raises)."""
    try:
        with open(path, "r") as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        return None


def _read_ref(device_dir: str, ref: str) -> str:
    """Resolve a *_ref to text: an http(s) URL is downloaded, else read relative to the device dir."""
    if ref.startswith("http://") or ref.startswith("https://"):
        with urllib.request.urlopen(ref, timeout=30) as r:  # noqa: S310 (device-trusted ref)
            return r.read().decode("utf-8")
    with open(os.path.join(device_dir, ref), "r") as f:
        return f.read()


def load_safety(device_dir: str, safety_ref: str) -> Optional[SafetyPolicy]:
    """Resolve `safety_ref` and parse the bounds, or None (with a WARN) when nothing is enforceable.

    Front matter with a bad schema or out-of-range bound raises and aborts boot.
    """
    if not safety_ref:
        return None
    try:
        text = _read_ref(device_dir, safety_ref)
    except Exception as e:
        logger.warning(
            "[safety] cannot read safety_ref %r: %s — bounds not enforced", safety_ref, e
        )
        return None
    if not extract_front_matter(text):
        logger.warning(
            "[safety] %s has no machine front matter — bounds not enforced (prose only)",
            safety_ref,
        )
        return None
    return parse_safety(text)  # validates schema fail-loud
