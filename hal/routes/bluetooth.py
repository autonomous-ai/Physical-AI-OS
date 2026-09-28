"""Bluetooth headset routes: pair / connect / route TTS+STT to a BT headset."""

import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import hal.app_state as state
from hal import config
from hal.drivers.audio_route import (
    current_label,
    quiesce_portaudio_users,
    resume_capture,
    route_op_lock,
    route_to_bluetooth_pa,
    route_to_builtin,
)
from hal.drivers.bluetooth_manager import BluetoothManager

logger = logging.getLogger("hal.bluetooth.routes")

router = APIRouter(prefix="/bluetooth", tags=["Bluetooth"])

try:
    import sounddevice as sd
except ImportError:
    sd = None  # type: ignore

_manager: Optional[BluetoothManager] = None


def _mgr() -> BluetoothManager:
    global _manager
    if _manager is None:
        _manager = BluetoothManager()
    return _manager


class MacRequest(BaseModel):
    mac: str


class ActiveRequest(BaseModel):
    mac: Optional[str] = None  # None / "" → switch back to the device


@router.get("/available")
def bt_available():
    """Tell the UI whether bluetoothctl works on this host."""
    return {"available": _mgr().available()}


@router.post("/scan/start")
def bt_scan_start():
    mgr = _mgr()
    if not mgr.available():
        raise HTTPException(503, "bluetoothctl not available")
    mgr.scan_start()
    return {"status": "ok"}


@router.get("/scan/results")
def bt_scan_results():
    mgr = _mgr()
    paired_macs = {d["mac"] for d in mgr.paired_devices()}
    discovered = [d for d in mgr.discovered_devices() if d["mac"] not in paired_macs]
    return {"scanning": mgr.scan_active(), "devices": discovered}


@router.get("/devices")
def bt_devices():
    mgr = _mgr()
    paired = mgr.paired_devices()
    active = mgr.active_mac
    # "active" means audio is routed there now, not just the persisted preference.
    routed = bool(active) and current_label() == f"bt:{active}"
    for d in paired:
        d["active"] = routed and d["mac"] == active
    return {"active_mac": active, "label": current_label(), "devices": paired}


@router.post("/pair")
def bt_pair(req: MacRequest):
    mgr = _mgr()
    if not mgr.available():
        raise HTTPException(503, "bluetoothctl not available")
    if not mgr.pair(req.mac):
        raise HTTPException(
            500,
            "Pairing failed — make sure the headset is in pairing mode and try again",
        )
    return {"status": "ok"}


@router.post("/forget")
def bt_forget(req: MacRequest):
    mgr = _mgr()
    target = req.mac.upper()
    if mgr.active_mac and mgr.active_mac == target:
        # Route back to the device first, or the persistent OutputStream points at a gone sink.
        with route_op_lock:
            route_to_builtin()
    if not mgr.forget(target):
        raise HTTPException(500, "Forget failed")
    return {"status": "ok"}


@router.get("/active")
def bt_active_get():
    mgr = _mgr()
    return {"active_mac": mgr.active_mac, "label": current_label()}


@router.post("/active")
def bt_active_set(req: ActiveRequest):
    """Toggle voice routing: null/"" -> built-in speaker+mic; MAC -> route TTS+STT to the BT device."""
    mgr = _mgr()
    target = (req.mac or "").strip().upper() or None

    # Serializes whole route flows: interleaved PortAudio re-inits abort the process.
    if target is None:
        with route_op_lock:
            route_to_builtin()
            mgr.set_active_mac(None)
        return {"status": "ok", "active_mac": None, "label": current_label()}

    if sd is None:
        raise HTTPException(503, "sounddevice not available on host")

    with route_op_lock:
        if not mgr.info(target)["connected"] and not mgr.connect(target):
            raise HTTPException(503, f"Could not connect to {target}")

        # HFP routes the headset mic too (mono 16 kHz); default A2DP keeps the device mic for STT.
        card = mgr.pa_card_for_mac(target)
        if card:
            profiles = mgr.pa_card_profiles(card)
            profile = None
            if config.BT_PREFER_HFP and profiles.get("handsfree_head_unit", False):
                profile = "handsfree_head_unit"
            elif profiles.get("a2dp_sink", False):
                profile = "a2dp_sink"
            if profile:
                mgr.set_pa_card_profile(card, profile)

        # PA exposes the bluez sink asynchronously; poll up to ~8s.
        pa_sink: Optional[str] = None
        for _ in range(8):
            pa_sink = mgr.pa_sink_for_mac(target)
            if pa_sink:
                break
            time.sleep(1.0)
        if not pa_sink:
            raise HTTPException(
                503,
                f"PulseAudio has no sink for {target} — check pulseaudio-module-bluetooth",
            )
        # PortAudio re-init aborts the process while other streams are live; quiesce first.
        quiesce_portaudio_users()
        pulse_idx = mgr.pulse_sd_index(sd)
        if pulse_idx is None:
            resume_capture()
            raise HTTPException(
                503,
                "PortAudio cannot see PulseAudio — sounddevice/portaudio not built with pulse support",
            )
        pa_source = mgr.pa_source_for_mac(target)

        route_to_bluetooth_pa(pulse_idx, pa_sink, pa_source, target)
        mgr.set_active_mac(target)
    return {
        "status": "ok",
        "active_mac": target,
        "label": current_label(),
        "pa_sink": pa_sink,
        "pa_source": pa_source,
        "pulse_sd_index": pulse_idx,
    }


@router.get("/status")
def bt_status():
    """Snapshot for the monitor card — what's active, what's around."""
    mgr = _mgr()
    return {
        "available": mgr.available(),
        "active_mac": mgr.active_mac,
        "label": current_label(),
        "scanning": mgr.scan_active(),
        "paired": mgr.paired_devices(),
    }
