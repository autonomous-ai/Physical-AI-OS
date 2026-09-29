#!/usr/bin/env python3
"""Defer OrangePi's boot-only MOTD package count, not package updates."""

import argparse
from pathlib import Path
import re
import shutil


def apply(root: Path) -> bool:
    cron = root / "etc/cron.d/orangepi-updates"
    if not cron.is_file():
        return False
    original = cron.read_text()
    updated = re.sub(
        r"^(@reboot[ \t]+root[ \t]+)(/usr/lib/orangepi/orangepi-apt-updates)[ \t]*$",
        r"\1/bin/sleep 120 && \2",
        original,
        flags=re.MULTILINE,
    )
    if updated == original:
        return False
    backup = root / "var/backups/autonomous/orangepi-updates.before-boot-delay"
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        shutil.copy2(cron, backup)
    # Preserve the cron file's ownership and permissions.
    cron.write_text(updated)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/"))
    args = parser.parse_args()
    print("Deferred OrangePi boot update count by 120s" if apply(args.root) else "No change needed")
