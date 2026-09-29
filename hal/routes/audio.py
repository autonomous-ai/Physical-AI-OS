"""Audio route handlers -- /audio devices, /audio/volume, /audio/play-tone, /audio/record."""

import io
import os
import re
import subprocess
import wave
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

import hal.app_state as state
from hal import privacy
from hal.config import AUDIO_OUTPUT_ALSA, VOLUME_STATE_PATH
from hal.drivers.voice._internal import live_playback
from hal.safety.policy import clamp_volume, max_volume_pct
from hal.models import (
    AudioDevicesResponse,
    StatusResponse,
    VolumeRequest,
    VolumeResponse,
    VolumeSetResponse,
)

router = APIRouter(tags=["Audio"])

sd = None
np = None
try:
    import numpy as np
    import sounddevice as sd
except ImportError:
    pass


def _amixer_ctl_device() -> Optional[str]:
    """Derive the amixer control (-D) device from HAL_AUDIO_OUTPUT_ALSA; None = default card.

    Example: 'plug:device_speaker' -> 'device_speaker'
    """
    val = AUDIO_OUTPUT_ALSA
    if not val:
        return None
    rest = val.split(":", 1)[1] if ":" in val else val
    rest = rest.strip()
    if not rest:
        return None
    if "," in rest:  # hw-style "card,device" -> control is the card
        rest = rest.split(",", 1)[0]
    if rest.isdigit():  # bare card index -> hw:N
        return f"hw:{rest}"
    return rest  # named alias (ctl.<alias>) or card id


