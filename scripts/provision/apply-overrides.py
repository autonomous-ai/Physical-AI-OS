#!/usr/bin/env python3
"""Render an explicitly selected hardware overlay into a staged device package.

No hardware-profile file (or empty/standard) preserves the legacy package.
Product names, audio routing and tuning belong to the package's overrides/ data.
"""

import argparse
import json
from pathlib import Path
import re
import shutil


PROFILE_PATH = "etc/autonomous/hardware-profile"


def selected_profile(root):
    path = root / PROFILE_PATH
    name = path.read_text().strip() if path.exists() else ""
    if name in ("", "standard"):
        return None
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
        raise ValueError("invalid hardware-profile name")
    return name


def merge_env(base, override):
    for line in override.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, _ = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("invalid environment override")
        pattern = rf"^{re.escape(key)}=.*$"
        if re.search(pattern, base, re.MULTILINE):
            base = re.sub(pattern, lambda _: line, base, flags=re.MULTILINE)
        else:
            base = base.rstrip() + "\n" + line + "\n"
    return base


def volume_field(path, pattern, value):
    if type(value) is not int or not 0 <= value <= 100:
        raise ValueError("volume override must be an integer from 0 to 100")
    parts = path.read_text().split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        raise ValueError(f"{path.name}: missing front matter")
    parts[1], count = re.subn(pattern, lambda m: m.group(1) + str(value), parts[1], flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"{path.name}: expected one volume field")
    return "---".join(parts)


def capability_fields(text, capabilities):
    """Toggle declared inline capability maps without rewriting the document."""
    if not isinstance(capabilities, dict):
        raise ValueError("capabilities override must be an object")
    parts = text.split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        raise ValueError("ROBOT.md: missing front matter")
    sections = list(re.finditer(r"^capabilities:[ \t]*$", parts[1], re.MULTILINE))
    if len(sections) != 1:
        raise ValueError("ROBOT.md: expected one capabilities section")
    start = sections[0].end()
    end_match = re.search(r"^[^\s#]", parts[1][start:], re.MULTILINE)
    end = start + end_match.start() if end_match else len(parts[1])
    block = parts[1][start:end]
    for name, enabled in capabilities.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or type(enabled) is not bool:
            raise ValueError("capability overrides require valid names and boolean values")
        pattern = rf"^(  )(?:#[ \t]*)?({re.escape(name)}:[ \t]*\{{[^\n]*\}}[^\n]*)$"
        block, count = re.subn(pattern, lambda m: m.group(1) + ("" if enabled else "# ") + m.group(2), block, flags=re.MULTILINE)
        if count != 1:
            raise ValueError(f"ROBOT.md: expected one declaration for capability {name}")
    parts[1] = parts[1][:start] + block + parts[1][end:]
    return "---".join(parts)


def apply_overrides(profile, root):
    name = selected_profile(root)
    if name is None:
        return None
    overlay = profile / "overrides" / name
    settings = json.loads((overlay / "profile.json").read_text())
    if not isinstance(settings, dict) or set(settings) - {"startup_volume", "max_volume", "capabilities"}:
        raise ValueError("unknown profile override fields")
    if {"startup_volume", "max_volume"} <= set(settings) and settings["startup_volume"] > settings["max_volume"]:
        raise ValueError("startup volume exceeds ceiling")
    outputs = {}
    for key, filename, pattern in (
        ("startup_volume", "ROBOT.md", r"^(startup_volume:\s*)[^\n]*$"),
        ("max_volume", "SAFETY.md", r"^(  max_volume:\s*)[^\n]*$"),
    ):
        if key in settings:
            outputs[profile / filename] = volume_field(profile / filename, pattern, settings[key])
    if "capabilities" in settings:
        robot = profile / "ROBOT.md"
        outputs[robot] = capability_fields(outputs.get(robot, robot.read_text()), settings["capabilities"])
    device = overlay / "device"
    if device.exists():
        for source in device.iterdir():
            target = profile / source.name
            if source.is_symlink() or not source.is_file() or source.suffix != ".json" or not target.is_file() or target.is_symlink():
                raise ValueError("device overrides must replace existing top-level JSON files")
            text = source.read_text()
            if not isinstance(json.loads(text), dict):
                raise ValueError(f"{source.name}: device override must be a JSON object")
            outputs[target] = text
    files = []
    for source in (overlay / "rootfs").rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(overlay / "rootfs")
        if relative.as_posix() == PROFILE_PATH:
            raise ValueError("hardware-profile identity cannot be part of an overlay")
        target = profile / "rootfs" / relative
        if relative.as_posix() == "opt/hal/.env":
            outputs[target] = merge_env(target.read_text(), source.read_text())
        else:
            files.append((source, target))
    # A saved gain for different hardware must not override its default, even
    # when the selected overlay only changes routing or volume policy.
    env_path = profile / "rootfs/opt/hal/.env"
    if env_path.exists():
        env = outputs.get(env_path, env_path.read_text())
        config = re.search(r"^OS_CONFIG_PATH=(.+)$", env, re.MULTILINE)
        config_dir = Path(config.group(1) if config else "/root/config/config.json").parent
        outputs[env_path] = merge_env(env, f"HAL_VOLUME_STATE_PATH={config_dir / ('.volume-' + name)}")
    # Read/validate before writing. OTA owns the staging/publication transaction.
    for target, text in outputs.items():
        target.write_text(text)
    for source, target in files:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return name


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--root", type=Path, default=Path("/"))
    args = parser.parse_args()
    try:
        applied = apply_overrides(args.profile, args.root)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f"Hardware override failed: {exc}\n")
    print(f"Hardware overrides staged: {applied}" if applied else "Legacy profile unchanged")
