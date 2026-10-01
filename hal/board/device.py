"""Device profile layer: parse ROBOT.md capabilities into a HAL mount plan.

declared+present -> mount; declared+required+missing -> fail loud; otherwise skip.
"""
from __future__ import annotations

import logging
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger("hal.device")

# ABI tag: unknown major versions fail boot.
SCHEMA_NAMESPACE = "autonomous.device"
SUPPORTED_SCHEMA_MAJORS = frozenset({1})

# Fail-safe to max, never silent. Must equal Go's device.DefaultStartupVolume.
DEFAULT_STARTUP_VOLUME = 100

_RE_SCHEMA = re.compile(r"^schema:\s*(\S+)\s*$", re.MULTILINE)
_RE_SCHEMA_VERSION = re.compile(r"^" + re.escape(SCHEMA_NAMESPACE) + r"\.v(\d+)$")


@dataclass(frozen=True)
class Capability:
    group: str
    routes: List[str]
    required: bool
    driver: Optional[str] = None   # implementation family; motion selector (factory.py), others informational
    safety: Optional[str] = None
    # Process that holds this hardware and hands it over on request; None = HAL opens it directly.
    owner: Optional[str] = None


def extract_front_matter(text: str) -> str:
    """Return the YAML front-matter block (between the first two '---' fences)."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    return m.group(1) if m else ""


def _parse_routes(body: str) -> List[str]:
    m = re.search(r"routes:\s*\[([^\]]*)\]", body)
    if not m:
        return []
    return [r.strip() for r in m.group(1).split(",") if r.strip()]


def _parse_required(body: str) -> bool:
    m = re.search(r"required:\s*(true|false)", body, re.IGNORECASE)
    return bool(m and m.group(1).lower() == "true")


def _parse_safety(body: str) -> Optional[str]:
    m = re.search(r"safety:\s*([^\s,}]+)", body)
    return m.group(1) if m else None


def _parse_driver(body: str) -> Optional[str]:
    """The capability's `driver:` family, or None (selects the motion service class)."""
    m = re.search(r"driver:\s*([^\s,}]+)", body)
    return m.group(1) if m else None


def _parse_owner(body: str) -> Optional[str]:
    """The capability's `owner:` name, or None when HAL owns the hardware."""
    m = re.search(r"owner:\s*([^\s,}]+)", body)
    return m.group(1) if m else None


def parse_boards(front_matter: str) -> List[str]:
    """The `boards: [a, b, c]` flow list, or [] if absent."""
    m = re.search(r"^boards:\s*\[([^\]]*)\]", front_matter, re.MULTILINE)
    if not m:
        return []
    return [b.strip() for b in m.group(1).split(",") if b.strip()]


def _parse_scalar(front_matter: str, key: str) -> str:
    """A top-level `key: value` scalar from the front matter, trimmed, or ''."""
    m = re.search(r"^" + re.escape(key) + r":\s*(.+?)\s*$", front_matter, re.MULTILINE)
    return m.group(1) if m else ""


def _parse_startup_volume(front_matter: str) -> int:
    """The `startup_volume:` (0-100), else DEFAULT_STARTUP_VOLUME; mirrors Go's device.StartupVolume."""
    raw = _parse_scalar(front_matter, "startup_volume")
    try:
        v = int(raw)
    except ValueError:
        return DEFAULT_STARTUP_VOLUME
    return v if 0 <= v <= 100 else DEFAULT_STARTUP_VOLUME


def _parse_memory_backend(front_matter: str) -> str:
    """The `memory: { backend: <x> }` backend name, or '' (informational)."""
    m = re.search(r"^memory:\s*\{[^}]*\bbackend:\s*([^\s,}]+)", front_matter, re.MULTILINE)
    return m.group(1) if m else ""


