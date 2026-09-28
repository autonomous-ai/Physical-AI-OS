"""Bluetooth manager — bluetoothctl wrapper for BT audio (headset) routing."""

import json
import logging
import os
import pwd
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hal.bluetooth")

_STATE_DIR = Path(os.environ.get("HAL_BT_STATE_DIR", "/var/lib/hal"))
_STATE_FILE = _STATE_DIR / "bluetooth.json"

SCAN_TIMEOUT_S = 30
_DEVICE_LINE_RE = re.compile(r"^Device ([0-9A-F:]{17})\s+(.*)$", re.I)


def _run(args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _resolve_pulse_owner() -> tuple[Optional[str], Optional[int], Optional[int]]:
    """Locate the per-user PulseAudio: returns (XDG_RUNTIME_DIR, uid, gid)."""
    if os.environ.get("XDG_RUNTIME_DIR"):
        try:
            st = os.stat(os.environ["XDG_RUNTIME_DIR"])
            return os.environ["XDG_RUNTIME_DIR"], st.st_uid, st.st_gid
        except OSError:
            return os.environ["XDG_RUNTIME_DIR"], None, None
    try:
        for entry in os.scandir("/run/user"):
            sock = os.path.join(entry.path, "pulse", "native")
            if os.path.exists(sock):
                try:
                    uid = int(entry.name)
                    gid = pwd.getpwuid(uid).pw_gid
                except (KeyError, ValueError):
                    uid = gid = None
                return entry.path, uid, gid
    except FileNotFoundError:
        pass
    return None, None, None


_PULSE_RUNTIME_DIR, _PULSE_UID, _PULSE_GID = _resolve_pulse_owner()
if _PULSE_RUNTIME_DIR:
    logger.info(
        "PulseAudio runtime: dir=%s uid=%s gid=%s",
        _PULSE_RUNTIME_DIR, _PULSE_UID, _PULSE_GID,
    )
else:
    logger.warning("No PulseAudio runtime dir found — pactl calls will fail")


def pulse_popen_kwargs() -> dict:
    """Env + privilege-drop kwargs so a root-spawned PulseAudio client (pactl, paplay, …)
    reaches the per-user PA daemon.
    """
    env = os.environ.copy()
    if _PULSE_RUNTIME_DIR:
        env["XDG_RUNTIME_DIR"] = _PULSE_RUNTIME_DIR

    kwargs: dict = {"env": env}

    if (
        os.geteuid() == 0
        and _PULSE_UID is not None
        and _PULSE_UID != 0
    ):
        uid, gid = _PULSE_UID, _PULSE_GID

        def _drop_priv():
            if gid is not None:
                try:
                    os.setgroups([])
                except PermissionError:
                    pass
                os.setgid(gid)
            os.setuid(uid)

        kwargs["preexec_fn"] = _drop_priv

    return kwargs


def _pactl(args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    """Run pactl with the right env + identity to reach the per-user PA."""
    kwargs: dict = dict(
        capture_output=True, text=True, timeout=timeout, **pulse_popen_kwargs()
    )
    return subprocess.run(["pactl", *args], **kwargs)


def _parse_devices(out: str) -> list[dict]:
    devices: list[dict] = []
    for line in out.splitlines():
        m = _DEVICE_LINE_RE.match(line.strip())
        if m:
            devices.append({"mac": m.group(1).upper(), "name": m.group(2).strip()})
    return devices


def _device_info(mac: str) -> dict:
    info = {
        "mac": mac.upper(),
        "name": None,
        "paired": False,
        "connected": False,
        "trusted": False,
    }
    try:
        r = _run(["bluetoothctl", "info", mac], timeout=5)
        if r.returncode != 0:
            return info
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("Name:"):
                info["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("Paired:"):
                info["paired"] = "yes" in line.lower()
            elif line.startswith("Connected:"):
                info["connected"] = "yes" in line.lower()
            elif line.startswith("Trusted:"):
                info["trusted"] = "yes" in line.lower()
    except Exception as e:
        logger.warning("bluetoothctl info %s failed: %s", mac, e)
    return info


class BluetoothManager:
    def __init__(self):
        self._scan_thread: Optional[threading.Thread] = None
        self._scan_lock = threading.Lock()
        self._state = self._load_state()

    def _load_state(self) -> dict:
        try:
            if _STATE_FILE.exists():
                return json.loads(_STATE_FILE.read_text())
        except Exception as e:
            logger.warning("Loading BT state failed: %s", e)
        return {"active_mac": None}

    def _save_state(self) -> None:
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            _STATE_FILE.write_text(json.dumps(self._state, indent=2))
        except Exception as e:
            logger.warning("Saving BT state failed: %s", e)

    @property
    def active_mac(self) -> Optional[str]:
        return self._state.get("active_mac")

    def set_active_mac(self, mac: Optional[str]) -> None:
        self._state["active_mac"] = mac.upper() if mac else None
        self._save_state()

    def available(self) -> bool:
        try:
            r = _run(["bluetoothctl", "--version"], timeout=2)
            return r.returncode == 0
        except Exception:
            return False

    def scan_start(self, timeout_s: int = SCAN_TIMEOUT_S) -> None:
        """Kick off a time-boxed scan in the background. Idempotent."""
        with self._scan_lock:
            if self._scan_thread and self._scan_thread.is_alive():
                return

            def _scan():
                try:
                    _run(
                        ["bluetoothctl", "--timeout", str(timeout_s), "scan", "on"],
                        timeout=timeout_s + 5,
                    )
                except Exception as e:
                    logger.warning("BT scan failed: %s", e)

            t = threading.Thread(target=_scan, daemon=True, name="bt-scan")
            t.start()
            self._scan_thread = t

    def scan_active(self) -> bool:
        return self._scan_thread is not None and self._scan_thread.is_alive()

    def discovered_devices(self) -> list[dict]:
        """All devices BlueZ has seen — caller filters to unpaired for UI."""
        try:
            r = _run(["bluetoothctl", "devices"], timeout=5)
            return _parse_devices(r.stdout) if r.returncode == 0 else []
        except Exception as e:
            logger.warning("discovered_devices failed: %s", e)
            return []

    def paired_devices(self) -> list[dict]:
        try:
            r = _run(["bluetoothctl", "devices", "Paired"], timeout=5)
            if r.returncode != 0:
                r = _run(["bluetoothctl", "paired-devices"], timeout=5)
            base = _parse_devices(r.stdout) if r.returncode == 0 else []
        except Exception:
            base = []
        out = []
        for d in base:
            info = _device_info(d["mac"])
            if info["name"] is None:
                info["name"] = d["name"]
            out.append(info)
        return out

    def info(self, mac: str) -> dict:
        return _device_info(mac)

    def _le_toggle(self, enable: bool) -> None:
        """Toggle the controller's LE side at runtime via btmgmt — the flag only changes
        while the adapter is powered off, so power-cycle around it.
        """
        val = "on" if enable else "off"
        for args in (["power", "off"], ["le", val], ["power", "on"]):
            try:
                _run(["btmgmt", *args], timeout=5)
            except Exception as e:
                logger.warning("btmgmt %s failed: %s", " ".join(args), e)
        time.sleep(1.0)

    def pair(self, mac: str) -> bool:
        mac = mac.upper()
        try:
            _run(["bluetoothctl", "pair", mac], timeout=20)
        except Exception as e:
            logger.warning("pair %s failed: %s", mac, e)
        if not _device_info(mac)["paired"]:
            logger.info("pair %s failed in dual mode — retrying BR/EDR-only", mac)
            self._le_toggle(False)
            try:
                _run(["bluetoothctl", "pair", mac], timeout=30)
                if not _device_info(mac)["paired"]:
                    time.sleep(2)
                    _run(["bluetoothctl", "pair", mac], timeout=30)
            except Exception as e:
                logger.warning("BR/EDR pair %s failed: %s", mac, e)
            finally:
                self._le_toggle(True)
        try:
            _run(["bluetoothctl", "trust", mac], timeout=5)
        except Exception:
            pass
        try:
            _run(["bluetoothctl", "connect", mac], timeout=30)
        except Exception as e:
            logger.warning("connect after pair %s failed: %s", mac, e)
        return _device_info(mac)["paired"]

    def connect(self, mac: str) -> bool:
        try:
            _run(["bluetoothctl", "connect", mac.upper()], timeout=30)
        except Exception as e:
            logger.warning("connect %s failed: %s", mac, e)
        # PulseAudio takes a beat to expose the new sink after BlueZ reports connected.
        for _ in range(10):
            if _device_info(mac)["connected"]:
                time.sleep(0.5)
                return True
            time.sleep(0.3)
        return False

    def disconnect(self, mac: str) -> bool:
        try:
            _run(["bluetoothctl", "disconnect", mac.upper()], timeout=10)
        except Exception as e:
            logger.warning("disconnect %s failed: %s", mac, e)
        return not _device_info(mac)["connected"]

    def forget(self, mac: str) -> bool:
        mac = mac.upper()
        try:
            _run(["bluetoothctl", "disconnect", mac], timeout=10)
        except Exception:
            pass
        try:
            r = _run(["bluetoothctl", "remove", mac], timeout=10)
            ok = r.returncode == 0
        except Exception as e:
            logger.warning("remove %s failed: %s", mac, e)
            ok = False
        if self.active_mac == mac:
            self.set_active_mac(None)
        return ok

    # PortAudio (used by sounddevice) only enumerates a single generic `pulse` device
    # for the whole PulseAudio server, not one device per bluez sink.

    def pa_default_sink(self) -> Optional[str]:
        try:
            r = _pactl(["get-default-sink"], timeout=3)
            return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
        except Exception:
            return None

    def pa_default_source(self) -> Optional[str]:
        try:
            r = _pactl(["get-default-source"], timeout=3)
            return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
        except Exception:
            return None

    def set_pa_default_sink(self, sink_name: str) -> bool:
        try:
            r = _pactl(["set-default-sink", sink_name], timeout=3)
            return r.returncode == 0
        except Exception as e:
            logger.warning("set-default-sink %s failed: %s", sink_name, e)
            return False

    def set_pa_default_source(self, source_name: str) -> bool:
        try:
            r = _pactl(["set-default-source", source_name], timeout=3)
            return r.returncode == 0
        except Exception as e:
            logger.warning("set-default-source %s failed: %s", source_name, e)
            return False

    def first_alsa_sink(self) -> Optional[str]:
        """First non-bluez hardware sink — the safe 'built-in speaker' default when the
        captured snapshot is unusable (e.g. hal booted while a BT sink was still
        PulseAudio's default).
        """
        try:
            r = _pactl(["list", "short", "sinks"], timeout=5)
            if r.returncode != 0:
                return None
            for line in r.stdout.splitlines():
                cols = line.split("\t")
                if len(cols) >= 2 and cols[1].startswith("alsa_output."):
                    return cols[1]
        except Exception as e:
            logger.warning("first_alsa_sink failed: %s", e)
        return None

    def set_pa_sink_volume(self, sink_name: str, pct: int) -> bool:
        """Set the sink volume (0-100%). On A2DP sinks PulseAudio forwards it to the
        headset via AVRCP absolute volume, so this is the volume knob while a BT headset
        is the active output.
        """
        pct = max(0, min(100, pct))
        try:
            r = _pactl(["set-sink-volume", sink_name, f"{pct}%"], timeout=5)
            return r.returncode == 0
        except Exception as e:
            logger.warning("set-sink-volume %s %s%% failed: %s", sink_name, pct, e)
            return False

    def pa_sink_volume(self, sink_name: str) -> Optional[int]:
        """Current sink volume in percent, or None if unreadable."""
        try:
            r = _pactl(["get-sink-volume", sink_name], timeout=5)
            if r.returncode != 0:
                return None
            m = re.search(r"(\d+)%", r.stdout)
            return int(m.group(1)) if m else None
        except Exception:
            return None

    def pa_sink_for_mac(self, mac: str) -> Optional[str]:
        """Return the PulseAudio sink name exposing this BT device, or None."""
        try:
            r = _pactl(["list", "short", "sinks"], timeout=5)
            if r.returncode != 0:
                logger.warning("pactl list sinks failed: rc=%s stderr=%s",
                               r.returncode, r.stderr.strip())
                return None
            needle = mac.upper().replace(":", "_")
            for line in r.stdout.splitlines():
                cols = line.split("\t")
                if len(cols) < 2:
                    continue
                name = cols[1]
                if "bluez" in name.lower() and needle in name.upper():
                    return name
        except Exception as e:
            logger.warning("pa_sink_for_mac %s failed: %s", mac, e)
        return None

    def pa_card_for_mac(self, mac: str) -> Optional[str]:
        """Return the PulseAudio card name (`bluez_card.XX_XX...`) for this MAC."""
        try:
            r = _pactl(["list", "short", "cards"], timeout=5)
            if r.returncode != 0:
                return None
            needle = mac.upper().replace(":", "_")
            for line in r.stdout.splitlines():
                cols = line.split("\t")
                if len(cols) < 2:
                    continue
                name = cols[1]
                if name.startswith("bluez_card.") and needle in name.upper():
                    return name
        except Exception as e:
            logger.warning("pa_card_for_mac %s failed: %s", mac, e)
        return None

    def pa_card_profiles(self, card_name: str) -> dict[str, bool]:
        """Return {profile_name: available} for the given card."""
        out: dict[str, bool] = {}
        try:
            r = _pactl(["list", "cards"], timeout=5)
            if r.returncode != 0:
                return out
            in_card = False
            for line in r.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("Name:"):
                    in_card = stripped.split(":", 1)[1].strip() == card_name
                    continue
                if not in_card:
                    continue
                m = re.match(r"^([a-zA-Z0-9_+\-]+):\s.*available:\s*(yes|no)\)", stripped)
                if m:
                    out[m.group(1)] = m.group(2) == "yes"
        except Exception as e:
            logger.warning("pa_card_profiles %s failed: %s", card_name, e)
        return out

    def set_pa_card_profile(self, card_name: str, profile: str) -> bool:
        """Switch the bluez card to a different profile (a2dp_sink ↔ HFP). Switching to HFP
        exposes a real PulseAudio source (the headset mic); A2DP only exposes the sink.
        """
        try:
            r = _pactl(["set-card-profile", card_name, profile], timeout=10)
            return r.returncode == 0
        except Exception as e:
            logger.warning("set-card-profile %s %s failed: %s", card_name, profile, e)
            return False

    def pa_source_for_mac(self, mac: str) -> Optional[str]:
        """Return the PulseAudio source for this BT device (HFP profile only;
        A2DP-only headphones have no real source)."""
        try:
            r = _pactl(["list", "short", "sources"], timeout=5)
            if r.returncode != 0:
                return None
            needle = mac.upper().replace(":", "_")
            for line in r.stdout.splitlines():
                cols = line.split("\t")
                if len(cols) < 2:
                    continue
                name = cols[1]
                if "bluez" in name.lower() and needle in name.upper() and not name.endswith(".monitor"):
                    return name
        except Exception as e:
            logger.warning("pa_source_for_mac %s failed: %s", mac, e)
        return None

    def pulse_sd_index(self, sd_module) -> Optional[int]:
        """Find the PortAudio index of the generic `pulse` device.

        PA outlives hal restarts, so in practice the first enumeration hits and the
        re-init never runs.
        """

        def _find() -> Optional[int]:
            for i, dev in enumerate(sd_module.query_devices()):
                if dev.get("name", "").lower() == "pulse":
                    return i
            return None

        idx = _find()
        if idx is not None:
            return idx

        from hal.drivers import audio_route

        audio_route.pa_reinit_event.set()
        try:
            # Close streams opened since the caller's quiesce (TTS speak can fire from
            # an agent turn at any moment).
            audio_route.quiesce_portaudio_users()
            with audio_route.pa_reinit_lock:
                try:
                    sd_module._terminate()
                    sd_module._initialize()
                except Exception:
                    logger.exception("PortAudio reinit failed during pulse lookup")
        finally:
            audio_route.pa_reinit_event.clear()
        return _find()
