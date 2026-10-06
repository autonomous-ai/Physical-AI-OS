"""LED route handlers -- all /led/* endpoints."""

import threading
from typing import Optional

from fastapi import APIRouter, Body, HTTPException

import hal.app_state as state
from hal.drivers.motors import hold
from hal.models import (
    LEDColorResponse,
    LEDEffectRequest,
    LEDEffectResponse,
    LEDOffRequest,
    LEDPaintRequest,
    LEDRestingPreviewRequest,
    LEDRestingPreviewResponse,
    LEDRestingRequest,
    LEDRestingResponse,
    LEDSolidRequest,
    LEDStateResponse,
    LEDStatusRequest,
    StatusResponse,
)
from hal.presets import (
    FX_SPEAKING_WAVE,
    LST_EFFECT,
    LST_PAINT,
    LST_SOLID,
    RGB_CMD_PAINT,
    RGB_CMD_SOLID,
    STATUS_LED_PRESETS,
    VALID_LED_EFFECTS,
)
from hal.drivers.rgb.effects import run_effect as _run_effect

router = APIRouter(tags=["LED"])


def _sleep_led_locked(route: str) -> bool:
    """True while the device is asleep; HTTP LED writes are dropped (clearing routes are not gated)."""
    if not state._sleeping:
        return False
    state.logger.info("%s skipped -- sleepy owns the strip", route)
    return True


def _end_scene() -> None:
    """A non-transient LED override ends the scene, its servo hold, and its restore-on-restart."""
    from hal.routes.scene import _persist_scene

    state._active_scene = None
    _persist_scene(None)
    if hold.release(state.animation_service, hold.SCENE):
        still = hold.holder(state.animation_service)
        if still:
            state.logger.info("Scene ended by an LED override: scene hold released, "
                              "servo still held by %s", still)
        else:
            state.logger.info("Scene ended by an LED override: servo released")


@router.get("/led", response_model=LEDStateResponse)
def get_led_state():
    """Get LED strip info."""
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    return {"led_count": state.rgb_service.led_count}


def _read_ring(rgb_service) -> list[tuple[int, int, int]]:
    """Every pixel on the strip as (r, g, b); best-effort, empty on read failure."""
    pixels: list[tuple[int, int, int]] = []
    try:
        for i in range(rgb_service.led_count):
            raw = rgb_service.strip.getPixelColor(i)
            pixels.append(((raw >> 16) & 0xFF, (raw >> 8) & 0xFF, raw & 0xFF))
    except Exception as e:
        state.logger.warning("LED read-back failed at pixel %d: %s", len(pixels), e)
    return pixels


@router.get("/led/color", response_model=LEDColorResponse)
def get_led_color():
    """Get current LED state: actual pixel color read from strip, effect, scene, brightness."""
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    effect_running = (
        state._effect_name is not None
        and state._effect_thread is not None
        and state._effect_thread.is_alive()
    )
    uniform = True
    # `color` keeps reporting the effect base while one runs (os-server's ambient loop keys off it).
    ring = _read_ring(state.rgb_service)
    ring_lit = any(px != (0, 0, 0) for px in ring)
    if effect_running and state._effect_base_color:
        r, g, b = state._effect_base_color
        uniform = all(px == ring[0] for px in ring) if ring else True
    else:
        # The whole ring, not pixel 0: dithered effects are non-uniform by design.
        r, g, b = max(ring, key=lambda px: max(px)) if ring else (0, 0, 0)
        uniform = all(px == ring[0] for px in ring) if ring else True
    brightness = round(max(r, g, b) / 255.0, 3)
    # An effect only counts as "on" if it is actually painting light.
    is_on = (r, g, b) != (0, 0, 0) or ring_lit
    return {
        "led_count": state.rgb_service.led_count,
        "on": is_on,
        "color": [r, g, b],
        "hex": f"#{r:02x}{g:02x}{b:02x}",
        "brightness": brightness,
        "uniform": uniform,
        "effect": state._effect_name,
        "scene": state._active_scene,
    }


