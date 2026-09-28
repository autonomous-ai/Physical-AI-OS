"""Whole-body stability gate for servo recordings — the tip-over check."""
from __future__ import annotations

import logging
import math
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("hal.motion.stability")


@dataclass(frozen=True)
class _Link:
    """One revolute joint and the link it carries, in chain order."""
    joint: str
    offset: Tuple[float, float, float]
    rpy: Tuple[float, float, float]
    axis: Tuple[float, float, float]
    mass: float


@dataclass(frozen=True)
class BodyGeometry:
    """The serial chain a stability score is computed against."""
    chain: List[_Link]
    root_mass: float

    @property
    def joints(self) -> frozenset:
        return frozenset(link.joint for link in self.chain)


def _floats(text: Optional[str], default: Tuple[float, float, float]) -> Tuple[float, float, float]:
    if not text:
        return default
    parts = [float(v) for v in text.split()]
    return (parts[0], parts[1], parts[2])


def parse_urdf(text: str) -> Optional[BodyGeometry]:
    """Read a URDF into the single serial chain of revolute joints from the root."""
    root = ET.fromstring(text)
    masses: Dict[str, float] = {}
    for link in root.findall("link"):
        mass_el = link.find("inertial/mass")
        masses[link.get("name", "")] = float(mass_el.get("value", 0.0)) if mass_el is not None else 0.0

    by_parent: Dict[str, List[ET.Element]] = {}
    children = set()
    for joint in root.findall("joint"):
        if joint.get("type") not in ("revolute", "continuous"):
            continue
        parent = joint.find("parent").get("link")
        by_parent.setdefault(parent, []).append(joint)
        children.add(joint.find("child").get("link"))

    roots = [name for name in masses if name not in children]
    if len(roots) != 1:
        logger.warning("URDF has %d root links; expected exactly one", len(roots))
        return None

    chain: List[_Link] = []
    current = roots[0]
    while current in by_parent:
        branches = by_parent[current]
        if len(branches) != 1:
            logger.warning(
                "URDF branches at link %r (%d children) — no single-chain CoG walk",
                current, len(branches),
            )
            return None
        joint = branches[0]
        origin = joint.find("origin")
        child = joint.find("child").get("link")
        chain.append(_Link(
            joint=joint.get("name", ""),
            offset=_floats(origin.get("xyz") if origin is not None else None, (0.0, 0.0, 0.0)),
            rpy=_floats(origin.get("rpy") if origin is not None else None, (0.0, 0.0, 0.0)),
            axis=_floats(joint.find("axis").get("xyz") if joint.find("axis") is not None else None,
                         (0.0, 0.0, 1.0)),
            mass=masses.get(child, 0.0),
        ))
        current = child

    if not chain:
        logger.warning("URDF declares no revolute joints — nothing to score")
        return None
    return BodyGeometry(chain=chain, root_mass=masses.get(roots[0], 0.0))


def load_geometry(device_dir: str, urdf_ref: str) -> Optional[BodyGeometry]:
    """Resolve `urdf_ref` (path or URL) and parse it, or None with a warning.

    Mirrors how `safety_ref` is resolved, and fails the same way: a declared but
    unreadable reference is a warning and pass-through, never a boot failure — a body
    that cannot be scored still has to move.
    """
    if not urdf_ref:
        return None
    try:
        from hal.safety.policy import _read_ref
        return parse_urdf(_read_ref(device_dir, urdf_ref))
    except Exception as e:
        logger.warning("urdf_ref %r could not be loaded (%s) — no stability gate", urdf_ref, e)
        return None


def _mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _mat_vec(m, v):
    return [sum(m[i][k] * v[k] for k in range(3)) for i in range(3)]


def _rpy(r: float, p: float, y: float):
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp,     cp * sr,                cp * cr],
    ]


def _axis_rot(axis: Tuple[float, float, float], angle: float):
    n = math.sqrt(sum(c * c for c in axis))
    x, y, z = (c / n for c in axis)
    s, c = math.sin(angle), math.cos(angle)
    t = 1.0 - c
    return [
        [t * x * x + c,     t * x * y - s * z, t * x * z + s * y],
        [t * x * y + s * z, t * y * y + c,     t * y * z - s * x],
        [t * x * z - s * y, t * y * z + s * x, t * z * z + c],
    ]


