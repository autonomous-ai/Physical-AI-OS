"""Scene route handlers -- /scene endpoints."""

import json
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException

import hal.app_state as state
from hal import config, privacy
from hal.models import (
    SceneListResponse,
    SceneRequest,
    SceneResponse,
    ServoAimRequest,
    StatusResponse,
)
from hal.presets import LST_OFF, LST_SCENE, RGB_CMD_SOLID, SCENE_PRESETS

router = APIRouter(tags=["Scene"])

# Persisted active scene — survives HAL *service* restarts so the agent's
# belief ("focus mode is on") stays true instead of silently desyncing from a
# scene-less HAL. Deliberately boot-scoped, twice over: the file lives on
# tmpfs AND carries the kernel boot_id — a full device reboot starts scene-less
# by design (restoring a days-old focus scene after a power cycle would be
# wrong), only an in-boot restart (OTA, deploy, crash) restores.
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
    """Re-activate the scene that was live before a service restart.

    Called once from server lifespan (background thread) after the hardware
    services it touches (LED, servo, voice) are up. Stale files (other boot,
    unknown scene) are removed, not applied.
    """
    try:
        if not _SCENE_STATE_PATH.exists():
            return
        data = json.loads(_SCENE_STATE_PATH.read_text())
        scene = data.get("scene")
        if not scene or data.get("boot_id") != _boot_id() or scene not in SCENE_PRESETS:
            _SCENE_STATE_PATH.unlink(missing_ok=True)
            return
        if state._sleeping:
            # Sleep owns the hardware across a restart. Keep scene identity so
            # the normal wake path can clear it, without repainting LEDs,
            # moving servos, or reopening the camera/mic/speaker. The saved
            # user LED state is already loaded by app_state at import time.
            state._active_scene = scene
            state.logger.info("Scene restore: retained '%s' while asleep (hardware unchanged)", scene)
            return
        activate_scene(SceneRequest(scene=scene))
        state.logger.info("Scene restore: re-activated '%s' after service restart", scene)
    except Exception as e:
        state.logger.warning("scene restore failed: %s", e)


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
    if state.sensing_service:
        state.sensing_service.presence.set_last_color(tuple(scaled))
    state._save_user_led_state({"type": LST_SCENE, "scene": req.scene})

    aim_dir = preset.get("aim")
    servo_mode = preset.get("servo")
    if aim_dir and state.animation_service:
        from hal.routes.servo import aim_servo

        # Release hold before aiming so the move isn't blocked
        if state.animation_service._hold_mode:
            state.animation_service._hold_mode = False
            state.animation_service._hold_explicit = False

        def _aim_then_hold():
            aim_servo(ServoAimRequest(direction=aim_dir))
            if servo_mode == "hold":
                state.animation_service._hold_mode = True
                state.logger.info("Scene %s: servo hold (after aim)", req.scene)

        threading.Thread(target=_aim_then_hold, daemon=True, name=f"scene-aim-{aim_dir}").start()
    elif servo_mode == "hold" and state.animation_service:
        state.animation_service._hold_mode = True
        state.logger.info("Scene %s: servo hold", req.scene)
    elif servo_mode != "hold" and state.animation_service and state.animation_service._hold_mode:
        state.animation_service._hold_mode = False
        state.animation_service._hold_explicit = False
        state.logger.info("Scene %s: servo released", req.scene)

    cam = preset.get("camera")
    if cam == LST_OFF:
        state._auto_camera_off(f"scene:{req.scene}")
    elif cam == "on":
        state._auto_camera_on(f"scene:{req.scene}")

    # Mic control
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
        # Mic is live again — drop a lingering privacy indicator flag (the
        # scene paint already owns the strip look).
        state._clear_mic_muted_led()
        state._persist_mic_state()
        state.logger.info("Scene %s: mic unmuted", req.scene)

    # Speaker control
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
    """Deactivate current scene and return to idle state.

    Reverses ALL peripheral changes made by scene activation:
    servo hold, camera, mic, speaker, LED.
    """
    prev = state._active_scene
    state._active_scene = None
    _persist_scene(None)
    state._save_user_led_state(None)
    state._cancel_scene_speaker_drain()

    # Release servo hold
    if state.animation_service and state.animation_service._hold_mode:
        state.animation_service._hold_mode = False
        state.animation_service._hold_explicit = False
        state.logger.info("Scene off: servo released")

    # Under a privacy lock the overlay would restore the scene's mute as the
    # user's preference; retarget its snapshot instead (as sleepy wake does).
    with privacy.lock:
        # Re-enable camera
        if state._camera_disabled:
            if privacy.camera_muted:
                if not state._camera_manual_override:
                    privacy.camera_before = False
                    state._persist_camera_state()
                    state.logger.info("Scene off: camera reopens when privacy releases")
            else:
                state._auto_camera_on("scene:off")

        # Unmute mic + restart voice pipeline
        if state._mic_muted and not privacy.mic_locked():
            state._mic_muted = False
            state._mic_manual_override = False
            state.start_voice_service("scene:off")
            state._clear_mic_muted_led()
            state._persist_mic_state()
            state.logger.info("Scene off: mic unmuted")

        # Unmute speaker
        if state._speaker_muted:
            if privacy.speaker_muted:
                privacy.speaker_before = False
                state._persist_speaker_state()
                state.logger.info("Scene off: speaker unmutes when privacy releases")
            else:
                state._speaker_muted = False
                state._persist_speaker_state()
                state.logger.info("Scene off: speaker unmuted")

    # Settle the strip the same way every other release path does. This used to
    # paint the `idle` preset color unconditionally, from back when the resting
    # look was a warm white — with AMBIENT_RESTING_LED black (default off, see
    # hal/presets.py), that left scene-off glowing dim orange until some
    # unrelated restore later cleared it, instead of turning the light off.
    # restore_led() reads the resting look, and honors mic-muted / TTS / music
    # ownership, which the raw dispatch did not.
    if state.rgb_service:
        from hal.routes.led import restore_led

        restore_led()

    state.logger.info("Scene off: deactivated %s, LED settled", prev)
    return {"status": "ok"}
