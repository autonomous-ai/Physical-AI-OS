"""Emotion route handler -- /emotion endpoint."""

import threading

from fastapi import APIRouter

import hal.app_state as state
from hal import config
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

# Emotions allowed through the sleep gate. Not all of them wake: greeting and
# stretching flip `_sleeping` back to False, while sleepy passes so a repeat
# (night scene, presence.away) can re-arm the auto-release timer and re-apply
# the sleepy peripheral state on an already-sleeping device.
#
# The expressive set (happy/excited/caring/laugh/curious/sad/shy/shock/
# confused) used to wake too, on the theory that an emotional reply means the
# agent is responding to a live user. It does not: an emotion name carries no
# evidence of who caused it, so an agent finishing a stale background task
# would light the strip and move the body on a sleeping device. Sleep is a
# do-not-disturb state — the way back out is a physical tap on the button, or
# presence.enter → greeting when the user walks back in.
_SLEEP_GATE_ALLOWED = {
    EMO_GREETING, EMO_STRETCHING, EMO_SLEEPY,
}

# Auto-release the servo shortly after *continuous* sleepy so the animation
# can settle before torque is disabled.
SLEEPY_AUTO_RELEASE_SECONDS = 1.0

# How long a still emotion (preset with servo=None) keeps the body frozen
# before idle breathing resumes. A following emotion clears the halt on its
# own (_handle_play calls _begin_motion), so this only covers the turn that
# never produces one: an LLM error, or silence after the first partial.
# Roughly matches the voice-service listening safety net (8s) with headroom
# for the reply to arrive first.
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
    """Sleep is refused while Harness voice mode is ON; unavailable means off."""
    from hal.drivers.voice._internal.harness_voice import read_voice_mode
    return bool(read_voice_mode().get("enabled"))