def validate_schema(front_matter: str) -> str:
    """Validate the `schema:` ABI tag and return it; raises ValueError on missing/unsupported major."""
    m = _RE_SCHEMA.search(front_matter)
    if not m:
        raise ValueError(
            f"ROBOT.md is missing the required 'schema:' field "
            f"(expected '{SCHEMA_NAMESPACE}.v<major>')"
        )
    schema = m.group(1)
    v = _RE_SCHEMA_VERSION.match(schema)
    if not v:
        raise ValueError(
            f"ROBOT.md schema '{schema}' is not a valid '{SCHEMA_NAMESPACE}.v<major>' tag"
        )
    major = int(v.group(1))
    if major not in SUPPORTED_SCHEMA_MAJORS:
        raise ValueError(
            f"ROBOT.md schema '{schema}' has major v{major}; this runtime supports "
            f"majors {sorted(SUPPORTED_SCHEMA_MAJORS)}"
        )
    return schema


def parse_capabilities(front_matter: str) -> Dict[str, Capability]:
    """Parse the `capabilities:` block of ROBOT.md front matter.

    Example: audio: { routes: [audio, speaker, voice], required: true }
    """
    caps: Dict[str, Capability] = {}
    in_block = False
    block_indent: Optional[int] = None
    for line in front_matter.splitlines():
        if re.match(r"^capabilities:\s*$", line):
            in_block = True
            continue
        if not in_block:
            continue
        if line.strip() == "":
            continue
        indent = len(line) - len(line.lstrip())
        if block_indent is None:
            block_indent = indent
        if indent < block_indent:
            break
        m = re.match(r"^\s+([A-Za-z0-9_]+):\s*\{(.*)\}\s*$", line)
        if not m:
            continue
        group, body = m.group(1), m.group(2)
        caps[group] = Capability(
            group=group,
            routes=_parse_routes(body),
            required=_parse_required(body),
            driver=_parse_driver(body),
            safety=_parse_safety(body),
            owner=_parse_owner(body),
        )
    return caps


@dataclass(frozen=True)
class DeviceProfile:
    # device_type is the folder id (== `id`); `type` is the display-only form-factor category.
    device_type: str
    id: str
    name: str
    type: str
    schema: str
    boards: List[str]
    safety_ref: str
    urdf_ref: str
    memory_backend: str
    startup_volume: int
    capabilities: Dict[str, Capability]
    # `answer_overheard_speech: true` lets the realtime voice agent reply to speech
    # that is not addressed to the device (default: stay silent).
    answer_overheard_speech: bool = False

    def declared_routes(self) -> Dict[str, bool]:
        """route -> required. A route is required if ANY capability that
        declares it is required."""
        out: Dict[str, bool] = {}
        for cap in self.capabilities.values():
            for route in cap.routes:
                out[route] = out.get(route, False) or cap.required
        return out


def parse_device(device_type: str, text: str) -> DeviceProfile:
    front_matter = extract_front_matter(text)
    schema = validate_schema(front_matter)
    dev_id = _parse_scalar(front_matter, "id")
    # `id` must equal the folder it is mounted from; a mismatch is a deploy fault.
    if dev_id != device_type:
        raise ValueError(
            f"ROBOT.md id '{dev_id}' does not match its folder '{device_type}' — "
            f"id must equal the device folder name"
        )
    capabilities = parse_capabilities(front_matter)
    # A required `presence` needs at least one people sensor (vision or audio).
    presence = capabilities.get("presence")
    if presence and presence.required and "vision" not in capabilities and "audio" not in capabilities:
        raise ValueError(
            f"ROBOT.md for '{device_type}' declares 'presence: required: true' but no "
            f"people sensor — perceiving a user's identity/emotion needs 'vision' (face) "
            f"or 'audio' (voice). Add one, or drop presence to required: false."
        )
    return DeviceProfile(
        device_type=device_type,
        id=dev_id,
        name=_parse_scalar(front_matter, "name"),
        type=_parse_scalar(front_matter, "type"),
        schema=schema,
        boards=parse_boards(front_matter),
        safety_ref=_parse_scalar(front_matter, "safety_ref"),
        urdf_ref=_parse_scalar(front_matter, "urdf_ref"),
        memory_backend=_parse_memory_backend(front_matter),
        startup_volume=_parse_startup_volume(front_matter),
        capabilities=capabilities,
        answer_overheard_speech=_parse_scalar(front_matter, "answer_overheard_speech").lower() == "true",
    )


