"""Emotion route handler -- /emotion endpoint."""

import threading

from fastapi import APIRouter

import hal.app_state as state
from hal import config
from hal.drivers.motors import hold
from hal.models import EmotionRequest, EmotionResponse
from hal.presets import (
    EMOTION_PRESETS,
    EMO_CURIOUS,
    EMO_GREETING,
    EMO_IDLE,
    EMO_LISTENING,
    EMO_SHOCK,
    EMO_SLEEPY,
    EMO_STRETCHING,
    EMO_THINKING,
    LST_OFF,
    SERVO_CMD_PLAY,
    SERVO_IDLE,
)

# Emotions allowed through the sleep gate; only greeting/stretching wake, sleepy re-arms release.
_SLEEP_GATE_ALLOWED = {
    EMO_GREETING, EMO_STRETCHING, EMO_SLEEPY,
}

# Let the animation settle before torque is disabled.
SLEEPY_AUTO_RELEASE_SECONDS = 1.0

# Still-emotion halt timeout if no follow-up emotion arrives (voice net is 8s + headroom).
STILL_IDLE_RESUME_SECONDS = 10.0

router = APIRouter(tags=["Emotion"])


@router.get("/emotion/status")
def emotion_status():
    """Return current emotion state."""
    return {
        "current_emotion": state._current_emotion,
        "sleeping": state._sleeping,
        "active_scene": state._active_scene,
    }


@router.get("/emotion/presets")
def list_emotion_presets():
    """Return all available emotion presets with their LED color and effect."""
    result = {}
    for name, preset in EMOTION_PRESETS.items():
        result[name] = {
            "color": preset.get("color"),
            "effect": preset.get("effect"),
            "speed": preset.get("speed"),
        }
    return result


def harness_blocks_sleep() -> bool:
    """Allow sleep only when OS confirms Harness voice mode is OFF."""
    from hal.drivers.voice._internal.harness_voice import read_voice_mode
    snapshot = read_voice_mode()
    return bool(snapshot.get("enabled") or snapshot.get("unavailable"))


def _resume_idle_after_still(svc, held: str) -> None:
    """Idle resume for a still emotion nothing replaced. A held body stays put; the
    resume is parked and replayed when the look hold releases."""
    # Any newer emotion already owns the body.
    if state._current_emotion != held:
        return
    owner = hold.holder(svc)
    if owner:
        state._still_idle_deferred = held
        state.logger.info("Still emotion %s: idle resume deferred -- servo held by %s", held, owner)
        return
    try:
        svc.ensure_running()
        svc.dispatch(SERVO_CMD_PLAY, SERVO_IDLE)
        state.logger.info("Still emotion %s held >= %.1fs -- idle resumed",
                          held, STILL_IDLE_RESUME_SECONDS)
    except Exception as e:
        state.logger.warning("Still-emotion idle resume failed: %s", e)


def resume_deferred_still_idle(svc) -> bool:
    """Replay a parked still-emotion idle resume once nothing holds the body. True if it ran."""
    held = state._still_idle_deferred
    if held is None:
        return False
    state._still_idle_deferred = None
    # A user's explicit hold keeps the arm; /servo/resume plays idle when it ends.
    if state._current_emotion != held or hold.holder(svc):
        return False
    _resume_idle_after_still(svc, held)
    return True