def _detect_playback_controls() -> tuple[list[str], Optional[str]]:
    """Return (playback_control_names, amixer_ctl_device), selected by ALSA capability, not name."""
    dev = _amixer_ctl_device()
    cmd = ["amixer", "-D", dev, "scontents"] if dev else ["amixer", "scontents"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return [], dev
        playback: list[str] = []
        current: Optional[str] = None
        for line in result.stdout.splitlines():
            m = re.search(r"Simple mixer control '([^']+)',([0-9]+)", line)
            if m:
                # ALSA omits index 0 in shorthand; keep legacy labels for it.
                current = m.group(1) if m.group(2) == "0" else f"{m.group(1)},{m.group(2)}"
                continue
            if current and "Capabilities:" in line:
                caps = line.split("Capabilities:", 1)[1].split()
                # Playback-only: a control that also has cvolume (e.g. Lamp 'Mic') must never receive speaker volume.
                if "pvolume" in caps and "cvolume" not in caps:
                    playback.append(current)
                current = None
        return playback, dev
    except Exception:
        pass
    return [], dev


@router.get("/audio", response_model=AudioDevicesResponse)
def get_audio_info():
    """Get audio device availability."""
    return {
        "output_device": state.audio_output_device,
        "input_device": state.audio_input_device,
        "available": state.audio_output_device is not None or state.audio_input_device is not None,
    }


# DAC-only controls take dB; map 0-100% onto this envelope. +2dB ceiling avoids speaker overdrive.
_DAC_MAX_DB = 2.0
_DAC_MIN_DB = -60.0
_DAC_CONTROLS = {"DACL", "DACR", "DAC"}


def _pct_to_db(pct: int) -> float:
    pct = max(0, min(100, pct))
    return _DAC_MIN_DB + (pct / 100.0) * (_DAC_MAX_DB - _DAC_MIN_DB)


def _db_to_pct(db: float) -> int:
    span = _DAC_MAX_DB - _DAC_MIN_DB
    pct = round((db - _DAC_MIN_DB) / span * 100.0)
    return max(0, min(100, pct))


def _bt_sink() -> Optional[str]:
    """The bluez sink while a BT headset route is active, else None (volume then goes via PulseAudio)."""
    try:
        from hal.drivers import audio_route
        if not audio_route.bt_active():
            return None
        from hal.drivers.bluetooth_manager import BluetoothManager
        mgr = BluetoothManager()
        mac = mgr.active_mac
        return mgr.pa_sink_for_mac(mac) if mac else None
    except Exception:
        return None


def _persist_volume(pct: int) -> None:
    """Persist the last-set volume for os-server boot restore (best-effort)."""
    try:
        os.makedirs(os.path.dirname(VOLUME_STATE_PATH), exist_ok=True)
        with open(VOLUME_STATE_PATH, "w") as f:
            f.write(str(pct))
    except Exception:
        pass


@router.post("/audio/volume", response_model=VolumeSetResponse)
def set_volume(req: VolumeRequest):
    """Set speaker volume (0-100%), clamped to SAFETY.md `audio.max_volume` for every sink and caller."""
    pct = clamp_volume(state.safety_policy, req.volume)
    if pct != req.volume:
        state.logger.info(
            "POST /audio/volume: %d%% clamped to safety ceiling %d%%", req.volume, pct
        )
    if state.simulation_audio:
        state.simulation_volume = pct
        _persist_volume(pct)
        return _vol_set_response(pct)
    sink = _bt_sink()
    if sink:
        from hal.drivers.bluetooth_manager import BluetoothManager
        if not BluetoothManager().set_pa_sink_volume(sink, pct):
            raise HTTPException(503, "Bluetooth sink volume change failed")
        _persist_volume(pct)
        return _vol_set_response(pct)
    controls, dev = _detect_playback_controls()
    if not controls:
        raise HTTPException(503, "No audio mixer controls found")
    cmd_prefix = ["amixer", "-D", dev] if dev else ["amixer"]
    dac_db = _pct_to_db(pct)
    for ctrl in controls:
        value = f"{dac_db:.1f}dB" if ctrl.rsplit(",", 1)[0].upper() in _DAC_CONTROLS else f"{pct}%"
        try:
            # `--` so amixer doesn't parse a leading `-` in negative dB as a flag.
            result = subprocess.run(
                [*cmd_prefix, "sset", ctrl, "--", value],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            live_playback.observe_mixer(ctrl, result.stdout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(503, f"Audio mixer write failed: {ctrl}") from exc
    _persist_volume(pct)
    return _vol_set_response(pct)


def _vol_set_response(volume: int) -> dict:
    """POST reply: the volume actually applied plus the ceiling."""
    return {
        "status": "ok",
        "volume": volume,
        "max_volume": max_volume_pct(state.safety_policy),
    }


def _vol_response(control: str, volume: int) -> dict:
    """Volume read response; always carries the safety ceiling."""
    return {
        "control": control,
        "volume": volume,
        "max_volume": max_volume_pct(state.safety_policy),
    }


@router.get("/audio/volume", response_model=VolumeResponse)
def get_volume():
    """Get current speaker volume from amixer (DAC dB controls first, else raw %)."""
    if state.simulation_audio:
        return _vol_response("virtual", state.simulation_volume)
    sink = _bt_sink()
    if sink:
        from hal.drivers.bluetooth_manager import BluetoothManager
        vol = BluetoothManager().pa_sink_volume(sink)
        if vol is not None:
            return _vol_response("bluetooth", vol)
    controls, dev = _detect_playback_controls()
    cmd_prefix = ["amixer", "-D", dev] if dev else ["amixer"]
    sorted_controls = sorted(
        controls, key=lambda c: 0 if c.rsplit(",", 1)[0].upper() in _DAC_CONTROLS else 1
    )
    for ctrl in sorted_controls:
        try:
            result = subprocess.run(
                [*cmd_prefix, "sget", ctrl],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                continue
            live_playback.observe_mixer(ctrl, result.stdout)
            if ctrl.rsplit(",", 1)[0].upper() in _DAC_CONTROLS:
                db_match = re.search(r"\[(-?\d+(?:\.\d+)?)dB\]", result.stdout)
                if db_match:
                    return _vol_response(ctrl, _db_to_pct(float(db_match.group(1))))
            pct_match = re.search(r"\[(\d+)%\]", result.stdout)
            if pct_match:
                return _vol_response(ctrl, int(pct_match.group(1)))
        except Exception:
            continue
    raise HTTPException(503, "Audio volume control not available")


@router.post("/audio/play-tone", response_model=StatusResponse)
def play_tone(
    frequency: Annotated[int, Query(ge=20, le=20_000)] = 440,
    duration_ms: Annotated[int, Query(ge=1, le=5_000)] = 500,
):
    """Play a bounded test tone while speaker privacy policy allows it."""
    if state._speaker_muted or privacy.speaker_muted:
        raise HTTPException(409, "Speaker is muted")
    if state.simulation_audio:
        return {"status": "ok"}
    if not sd or not np:
        raise HTTPException(503, "Audio not available")
    if state.audio_output_device is None:
        raise HTTPException(503, "No output audio device found")
    # Release the TTS stream so sd.play gets the ALSA device; TTS reopens lazily.
    if state.tts_service and hasattr(state.tts_service, "release_stream"):
        state.tts_service.release_stream()
    dev_info = sd.query_devices(state.audio_output_device)
    sample_rate = int(dev_info["default_samplerate"])
    t = np.linspace(
        0, duration_ms / 1000, int(sample_rate * duration_ms / 1000), endpoint=False
    )
    tone = 0.5 * np.sin(2 * np.pi * frequency * t).astype(np.float32)
    sd.play(tone, samplerate=sample_rate, device=state.audio_output_device)
    return {"status": "ok"}


@router.post("/audio/record")
def record_audio(duration_ms: Annotated[int, Query(ge=1, le=30_000)] = 3000):
    """Record bounded audio from an unmuted microphone. Returns WAV bytes."""
    if state._mic_muted or privacy.mic_locked():
        raise HTTPException(409, "Microphone is muted")
    if state.simulation_audio:
        sample_rate = 16_000
        frames = int(sample_rate * duration_ms / 1000)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(b"\x00\x00" * frames)
        return Response(content=buf.getvalue(), media_type="audio/wav")
    if not sd or not np:
        raise HTTPException(503, "Audio not available")
    if state.audio_input_device is None:
        raise HTTPException(503, "No input audio device found")
    dev_info = sd.query_devices(state.audio_input_device)
    sample_rate = int(dev_info["default_samplerate"])
    channels = 1
    frames = int(sample_rate * duration_ms / 1000)
    recording = sd.rec(
        frames,
        samplerate=sample_rate,
        channels=channels,
        dtype="int16",
        device=state.audio_input_device,
    )
    sd.wait()
    if state._mic_muted or privacy.mic_locked():
        raise HTTPException(409, "Microphone was muted during recording")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(recording.tobytes())
    buf.seek(0)
    return Response(content=buf.read(), media_type="audio/wav")
