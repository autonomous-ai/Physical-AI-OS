"""Scene route handlers -- /scene endpoints."""

import json
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

import hal.app_state as state
from hal import config, privacy
from hal.drivers.motors import hold
from hal.models import (
    SceneListResponse,
    SceneRequest,
    SceneResponse,
    ServoAimRequest,
    StatusResponse,
)
from hal.presets import LST_OFF, LST_SCENE, RGB_CMD_SOLID, SCENE_PRESETS

router = APIRouter(tags=["Scene"])

# Boot-scoped (tmpfs + boot_id): restored on service restart, cleared on reboot.
_SCENE_STATE_PATH = Path(config.STATE_DIR) / "hal-scene-state.json"


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except Exception:
        return ""


def _persist_scene(scene: str | None) -> None:
    try:
        if scene is None:
            _SCENE_STATE_PATH.unlink(missing_ok=True)
        else:
            _SCENE_STATE_PATH.write_text(json.dumps({"scene": scene, "boot_id": _boot_id()}))
    except Exception as e:
        state.logger.warning("scene persist failed: %s", e)


def restore_persisted_scene() -> None:
    """Re-activate the scene that was live before a service restart (stale files are removed)."""
    try:
        if not _SCENE_STATE_PATH.exists():
            return
        data = json.loads(_SCENE_STATE_PATH.read_text())
        scene = data.get("scene")
        if not scene or data.get("boot_id") != _boot_id() or scene not in SCENE_PRESETS:
            _SCENE_STATE_PATH.unlink(missing_ok=True)
            return
        if state._sleeping:
            # Sleep owns the hardware across a restart: keep scene identity without touching peripherals.
            state._active_scene = scene
            state.logger.info("Scene restore: retained '%s' while asleep (hardware unchanged)", scene)
            return
        activate_scene(SceneRequest(scene=scene))
        state.logger.info("Scene restore: re-activated '%s' after service restart", scene)
    except Exception as e:
        state.logger.warning("scene restore failed: %s", e)


def camera_held_off_by_scene() -> Optional[str]:
    """The active scene's name when that scene keeps the camera off, else None."""
    name = state._active_scene
    preset = SCENE_PRESETS.get(name) if name else None
    return name if preset and preset.get("camera") == LST_OFF else None


@router.get("/scene", response_model=SceneListResponse)
def list_scenes():
    """List all available lighting scene presets."""
    return {"scenes": list(SCENE_PRESETS.keys()), "active": state._active_scene}


