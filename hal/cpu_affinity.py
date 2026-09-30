"""Keep latency-critical threads on the fast cores of a big.LITTLE board, and heavy background work off them.

Linux applies sched_setaffinity(0, ...) to the calling thread only, so each thread pins itself as it
starts; children (ffmpeg) are pinned by pid.

Which cores are fast comes from a fixed table of ARM core models (the "CPU part" of each core in
/proc/cpuinfo), not from a guess: a core model missing from the table turns pinning off, and a
board whose cores are all one tier (Pi 5: 4x A76, Pi 4/CM4: 4x A72) is never pinned.

HAL_CPU_PINNING=1 turns it on; HAL_FAST_CPUS="6,7" overrides the table.
"""

from __future__ import annotations

import functools
import logging
import os
from typing import Callable, Optional

logger = logging.getLogger("hal.cpu_affinity")

FAST = "fast"
SLOW = "slow"

CPUINFO_PATH = "/proc/cpuinfo"
_ARM = "0x41"

# ARM "CPU part" -> (name, tier). Efficiency cores are SLOW, everything bigger is FAST.
# lamp (Allwinner sun60iw2): cpu0-5 0xd05, cpu6-7 0xd0b.
CORE_MODELS: dict[str, tuple[str, str]] = {
    "0xd04": ("Cortex-A35", SLOW),
    "0xd03": ("Cortex-A53", SLOW),
    "0xd05": ("Cortex-A55", SLOW),
    "0xd46": ("Cortex-A510", SLOW),
    "0xd80": ("Cortex-A520", SLOW),
    "0xd07": ("Cortex-A57", FAST),
    "0xd08": ("Cortex-A72", FAST),
    "0xd09": ("Cortex-A73", FAST),
    "0xd0a": ("Cortex-A75", FAST),
    "0xd0b": ("Cortex-A76", FAST),
    "0xd0d": ("Cortex-A77", FAST),
    "0xd41": ("Cortex-A78", FAST),
    "0xd44": ("Cortex-X1", FAST),
    "0xd47": ("Cortex-A710", FAST),
    "0xd48": ("Cortex-X2", FAST),
    "0xd4d": ("Cortex-A715", FAST),
    "0xd4e": ("Cortex-X3", FAST),
    "0xd81": ("Cortex-A720", FAST),
    "0xd82": ("Cortex-X4", FAST),
}


def _parse_cpus(raw: str) -> frozenset[int]:
    cpus = set()
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            cpus.update(range(int(lo), int(hi) + 1))
        elif part:
            cpus.add(int(part))
    return frozenset(cpus)


def read_core_parts(path: str = CPUINFO_PATH) -> dict[int, tuple[str, str]]:
    """{cpu: (implementer, part)} from /proc/cpuinfo; {} when unreadable (macOS, x86 has no part)."""
    cores: dict[int, tuple[str, str]] = {}
    try:
        text = open(path, encoding="ascii", errors="replace").read()
    except OSError:
        return cores
    for block in text.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip().lower()
        if "processor" in fields and fields["processor"].isdigit() and "CPU part" in fields:
            cores[int(fields["processor"])] = (fields.get("CPU implementer", ""), fields["CPU part"])
    return cores


def _tiers(allowed: frozenset[int]) -> Optional[dict[int, str]]:
    """{cpu: tier} for every allowed core, or None if any core is not in CORE_MODELS."""
    parts = read_core_parts()
    tiers = {}
    for cpu in sorted(allowed):
        implementer, part = parts.get(cpu, ("", ""))
        model = CORE_MODELS.get(part) if implementer == _ARM else None
        if model is None:
            logger.info("[cpu] cpu%d implementer=%r part=%r not in CORE_MODELS — pinning off",
                        cpu, implementer, part)
            return None
        tiers[cpu] = model[1]
    return tiers


@functools.lru_cache(maxsize=1)
def core_sets() -> Optional[tuple[frozenset[int], frozenset[int]]]:
    """(fast, slow) CPU sets, or None when pinning is off, the cores are unknown, or all one tier."""
    if os.environ.get("HAL_CPU_PINNING", "0") != "1" or not hasattr(os, "sched_setaffinity"):
        return None
    allowed = frozenset(os.sched_getaffinity(0))
    override = os.environ.get("HAL_FAST_CPUS", "").strip()
    if override:
        try:
            fast = _parse_cpus(override) & allowed
        except ValueError:
            logger.warning("[cpu] bad HAL_FAST_CPUS=%r — pinning off", override)
            return None
    else:
        tiers = _tiers(allowed)
        if tiers is None:
            return None
        fast = frozenset(cpu for cpu, tier in tiers.items() if tier == FAST)
    slow = allowed - fast
    if not fast or not slow:
        logger.info("[cpu] no fast/slow split in %s — pinning off", sorted(allowed))
        return None
    logger.info("[cpu] pinning on: fast=%s slow=%s", sorted(fast), sorted(slow))
    return fast, slow


def _cpus_for(role: str) -> Optional[frozenset[int]]:
    sets = core_sets()
    if sets is None:
        return None
    return sets[0] if role == FAST else sets[1]


def pin_current_thread(role: str) -> bool:
    """Pin the calling thread (and threads/children it creates afterwards) to `role` cores."""
    return pin_pid(0, role)


def pin_pid(pid: int, role: str) -> bool:
    """Pin a thread id or child process; 0 means the calling thread. Never raises."""
    cpus = _cpus_for(role)
    if cpus is None:
        return False
    try:
        os.sched_setaffinity(pid, cpus)
    except OSError as e:
        logger.warning("[cpu] pin %s to %s failed: %s", pid or "self", role, e)
        return False
    return True


def on_cores(role: str, fn: Callable[..., None]) -> Callable[..., None]:
    """Wrap a thread target so the thread pins itself before running `fn`."""

    @functools.wraps(fn)
    def run(*args, **kwargs):
        pin_current_thread(role)
        return fn(*args, **kwargs)

    return run