@router.post("/emotion", response_model=EmotionResponse)
def express_emotion(req: EmotionRequest, source: str = "api"):
    """Express an emotion by coordinating servo animation + LED color.

    `source` only labels the sleep journal (optional query parameter).
    """
    emotion = (req.emotion or "").strip().lower()
    preset = EMOTION_PRESETS.get(emotion)
    if not preset:
        # Invented names fall back to `curious` instead of a 400; while sleeping they are ignored.
        if state._sleeping:
            state.logger.info(
                "POST /emotion: ignored unknown '%s' while sleeping", req.emotion
            )
            return {"status": "ignored", "emotion": req.emotion, "servo": None, "led": None}
        state.logger.warning(
            "POST /emotion: unknown '%s' — falling back to %s", req.emotion, EMO_CURIOUS
        )
        emotion = EMO_CURIOUS
        preset = EMOTION_PRESETS[EMO_CURIOUS]
    req.emotion = emotion

    state.logger.info("POST /emotion: emotion=%s intensity=%s user_state=%s sleeping=%s",
                       req.emotion, req.intensity,
                       state._user_led_state.get("type") if state._user_led_state else None,
                       state._sleeping)

    if state._sleeping and req.emotion not in _SLEEP_GATE_ALLOWED:
        state.logger.info("POST /emotion: ignored %s while sleeping", req.emotion)
        return {"status": "ignored", "emotion": req.emotion, "servo": None, "led": None}

    # Harness ON: nothing may put the device to sleep under the user.
    if req.emotion == EMO_SLEEPY and harness_blocks_sleep():
        state.logger.info("POST /emotion: ignored sleepy while Harness is on or unavailable (source=%s)", source)
        return {"status": "ignored", "emotion": req.emotion, "servo": None, "led": None}

    # No restore between the two effects, or the user sees a black LED flash.
    state.clear_listening_pending_cue(restore=False)

    was_sleeping = state._sleeping
    state._sleeping = req.emotion == EMO_SLEEPY
    if state._sleeping != was_sleeping:
        # Survive a HAL restart; otherwise the device wakes on its own.
        state._persist_sleep_state()
        state._log_sleep_transition(
            "sleep" if state._sleeping else "wake", req.emotion, source
        )
    emotion_generation = state._begin_emotion(req.emotion)
    # Close audio admission before servo/camera work can delay sleep.
    if req.emotion == EMO_SLEEPY:
        state._finalize_sleepy_peripherals(
            mute_mic=preset.get("mic") == "off",
            mute_speaker=preset.get("speaker") == "off",
        )
    # Drop the thinking cue's claim so a restore never repaints thinking.
    if req.emotion != EMO_THINKING:
        state._thinking_cue_active = False
    if was_sleeping and not state._sleeping:
        state._wake_sleepy_peripherals()
        # Every wake restarts the presence countdown, not only a face on camera.
        state.note_presence_wake()

    state.cancel_still_idle_timer()

    # Stuck-thinking net: fall back to idle after a continuous hold.
    if state._thinking_reset_timer is not None:
        state._thinking_reset_timer.cancel()
        state._thinking_reset_timer = None
    if req.emotion == EMO_THINKING and config.EMOTION_THINKING_RESET_S > 0:
        def _reset_after_stuck_thinking():
            if state._current_emotion != EMO_THINKING:
                return
            state.logger.warning(
                "Thinking held >= %.1fs with no follow-up emotion -- reverting to idle",
                config.EMOTION_THINKING_RESET_S,
            )
            try:
                # Drop the cue's claim first, or the restore path repaints thinking.
                state._thinking_cue_active = False
                express_emotion(EmotionRequest(emotion=EMO_IDLE))
                from hal.routes.led import restore_led

                restore_led()
            except Exception as e:
                state.logger.warning("Thinking reset failed: %s", e)

        state._thinking_reset_timer = threading.Timer(
            config.EMOTION_THINKING_RESET_S, _reset_after_stuck_thinking
        )
        state._thinking_reset_timer.daemon = True
        state._thinking_reset_timer.start()

    # Fires only if sleepy stays continuous; any other emotion cancels it.
    if state._sleepy_release_timer is not None:
        state._sleepy_release_timer.cancel()
        state._sleepy_release_timer = None
    if req.emotion == EMO_SLEEPY:
        def _auto_release_after_sleepy():
            # Re-check: state may have changed before the timer fired.
            if state._current_emotion != EMO_SLEEPY:
                return
            try:
                from hal.routes.servo import release_servos

                # Serialize with wake/resume so a wake cannot re-enable torque mid-release.
                with state._sleep_servo_lock:
                    state._sleep_servo_released = True
                    state.logger.info(
                        "Auto-release: sleepy held >= %.1fs, releasing servo",
                        SLEEPY_AUTO_RELEASE_SECONDS,
                    )
                    release_servos()
            except Exception as e:
                state.logger.warning(f"Sleepy auto-release failed: {e}")

        state._sleepy_release_timer = threading.Timer(
            SLEEPY_AUTO_RELEASE_SECONDS, _auto_release_after_sleepy
        )
        state._sleepy_release_timer.daemon = True
        state._sleepy_release_timer.start()

    if was_sleeping and not state._sleeping and state._active_scene:
        from hal.routes.scene import deactivate_scene
        state.logger.info("POST /emotion: waking from sleep, auto scene off (%s)", state._active_scene)
        deactivate_scene()

    # hold_mode suppresses most emotion servo (explicit holds block scene-change ones too);
    # tracking_active suppresses all emotion servo. The LED still updates.
    svc = state.animation_service
    tracking_active = svc and getattr(svc, "_tracking_active", False)
    servo_held = svc and getattr(svc, "_hold_mode", False)
    hold_explicit = svc and getattr(svc, "_hold_explicit", False)
    scene_change = req.emotion in {EMO_GREETING, EMO_SLEEPY, EMO_STRETCHING}
    servo_blocked = tracking_active or (servo_held and (hold_explicit or not scene_change))

    servo_played = None

    if svc and preset.get("servo") and not servo_blocked:
        try:
            # Sleepy auto-release stopped the loop; restart via the MotionService contract so the wake plays.
            if was_sleeping and state._sleep_servo_released:
                with state._sleep_servo_lock:
                    svc.resume()
                    state._sleep_servo_released = False
            else:
                svc.ensure_running()
            svc.dispatch(SERVO_CMD_PLAY, preset["servo"])
            servo_played = preset["servo"]
        except Exception as e:
            state.logger.warning(f"Emotion servo failed: {e}")
    elif servo_blocked:
        reason = "tracking active" if tracking_active else "hold mode"
        state.logger.info("POST /emotion: servo suppressed (%s) -- %s", req.emotion, reason)
    elif svc and preset.get("servo") is None:
        # Still emotion: halt() pins the pose (idle would keep swinging); music is exempt.
        if getattr(svc, "_music_playing", False):
            state.logger.info("POST /emotion: still emotion (%s) -- music playing, body keeps moving", req.emotion)
        else:
            try:
                svc.halt()
                state.logger.info("POST /emotion: still emotion (%s) -- body halted, idle in %.1fs",
                                  req.emotion, STILL_IDLE_RESUME_SECONDS)

                state._still_idle_timer = threading.Timer(
                    STILL_IDLE_RESUME_SECONDS, _resume_idle_after_still, args=(svc, req.emotion)
                )
                state._still_idle_timer.daemon = True
                state._still_idle_timer.start()
            except Exception as e:
                state.logger.warning("Still-emotion halt failed: %s", e)

    # LED updates unless hold_mode (non-scene-change) blocks the servo.
    led_allowed = tracking_active or not servo_blocked
    led_color = state._apply_emotion_led_display(req.emotion, req.intensity) if led_allowed else None

    if req.emotion == EMO_IDLE:
        pass
    elif req.emotion == EMO_SLEEPY:
        pass
    elif req.emotion == EMO_LISTENING:
        pass
    elif req.emotion == EMO_SHOCK:
        state._schedule_led_restore(2.0)
        state._schedule_emotion_idle(2.0, emotion_generation)
        state.logger.info("Emotion: shock -- LED restore scheduled in 2.0s")
    else:
        servo_name = preset.get("servo", "")
        restore_delay = state._get_recording_duration(servo_name) + 0.5 if servo_name else 3.5
        state.logger.info("Emotion: %s -- LED restore scheduled in %.1fs (servo=%s)", req.emotion, restore_delay, servo_name)
        state._schedule_led_restore(restore_delay)
        if req.emotion != EMO_THINKING:
            state._schedule_emotion_idle(restore_delay, emotion_generation)

    cam = preset.get("camera")
    if cam == LST_OFF:
        state._auto_camera_off(f"emotion:{req.emotion}")
    elif cam == "on" and state._camera_disabled:
        from hal.routes.scene import camera_held_off_by_scene

        held_by = camera_held_off_by_scene()
        if held_by:
            state.logger.info("Emotion %s: camera stays off -- scene %s keeps it off",
                              req.emotion, held_by)
        else:
            state._auto_camera_on(f"emotion:{req.emotion}")

    return {
        "status": "ok",
        "emotion": req.emotion,
        "servo": servo_played,
        "led": led_color,
    }