@router.post("/led/solid", response_model=StatusResponse)
def set_led_solid(req: LEDSolidRequest):
    """Fill entire LED strip with a single color."""
    return _set_led_solid(req)


def _set_led_solid(req: LEDSolidRequest, *, source: str | None = None):
    """Keep system status provenance separate from explicit user colors."""
    if _sleep_led_locked("led/solid"):
        return {"status": "ok"}
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    color = tuple(req.color) if isinstance(req.color, list) else req.color
    state._stop_current_effect()
    state.rgb_service.dispatch(RGB_CMD_SOLID, color)
    # Transient overlays must not exit the active scene.
    if not req.transient:
        _end_scene()
    if state.sensing_service and isinstance(color, tuple):
        state.sensing_service.presence.set_last_color(color)
    if req.transient:
        state._cancel_pending_restore()
    else:
        # Explicit user look — wins over the mic-muted resting indicator.
        state._dismiss_mic_muted_led("led/solid")
        saved = {"type": LST_SOLID, "color": list(color)}
        if source is not None:
            saved["source"] = source
        state._save_user_led_state(saved)
    return {"status": "ok"}


def _expand_gradient(stops, n: int) -> list:
    """Linearly interpolate color stops across n pixels (CSS-gradient style)."""
    rgb = []
    for c in stops:
        if isinstance(c, int):
            rgb.append(((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF))
        elif isinstance(c, (list, tuple)) and len(c) >= 3:
            rgb.append(tuple(c[:3]))
    if not rgb:
        return []
    if len(rgb) == 1 or n <= 1:
        return [rgb[0]] * max(n, 1)
    out = []
    segs = len(rgb) - 1
    for i in range(n):
        pos = i * segs / (n - 1)
        k = min(int(pos), segs - 1)
        t = pos - k
        a, b = rgb[k], rgb[k + 1]
        out.append(tuple(int(x + (y - x) * t) for x, y in zip(a, b)))
    return out


@router.post("/led/paint", response_model=StatusResponse)
def set_led_paint(req: LEDPaintRequest):
    """Set individual pixel colors (multi-color fills, gradients)."""
    if _sleep_led_locked("led/paint"):
        return {"status": "ok"}
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    if req.gradient:
        colors = _expand_gradient(req.colors, state.rgb_service.led_count)
    else:
        colors = [tuple(c) if isinstance(c, list) else c for c in req.colors]
    # A running effect repaints every ~40ms; stop it first.
    state._stop_current_effect()
    state.rgb_service.dispatch(RGB_CMD_PAINT, colors)
    if not req.transient:
        _end_scene()
    if state.sensing_service:
        avg = state._avg_paint_color(colors)
        if avg:
            state.sensing_service.presence.set_last_color(avg)
    if req.transient:
        state._cancel_pending_restore()
    else:
        state._dismiss_mic_muted_led("led/paint")
        # Persist the expanded pixel list so restore repaints exactly what was shown.
        state._save_user_led_state(
            {
                "type": LST_PAINT,
                "colors": [list(c) if isinstance(c, tuple) else c for c in colors],
            }
        )
    return {"status": "ok"}


@router.post("/led/off", response_model=StatusResponse)
def turn_off_leds(req: Optional[LEDOffRequest] = Body(default=None)):
    """Turn off all LEDs and preserve explicit off across ambient restores."""
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    transient = req.transient if req else False
    # Legacy os-server ends provisioning with /led/off followed by /led/restore.
    # Only the tagged setup cue is discarded; never infer ownership from its color.
    ending_setup = bool(
        not transient and state._user_led_state
        and state._user_led_state.get("source") == "status:setup"
    )
    state._stop_current_effect()
    state.rgb_service.clear()
    if not transient:
        _end_scene()
    if state.sensing_service:
        state.sensing_service.presence.set_last_color((0, 0, 0))
    if transient:
        state._cancel_pending_restore()
    elif ending_setup:
        state._save_user_led_state(None)
        state._restore_user_led()
    else:
        state._dismiss_mic_muted_led("led/off")
        state._save_user_led_state({"type": LST_SOLID, "color": [0, 0, 0]})
    return {"status": "ok"}


@router.post("/led/effect", response_model=LEDEffectResponse)
def start_led_effect(req: LEDEffectRequest):
    """Start a LED effect (breathing, candle, rainbow, notification_flash, pulse)."""
    if _sleep_led_locked(f"LED effect '{req.effect}'"):
        return {"status": "ok", "effect": req.effect, "speed": req.speed}
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    if req.effect not in VALID_LED_EFFECTS:
        raise HTTPException(
            400, f"Unknown effect '{req.effect}'. Available: {VALID_LED_EFFECTS}"
        )

    if state._tts_speaking and req.effect != FX_SPEAKING_WAVE:
        state.logger.info("LED effect '%s' skipped -- TTS speaking_wave active", req.effect)
        return {"status": "ok", "effect": req.effect, "speed": req.speed}

    # Mic-muted red is a privacy indicator; transient overlays must not paint over it.
    if req.transient and state._mic_muted_led_owns_strip():
        state.logger.info(
            "LED effect '%s' (transient) skipped -- mic-muted indicator owns strip",
            req.effect,
        )
        return {"status": "ok", "effect": req.effect, "speed": req.speed}

    from hal.drivers.harness import led as harness_voice_led
    if req.transient and req.effect == "breathing" and req.duration_ms is None and harness_voice_led.enabled():
        return {"status": "ok", "effect": req.effect, "speed": req.speed}

    # No "light is off" guard: transient status cues may light a resting strip.
    state._stop_current_effect()
    if not req.transient:
        _end_scene()

    base_color = tuple(req.color) if req.color else (255, 180, 100)
    # Transient effects overlay on the user's saved color; non-transient replace the strip.
    overlay_base = state._get_user_base_color() if req.transient else (0, 0, 0)

    state._effect_stop.clear()
    state._effect_name = req.effect
    state._effect_base_color = base_color
    state._effect_thread = threading.Thread(
        target=_run_effect,
        args=(
            req.effect,
            base_color,
            req.speed,
            req.duration_ms,
            state._effect_stop,
            state.rgb_service,
        ),
        kwargs={"base_color": overlay_base, "brightness": req.brightness},
        daemon=True,
        name=f"led-effect-{req.effect}",
    )
    state._effect_thread.start()
    state.logger.info(
        "LED effect started: %s (speed=%.1f, duration=%s, transient=%s)",
        req.effect,
        req.speed,
        req.duration_ms,
        req.transient,
    )

    if req.transient:
        state._cancel_pending_restore()
    else:
        state._dismiss_mic_muted_led("led/effect")
        state._save_user_led_state(
            {
                "type": LST_EFFECT,
                "effect": req.effect,
                "color": list(base_color),
                "speed": req.speed,
                "brightness": req.brightness,
            }
        )

    return {"status": "ok", "effect": req.effect, "speed": req.speed}


@router.post("/led/status", response_model=LEDEffectResponse)
def set_led_status(req: LEDStatusRequest):
    """Apply an os-server status state (booting/error/ota/...) by name, transiently via STATUS_LED_PRESETS."""
    preset = STATUS_LED_PRESETS.get(req.state)
    if not preset:
        raise HTTPException(
            400,
            f"Unknown status state '{req.state}'. Available: {sorted(STATUS_LED_PRESETS)}",
        )
    effect, color, speed = preset["effect"], preset["color"], preset.get("speed", 1.0)
    # Solid statuses survive transient overlays, but retain their system provenance.
    if effect == "solid":
        _set_led_solid(LEDSolidRequest(color=color), source=f"status:{req.state}")
    else:
        start_led_effect(LEDEffectRequest(effect=effect, color=color, speed=speed, transient=True))
    return {"status": "ok", "effect": effect, "speed": speed}


@router.post("/led/restore", response_model=StatusResponse)
def restore_led():
    """Restore the strip to the user's saved LED state, else settle on the resting look."""
    if _sleep_led_locked("led/restore"):
        return {"status": "ok"}
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    state._restore_user_led()
    return {"status": "ok"}


@router.get("/led/resting", response_model=LEDRestingResponse)
def get_led_resting():
    """The owner's resting LED choice, the device default and the look in effect."""
    from hal import resting_led

    return resting_led.snapshot()


@router.put("/led/resting", response_model=LEDRestingResponse)
def set_led_resting(req: LEDRestingRequest):
    """Save the owner's resting LED look and show it now when the strip is resting."""
    from hal import resting_led

    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    try:
        snap = resting_led.set_choice(req.mode, req.color)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _cancel_resting_preview()
    # The new resting look replaces an earlier explicit colour or off.
    state._save_user_led_state(None)
    if not _sleep_led_locked("led/resting"):
        state._restore_user_led()
    return snap


# A preview the app never follows with a save falls back to the saved look.
RESTING_PREVIEW_HOLD_S = 10.0
_preview_lock = threading.Lock()
_preview_timer: Optional[threading.Timer] = None


def _cancel_resting_preview() -> None:
    global _preview_timer
    with _preview_lock:
        if _preview_timer is not None:
            _preview_timer.cancel()
            _preview_timer = None


def _end_resting_preview(timer: threading.Timer) -> None:
    global _preview_timer
    with _preview_lock:
        if _preview_timer is not timer:
            return
        _preview_timer = None
    state.logger.info("LED resting preview expired -- restoring saved look")
    state._restore_user_led()


@router.post("/led/resting/preview", response_model=LEDRestingPreviewResponse)
def preview_led_resting(req: LEDRestingPreviewRequest):
    """Paint a candidate resting colour now without saving it; reverts after 10 s of no previews."""
    global _preview_timer
    from hal import resting_led

    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    try:
        color = resting_led.valid_preview_color(req.color)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Never cut into sleep, speech, music or the mic-privacy indicator; the saved look is unchanged either way.
    if (state._sleeping or state._tts_speaking or state._music_playing
            or state._mic_muted_led_owns_strip()):
        return {"status": "ok", "painted": False}
    state._cancel_pending_restore()
    state._stop_current_effect()
    state.rgb_service.dispatch(RGB_CMD_SOLID, color)
    timer = threading.Timer(RESTING_PREVIEW_HOLD_S, lambda: _end_resting_preview(timer))
    timer.daemon = True
    with _preview_lock:
        if _preview_timer is not None:
            _preview_timer.cancel()
        _preview_timer = timer
    timer.start()
    return {"status": "ok", "painted": True}


@router.post("/led/effect/stop", response_model=StatusResponse)
def stop_led_effect():
    """Stop the currently running LED effect."""
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    if state._tts_speaking:
        state.logger.info("LED effect/stop skipped -- TTS speaking_wave active")
        return {"status": "ok"}
    # While the mic-muted indicator owns the strip, any stop here is stale; ignore it.
    if state._mic_muted_led_owns_strip():
        state.logger.info("LED effect/stop skipped -- mic-muted indicator owns strip")
        return {"status": "ok"}
    from hal.drivers.harness.led import owns_effect
    if owns_effect():
        return {"status": "ok"}
    state._stop_current_effect()
    # Stopping a thread leaves its last frame; clear it when the resting state is dark.
    if state.led_should_stay_dark():
        state.rgb_service.dispatch(RGB_CMD_SOLID, (0, 0, 0))
        state.logger.info("LED effect/stop -- resting dark, cleared last frame")
    return {"status": "ok"}