@router.post("/emotion", response_model=EmotionResponse)
def express_emotion(req: EmotionRequest, source: str = "api"):
    """Express an emotion by coordinating servo animation + LED color simultaneously.

    `source` only labels the sleep journal -- it says who caused a transition,
    not what happens. FastAPI reads it as an optional query parameter, so every
    existing HTTP caller keeps working and lands under the default; the
    in-process callers (button, touchpad) name themselves.
    """
    emotion = (req.emotion or "").strip().lower()
    preset = EMOTION_PRESETS.get(emotion)
    if not preset:
        # Callers are AI agents that sometimes invent emotion names — a 400
        # wastes their turn and nothing shows on the device. Fall back to
        # curious (a neutral, always-safe expression) instead of rejecting.
        # While sleeping, ignore instead. `curious` no longer wakes, so the
        # fallback would not lift the sleep gate — but it would still resolve
        # to a servo/LED-bearing emotion that the gate below then drops, and
        # logging it as "curious" hides which invented name the agent sent.
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

    # Harness ON means the user is at their desk working; the absence timer
    # and any API caller must not put the device to sleep under them.
    if req.emotion == EMO_SLEEPY and harness_blocks_sleep():
        state.logger.info("POST /emotion: ignored sleepy while Harness is on (source=%s)", source)
        return {"status": "ignored", "emotion": req.emotion, "servo": None, "led": None}

    # The gaze+VAD acknowledgement is LED-only and must yield silently to the
    # real emotion that now owns the device (normally ``listening`` on first
    # STT partial). Do not restore between the two effects or the user sees a
    # black/resting LED flash.
    state.clear_listening_pending_cue(restore=False)

    was_sleeping = state._sleeping
    state._sleeping = req.emotion == EMO_SLEEPY
    if state._sleeping != was_sleeping:
        # Survive a HAL restart (OTA, deploy, crash): without this the device
        # wakes up on its own the next time the service restarts.
        state._persist_sleep_state()
        # Two writes, two questions. The sidecar above is overwritten and dies
        # with the boot, so it can only say whether the device is asleep NOW;
        # the journal keeps every transition so it can also say how often and
        # when. Both sit here because this is where all four routes into and
        # out of sleep meet -- marker, button, presence.enter, web UI.
        state._log_sleep_transition(
            "sleep" if state._sleeping else "wake", req.emotion, source
        )
    state._current_emotion = req.emotion
    # Any other emotion supersedes the realtime thinking cue — drop its claim
    # so an LED restore never repaints thinking over what was just expressed.
    if req.emotion != EMO_THINKING:
        state._thinking_cue_active = False
    if was_sleeping and not state._sleeping:
        state._wake_sleepy_peripherals()
        # Every wake restarts the presence countdown, not only a face on camera.
        state.note_presence_wake()

    # Any emotion cancels a pending still-emotion idle resume: either it plays
    # a recording (which clears the halt itself) or it is another still
    # emotion that re-arms the timer below.
    if state._still_idle_timer is not None:
        state._still_idle_timer.cancel()
        state._still_idle_timer = None

    # Stuck-thinking net: `thinking` is set at the start of a wait and is only
    # ever cleared by the emotion the reply expresses. A turn that dies before
    # producing one (realtime exception, delegate that never answers, forward
    # that never happens) leaves the face — and, via _thinking_cue_active, every
    # later LED restore — on thinking with no user input to break it out. Fall
    # back to idle after a continuous hold. Any other emotion cancels; a fresh
    # thinking re-arms, so a slow-but-alive turn is unaffected as long as it
    # keeps the same face for less than the window.
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
                # Drop the cue's claim first: idle is a background emotion, so
                # without this the restore path would repaint thinking again.
                state._thinking_cue_active = False
                express_emotion(EmotionRequest(emotion=EMO_IDLE))
                # idle is a background emotion, so the call above may leave the
                # forced thinking pulse running — settle the strip back on the
                # user's state explicitly (restore is TTS/music-guarded).
                from hal.routes.led import restore_led

                restore_led()
            except Exception as e:
                state.logger.warning("Thinking reset failed: %s", e)

        state._thinking_reset_timer = threading.Timer(
            config.EMOTION_THINKING_RESET_S, _reset_after_stuck_thinking
        )
        state._thinking_reset_timer.daemon = True
        state._thinking_reset_timer.start()

    # Sleepy auto-release: fires only if sleepy stays continuous for the
    # full window. Any other emotion (including a wake) cancels the timer.
    if state._sleepy_release_timer is not None:
        state._sleepy_release_timer.cancel()
        state._sleepy_release_timer = None
    if req.emotion == EMO_SLEEPY:
        def _auto_release_after_sleepy():
            # Re-check inside the timer callback in case the state changed
            # between the cancel-window and the timer firing.
            if state._current_emotion != EMO_SLEEPY:
                return
            try:
                from hal.routes.servo import release_servos

                # release() ramps to a gravity-rest pose before torque-off.
                # Serialize it with wake/resume so a wake cannot re-enable
                # torque halfway through the release sequence.
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

    # Auto-off scene when waking from sleep (e.g. Night mode → restore peripherals)
    if was_sleeping and not state._sleeping and state._active_scene:
        from hal.routes.scene import deactivate_scene
        state.logger.info("POST /emotion: waking from sleep, auto scene off (%s)", state._active_scene)
        deactivate_scene()

    # Two levels of suppression:
    #   - hold_mode: /servo/hold or focus/reading scene. Suppress most
    #     emotion servo animations. Scene-preset holds let scene-change
    #     emotions through (greeting/sleepy/stretching may legitimately
    #     transition scene) — but an EXPLICIT /servo/hold (_hold_explicit,
    #     agent command like "face the wall and stay there") blocks those
    #     too: a trailing [HW:/emotion:greeting] in the same reply used to
    #     ride the exemption and park the arm at the greeting pose instead
    #     of the commanded one.
    #   - tracking_active: vision tracker owns the servo. Suppress ALL
    #     emotion servo including scene-change — otherwise a loud-noise
    #     shock reaction would yank the device off the tracked object.
    # LED display updates in both cases so the user still gets visual
    # feedback.
    svc = state.animation_service
    tracking_active = svc and getattr(svc, "_tracking_active", False)
    servo_held = svc and getattr(svc, "_hold_mode", False)
    hold_explicit = svc and getattr(svc, "_hold_explicit", False)
    scene_change = req.emotion in {EMO_GREETING, EMO_SLEEPY, EMO_STRETCHING}
    servo_blocked = tracking_active or (servo_held and (hold_explicit or not scene_change))

    servo_played = None

    if svc and preset.get("servo") and not servo_blocked:
        try:
            # Sleepy auto-release stops the event loop and disables torque.
            # Restart the loop here so the wake animation actually plays —
            # mirrors the same restart in /servo/play. Goes through the
            # MotionService contract: the feetech backend restarts its event
            # thread, backends whose control loop lives elsewhere (Reachy's
            # daemon) no-op. Reaching for _running/_event_thread directly threw
            # AttributeError on any non-feetech body.
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
        # Still emotion (listening, thinking): the point is a body that does
        # not move, and leaving the servo alone does NOT achieve that. The
        # idle recording loops forever once it settles (animation_service
        # _continue_playback), and idle is not a subtle breath — it swings
        # wrist_roll ~32 deg and base_pitch ~17 deg per cycle. Worse, an
        # emotion that just finished interpolates BACK to idle over several
        # seconds, so the biggest movement of all lands exactly while the
        # user is talking. halt() drops whatever is playing and pins the
        # current pose with torque on; the next play clears it via
        # _begin_motion, so nothing has to un-halt explicitly.
        # Music is exempt: the groove is the point of that moment, and a
        # listening cue must not stop the dance.
        if getattr(svc, "_music_playing", False):
            state.logger.info("POST /emotion: still emotion (%s) -- music playing, body keeps moving", req.emotion)
        else:
            try:
                svc.halt()
                state.logger.info("POST /emotion: still emotion (%s) -- body halted, idle in %.1fs",
                                  req.emotion, STILL_IDLE_RESUME_SECONDS)

                def _resume_idle_after_still(held=req.emotion):
                    # Only resume if the device is still in the same still
                    # emotion: any newer emotion already owns the body.
                    if state._current_emotion != held:
                        return
                    try:
                        svc.ensure_running()
                        svc.dispatch(SERVO_CMD_PLAY, SERVO_IDLE)
                        state.logger.info("Still emotion %s held >= %.1fs -- idle resumed",
                                          held, STILL_IDLE_RESUME_SECONDS)
                    except Exception as e:
                        state.logger.warning("Still-emotion idle resume failed: %s", e)

                state._still_idle_timer = threading.Timer(
                    STILL_IDLE_RESUME_SECONDS, _resume_idle_after_still
                )
                state._still_idle_timer.daemon = True
                state._still_idle_timer.start()
            except Exception as e:
                state.logger.warning("Still-emotion halt failed: %s", e)

    # LED behavior:
    #   - tracking_active: LED still updates so the user sees emotion
    #     feedback (eyes/color) even though the servo is locked.
    #   - hold_mode (non-scene-change): LED suppressed — /servo/hold asks
    #     for a fully "held" device, including ambient light.
    #   - otherwise: LED updates normally.
    led_allowed = tracking_active or not servo_blocked
    led_color = state._apply_emotion_led_display(req.emotion, req.intensity) if led_allowed else None

    if req.emotion == EMO_IDLE:
        pass
    elif req.emotion == EMO_SLEEPY:
        pass
    elif req.emotion == EMO_LISTENING:
        # Hold blue-pulse for the whole STT session — next emotion (LLM
        # response or voice_service idle-reset safety net) overwrites it.
        pass
    elif req.emotion == EMO_SHOCK:
        state._schedule_led_restore(2.0)
        state.logger.info("Emotion: shock -- LED restore scheduled in 2.0s")
    else:
        servo_name = preset.get("servo", "")
        restore_delay = state._get_recording_duration(servo_name) + 0.5 if servo_name else 3.5
        state.logger.info("Emotion: %s -- LED restore scheduled in %.1fs (servo=%s)", req.emotion, restore_delay, servo_name)
        state._schedule_led_restore(restore_delay)

    cam = preset.get("camera")
    if cam == LST_OFF:
        state._auto_camera_off(f"emotion:{req.emotion}")
    elif cam == "on" and state._camera_disabled:
        state._auto_camera_on(f"emotion:{req.emotion}")

    if req.emotion == EMO_SLEEPY:
        state._finalize_sleepy_peripherals(
            mute_mic=preset.get("mic") == "off",
            mute_speaker=preset.get("speaker") == "off",
        )

    return {
        "status": "ok",
        "emotion": req.emotion,
        "servo": servo_played,
        "led": led_color,
    }
