"""Transfer SPI ownership from the optional systemd boot indicator to HAL."""

from pathlib import Path
import subprocess


def stop_boot_indicator():
    # Blocking stop waits for the indicator's final clear and process exit.
    # On failure the caller must not open SPI against a possible live writer.
    # Keep HAL-only updates compatible with the earlier device test package.
    for unit in ('lamp-led-boot.service', 'led-boot.service'):
        if Path('/etc/systemd/system', unit).exists():
            subprocess.run(['systemctl', 'stop', unit], check=True, timeout=5)