@router.post("/scene", response_model=SceneResponse)
def activate_scene(req: SceneRequest):
    """Activate a lighting scene preset."""
    preset = SCENE_PRESETS.get(req.scene)
    if not preset:
        available = list(SCENE_PRESETS.keys())
        raise HTTPException(400, f"Unknown scene '{req.scene}'. Available: {available}")

    if not state.rgb_service:
        raise HTTPException(503, "LED not available")

    state._stop_current_effect()
    base = preset["color"]
    brightness = preset["brightness"]
    scaled = [int(c * brightness) for c in base]
    try:
        state.rgb_service.dispatch(RGB_CMD_SOLID, tuple(scaled))
    except Exception as e:
        raise HTTPException(500, f"Failed to set scene: {e}")

    state._active_scene = req.scene
    _persist_scene(req.scene)
    state._save_user_led_state({"type": LST_SCENE, "scene": req.scene})

    aim_dir = preset.get("aim")
    servo_mode = preset.get("servo")
    svc = state.animation_service
    # Only this scene's own hold is ours to drop; tracking and explicit holds stay.
    if svc and hold.release(svc, hold.SCENE) and servo_mode != "hold":
        state.logger.info("Scene %s: servo released", req.scene)
    if aim_dir and svc:
        from hal.routes.servo import aim_servo

        def _aim_then_hold():
            aim_servo(ServoAimRequest(direction=aim_dir))
            # The scene may have ended while the arm was moving.
            if servo_mode == "hold" and state._active_scene == req.scene:
                hold.claim(svc, hold.SCENE)
                state.logger.info("Scene %s: servo hold (after aim)", req.scene)

        threading.Thread(target=_aim_then_hold, daemon=True, name=f"scene-aim-{aim_dir}").start()
    elif servo_mode == "hold" and svc:
        hold.claim(svc, hold.SCENE)
        state.logger.info("Scene %s: servo hold", req.scene)

    cam = preset.get("camera")
    if cam == LST_OFF:
        state._auto_camera_off(f"scene:{req.scene}")
    elif cam == "on":
        state._auto_camera_on(f"scene:{req.scene}")

    mic = preset.get("mic")
    if mic == "off" and not state._mic_muted:
        state._mic_muted = True
        if state.voice_service and state.voice_service.available:
            state.voice_service.stop()
        state._persist_mic_state()
        state.logger.info("Scene %s: mic muted", req.scene)
    elif mic == "on" and state._mic_muted and not privacy.mic_locked():
        state._mic_muted = False
        state._mic_manual_override = False
        state.start_voice_service("scene:mic-on")
        state._clear_mic_muted_led()
        state._persist_mic_state()
        state.logger.info("Scene %s: mic unmuted", req.scene)

    spk = preset.get("speaker")
    if spk == "off" and not state._speaker_muted:
        # Stop music now; speech drains so the scene's own line still plays.
        if state.music_service and state.music_service.playing:
            state.music_service.stop()
        state._start_scene_speaker_drain(req.scene)
    elif spk == "on":
        state._cancel_scene_speaker_drain()
        if state._speaker_muted and not privacy.speaker_muted:
            state._speaker_muted = False
            state._persist_speaker_state()
            state.logger.info("Scene %s: speaker unmuted", req.scene)

    return {
        "status": "ok",
        "scene": req.scene,
        "brightness": brightness,
        "color": scaled,
        "aim": aim_dir,
    }


@router.post("/scene/off", response_model=StatusResponse)
def deactivate_scene():
    """Deactivate the current scene, reversing all peripheral changes (servo, camera, mic, speaker, LED)."""
    prev = state._active_scene
    state._active_scene = None
    _persist_scene(None)
    state._save_user_led_state(None)
    state._cancel_scene_speaker_drain()

    if hold.release(state.animation_service, hold.SCENE):
        still = hold.holder(state.animation_service)
        if still:
            state.logger.info("Scene off: scene hold released, servo still held by %s", still)
        else:
            state.logger.info("Scene off: servo released")

    # Under a privacy lock, retarget the overlay snapshot instead of restoring the scene's mute.
    with privacy.lock:
        if state._camera_disabled:
            if privacy.camera_muted:
                if not state._camera_manual_override:
                    privacy.camera_before = False
                    state._persist_camera_state()
                    state.logger.info("Scene off: camera reopens when privacy releases")
            else:
                state._auto_camera_on("scene:off")

        if state._mic_muted and not privacy.mic_locked():
            state._mic_muted = False
            state._mic_manual_override = False
            state.start_voice_service("scene:off")
            state._clear_mic_muted_led()
            state._persist_mic_state()
            state.logger.info("Scene off: mic unmuted")

        if state._speaker_muted:
            if privacy.speaker_muted:
                privacy.speaker_before = False
                state._persist_speaker_state()
                state.logger.info("Scene off: speaker unmutes when privacy releases")
            else:
                state._speaker_muted = False
                state._persist_speaker_state()
                state.logger.info("Scene off: speaker unmuted")

    # restore_led() honors the resting look and mic-muted / TTS / music ownership.
    if state.rgb_service:
        from hal.routes.led import restore_led

        restore_led()

    state.logger.info("Scene off: deactivated %s, LED settled", prev)
    return {"status": "ok"}