def validate_safety_refs(profile: DeviceProfile, safety_md_text: str) -> List[str]:
    """Check each capability's `safety: SAFETY.md#<anchor>` resolves; returns problem strings (empty = clean)."""
    problems: List[str] = []
    for cap in profile.capabilities.values():
        if not cap.safety:
            continue
        if not safety_md_text:
            problems.append(
                f"capability '{cap.group}' declares safety '{cap.safety}' but SAFETY.md is empty or missing"
            )
            continue
        m = re.match(r"SAFETY\.md#(.+)$", cap.safety)
        if not m:
            continue
        anchor = m.group(1)
        heading = re.compile(r"^##\s+" + re.escape(anchor) + r"\s*$", re.IGNORECASE | re.MULTILINE)
        if not heading.search(safety_md_text):
            problems.append(
                f"capability '{cap.group}' references '{cap.safety}' but no '## {anchor}' heading found in SAFETY.md"
            )
    return problems


def _read_ref(device_dir: str, ref: str) -> str:
    """Resolve a *_ref value to text: an http(s) URL is downloaded, else read relative to the device dir."""
    if ref.startswith("http://") or ref.startswith("https://"):
        with urllib.request.urlopen(ref, timeout=30) as r:  # noqa: S310 (device-trusted ref)
            return r.read().decode("utf-8")
    with open(os.path.join(device_dir, ref), "r") as f:
        return f.read()


# ROBOT.md is canonical; DEVICE.md is still accepted for devices in the field.
PROFILE_NAMES = ("ROBOT.md", "DEVICE.md")


def profile_path(device_dir: str) -> str:
    """The declaration file inside a device folder: ROBOT.md, else DEVICE.md."""
    for name in PROFILE_NAMES:
        candidate = os.path.join(device_dir, name)
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(device_dir, PROFILE_NAMES[0])


def load_device(device_type: str, devices_dir: str) -> DeviceProfile:
    """Load robots/<device_type>/ROBOT.md (or ROBOT.md) from a devices directory."""
    device_dir = os.path.join(devices_dir, device_type)
    with open(profile_path(device_dir), "r") as f:
        profile = parse_device(device_type, f.read())

    # safety_ref is optional; every problem is a warning, never a boot failure.
    if any(cap.safety for cap in profile.capabilities.values()):
        safety_text = ""
        if profile.safety_ref:
            try:
                safety_text = _read_ref(device_dir, profile.safety_ref)
            except Exception as e:
                logger.warning(
                    "[device] %s: cannot read safety_ref %r: %s",
                    device_type, profile.safety_ref, e,
                )
        else:
            logger.warning(
                "[device] %s declares per-capability safety refs but no top-level safety_ref",
                device_type,
            )
        for problem in validate_safety_refs(profile, safety_text):
            logger.warning("[device] %s: %s", device_type, problem)

    return profile


@dataclass(frozen=True)
class MountPlan:
    mounted: List[str]
    skipped: List[str]           # undeclared, or declared-optional-but-absent
    failed_required: List[str]   # declared + required + absent -> caller must raise

    @property
    def ok(self) -> bool:
        return not self.failed_required


def plan_mounts(declared: Dict[str, bool], available: Dict[str, bool]) -> MountPlan:
    """Pure mount planner.

    Args: declared route -> required (ROBOT.md); available route -> driver present.
    """
    mounted: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []
    for route in sorted(set(declared) | set(available)):
        if route not in declared:
            skipped.append(route)
        elif available.get(route, False):
            mounted.append(route)
        elif declared[route]:
            failed.append(route)
        else:
            skipped.append(route)
    return MountPlan(mounted=mounted, skipped=skipped, failed_required=failed)