def cog_offset_mm(frame: Dict[str, float], geometry: BodyGeometry) -> float:
    """Horizontal distance (mm) from the base axis to the whole-body CoG."""
    angles = {k.removesuffix(".pos"): v for k, v in frame.items()}
    rot = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    pos = [0.0, 0.0, 0.0]
    total = geometry.root_mass
    moment = [0.0, 0.0, 0.0]
    for link in geometry.chain:
        step = _mat_vec(rot, list(link.offset))
        pos = [pos[i] + step[i] for i in range(3)]
        rot = _mat_mul(_mat_mul(rot, _rpy(*link.rpy)),
                       _axis_rot(link.axis, math.radians(angles.get(link.joint, 0.0))))
        total += link.mass
        moment = [moment[i] + link.mass * pos[i] for i in range(3)]
    if total <= 0:
        return 0.0
    return math.hypot(moment[0] / total, moment[1] / total) * 1000.0


def worst_frame(frames: Iterable[Dict[str, float]],
                geometry: BodyGeometry) -> Tuple[int, float, Dict[str, float]]:
    """``(index, offset_mm, frame)`` of the pose that reaches furthest out."""
    worst_i, worst_mm, worst_f = -1, 0.0, {}
    for i, frame in enumerate(frames):
        offset = cog_offset_mm(frame, geometry)
        if offset > worst_mm:
            worst_i, worst_mm, worst_f = i, offset, frame
    return worst_i, worst_mm, worst_f


def _pose(frame: Dict[str, float]) -> str:
    return " ".join(f"{k.removesuffix('.pos')}={v:.1f}" for k, v in sorted(frame.items()))


_WARN_FRACTION = 0.85


def check_stable(frames: Iterable[Dict[str, float]], name: str = "",
                 policy: object = None, geometry: Optional[BodyGeometry] = None) -> None:
    """Raise ``ValueError`` if any frame reaches too far off the base axis.

    Both halves are per-body declarations and both must be present: the ceiling from
    ``SAFETY.md`` ``motion.max_cog_offset_mm`` and the geometry from ``ROBOT.md``
    ``urdf_ref``.
    """
    motion = getattr(policy, "motion", None)
    ceiling = getattr(motion, "max_cog_offset_mm", None) if motion else None
    if ceiling is None:
        return
    if geometry is None:
        logger.warning(
            "%r: motion.max_cog_offset_mm is declared (%s mm) but this body has no "
            "usable urdf_ref — cannot score a pose without geometry, passing through",
            name, ceiling,
        )
        return

    frames = list(frames)
    if frames:
        # Every modelled joint must be present.
        missing = geometry.joints - {k.removesuffix(".pos") for k in frames[0]}
        if missing:
            logger.warning(
                "stability gate skipped for %r — frames do not drive %s, "
                "so this body's pose cannot be reconstructed",
                name, sorted(missing),
            )
            return

    index, worst, frame = worst_frame(frames, geometry)
    if worst > ceiling:
        logger.error(
            "REFUSED recording %r — frame %d/%d reaches %.1f mm off the base axis, "
            "over the %s mm tip-over ceiling. Pose: %s",
            name, index, len(frames), worst, ceiling, _pose(frame),
        )
        raise ValueError(
            f"recording {name!r} reaches {worst:.1f} mm off the base axis, over the "
            f"{ceiling} mm tip-over ceiling — refusing to play it"
        )
    if worst >= ceiling * _WARN_FRACTION:
        logger.warning(
            "recording %r passes but is close to the limit — frame %d/%d at %.1f mm "
            "of %s mm. Pose: %s",
            name, index, len(frames), worst, ceiling, _pose(frame),
        )
    else:
        logger.info(
            "recording %r stable — peak %.1f mm of %s mm at frame %d/%d",
            name, worst, ceiling, index, len(frames),
        )
