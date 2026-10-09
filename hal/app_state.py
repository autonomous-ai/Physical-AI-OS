"""Shared mutable state for the HAL server (services, flags, cross-route helpers; avoids import cycles)."""

import csv
import logging
import os
import threading
import time
from typing import Optional

from hal import config, privacy
from hal.presets import (
    AMBIENT_RESTING_LED,
    ambient_resting_is_dark,
    EMO_IDLE,
    EMO_LISTENING,
    EMO_MUSIC_STRONG,
    EMO_SLEEPY,
    EMO_THINKING,
    EMOTION_PRESETS,
    FX_SPEAKING_WAVE,
    FX_SPEAKING_WAVE_RAINBOW,
    LED_BACKEND_ERROR_FLASH,
    LST_EFFECT,
    LST_OFF,
    LST_PAINT,
    LST_SCENE,
    LST_SOLID,
    RGB_CMD_PAINT,
    RGB_CMD_SOLID,
    SCENE_PRESETS,
    STATUS_LED_PRESETS,
)

# Background emotions skip the LED so the user's color stays visible.
_BACKGROUND_EMOTIONS = {EMO_IDLE}
from hal.drivers.rgb.effects import run_effect as _run_effect

logger = logging.getLogger("hal.server")


animation_service = None
rgb_service = None
camera_capture = None
sensing_service = None
environment_service = None
voice_service = None
display_service = None
tts_service = None
music_service = None
tracker_service = None
# Logging-only PolicyService until a body supplies a safety-gated executor.
policy_service = None

# Resolved SAFETY.md bounds, or None when the device declares none.
safety_policy = None

# Thermal fail-safe state (hysteresis), surfaced at GET /health.
thermal_over = False
soc_temp_c = None


audio_output_device: Optional[int] = None
audio_input_device: Optional[int] = None
# Simulation exposes an in-memory microphone/speaker pair.
simulation_audio: bool = config.SIMULATE and config.SIM_MEDIA != "host"
simulation_volume: int = 65

# Requested vs actual per-subsystem sim media; reasons surface at GET /simulator/state.
sim_media_requested: str = config.SIM_MEDIA
sim_media_camera: str = sim_media_requested
sim_media_audio: str = sim_media_requested
sim_media_reasons: dict = {}


def sim_media_fallback(kind: str, reason: str) -> None:
    """Downgrade one simulated media subsystem ("camera" or "audio") to virtual; first reason wins."""
    global sim_media_camera, sim_media_audio, simulation_audio
    if kind == "camera":
        sim_media_camera = "virtual"
    elif kind == "audio":
        sim_media_audio = "virtual"
        simulation_audio = True
    else:
        raise ValueError(f"unknown media kind: {kind}")
    sim_media_reasons.setdefault(kind, reason)
    logger.warning("[sim-media] %s falling back to virtual: %s", kind, reason)


_camera_disabled = False
_camera_manual_override = False

# Last realtime `look` frame, handed to a delegate/fallback turn by path; consumed once per turn.
realtime_look_frame_path: Optional[str] = None
# Servable copy of the same frame for the turn thumbnail.
realtime_look_monitor_path: Optional[str] = None
realtime_look_frame_ts: float = 0.0

# Voice identity as a presence signal: last confident speaker-ID match, aging out.
# `_voice_user` is the normalized label; `_voice_user_display` the display spelling.
_voice_user: Optional[str] = None
_voice_user_display: Optional[str] = None
_voice_user_ts: float = 0.0
_voice_user_lock = threading.RLock()


def set_voice_user(label: str, display: Optional[str] = None) -> None:
    """Record the speaker for the finished turn (confident matches only; normalized `label`)."""
    global _voice_user, _voice_user_display, _voice_user_ts
    if not label:
        return
    with _voice_user_lock:
        _voice_user = label
        _voice_user_display = display or label
        _voice_user_ts = time.time()


def clear_voice_user() -> None:
    """Forget the current voice user (the voice twin of /face/cooldowns/reset)."""
    global _voice_user, _voice_user_display, _voice_user_ts
    with _voice_user_lock:
        _voice_user = None
        _voice_user_display = None
        _voice_user_ts = 0.0


def voice_user() -> tuple[str, str, float]:
    """Return (label, display, age_s) for the current voice user; ("", "", 0.0) when nobody."""
    with _voice_user_lock:
        if not _voice_user:
            return "", "", 0.0
        age = time.time() - _voice_user_ts
        if age > config.VOICE_USER_FORGET_S:
            return "", "", 0.0
        return _voice_user, _voice_user_display or _voice_user, age


def face_user() -> tuple[str, float]:
    """Return (label, age_s) for the face-derived user; ("", 0.0) for nobody.

    Recomputes from the live people map (same accessor as `/face/current-user`).
    """
    try:
        if not sensing_service:
            return "", 0.0
        fr = sensing_service._perception_orchestrator._processors.face_recognizer
        if fr is None:
            return "", 0.0
        # getattr: an older perception.py (partial file sync) has only current_user().
        with_age = getattr(fr, "current_user_with_age", None)
        if with_age is None:
            return fr.current_user() or "", 0.0
        label, age = with_age()
        return label or "", age
    except Exception:
        logger.exception("[identity] face current_user lookup failed")
    return "", 0.0


def resolve_current_user() -> tuple[str, str, str, float]:
    """Resolve who the device is with right now -> (label, display, source, age_s).

    Face always wins; voice only fills an empty slot. The single definition of the rule.
    """
    face, face_age = face_user()
    if face:
        return face, face, "face", face_age
    label, display, age = voice_user()
    if label:
        return label, display, "voice", age
    return "", "", "", 0.0


_effect_thread: Optional[threading.Thread] = None
_effect_stop: threading.Event = threading.Event()
_effect_name: Optional[str] = None
_effect_base_color: Optional[tuple] = None
_active_scene: Optional[str] = None


_user_led_state: Optional[dict] = None
_restore_timer: Optional[threading.Timer] = None
_sleeping: bool = False
_current_emotion: Optional[str] = None
_emotion_state_lock = threading.Lock()
_emotion_generation = 0
_emotion_idle_timer: Optional[threading.Timer] = None


def _begin_emotion(emotion):
    """Give each accepted expression its own status lifetime, separate from LEDs."""
    global _current_emotion, _emotion_generation, _emotion_idle_timer
    with _emotion_state_lock:
        _emotion_generation += 1
        if _emotion_idle_timer is not None:
            _emotion_idle_timer.cancel()
            _emotion_idle_timer = None
        _current_emotion = emotion
        return _emotion_generation


def _schedule_emotion_idle(delay_s, generation):
    """Expire transient status without moving servos or interrupting TTS LEDs."""
    global _emotion_idle_timer

    def finish():
        global _current_emotion, _emotion_idle_timer
        with _emotion_state_lock:
            if generation != _emotion_generation or _sleeping:
                return
            previous = _current_emotion
            _current_emotion = EMO_IDLE
            _emotion_idle_timer = None
            logger.info("Emotion status: %s -> idle (expression elapsed)", previous)

    with _emotion_state_lock:
        if generation != _emotion_generation:
            return
        timer = threading.Timer(delay_s, finish)
        timer.daemon = True
        _emotion_idle_timer = timer
        timer.start()


# While set, restore repaints the thinking cue instead of the user state.
_thinking_cue_active: bool = False
# Cancelled the moment the emotion changes away from sleepy.
_sleepy_release_timer: Optional[threading.Timer] = None
# Scene confirmations may finish before muting; sleep mutes immediately.
SLEEPY_SPEAKER_GRACE_S = 2.0       # wait this long for an announcement to START
SLEEPY_SPEAKER_DRAIN_MAX_S = 15.0  # hard cap on the whole drain
_SLEEPY_DRAIN_POLL_S = 0.1
# Same drain for scenes with speaker "off".
_scene_drain_cancel: Optional[threading.Event] = None
# Resume idle after a still emotion halted the loop.
_still_idle_timer: Optional[threading.Timer] = None
# Last-resort net: idle after `thinking` is held too long.
_thinking_reset_timer: Optional[threading.Timer] = None
# LED-only cue bridging VAD confirmation and the first STT partial (1.5-2.5s).
_LISTENING_PENDING_CUE_TIMEOUT_S = 3.0
_listening_pending_cue_id = 0
_listening_pending_cue_active_id: Optional[int] = None
_listening_pending_cue_lock = threading.Lock()
# Servo routes honor this lock until a wake emotion resumes motion.
_sleep_servo_released = False
_sleep_servo_lock = threading.RLock()
# Only mutes owned by sleepy; a wake must never undo a manual user mute.
_sleepy_auto_muted_mic = False
_sleepy_auto_muted_speaker = False


_tts_speaking: bool = False


_music_playing: bool = False


_mic_muted = False
_mic_manual_override = False
_speaker_muted = False

# Hardware mic switch position; None on devices without it. True is authoritative (unmute -> 409).
_hw_mic_switch_muted: "bool | None" = None

# Mic-muted resting LED indicator; explicit user LED commands dismiss it.
_mic_muted_led = False

# True only while enrollment records; its speaker mute is not a user preference.
_enrolling = False

# Monotonic time of the last cancel click; /audio/play refuses requests within the guard.
_music_cancel_ms: float = 0.0

# Covers the cancelled turn's in-flight tool call, below any genuinely new request (~3s).
MUSIC_CANCEL_GUARD_S = 3.0


def note_music_cancel() -> None:
    """Stamp the music cancel watermark. Called by the single-click gesture."""
    global _music_cancel_ms
    _music_cancel_ms = time.monotonic()


def music_cancel_active() -> bool:
    """True while /audio/play must be refused because of a recent cancel."""
    if not _music_cancel_ms:
        return False
    return (time.monotonic() - _music_cancel_ms) < MUSIC_CANCEL_GUARD_S


# Set by destructive button actions so shutdown doesn't announce twice.
_shutdown_announced = False


# Follows AGENT_GATEWAY so the agent's image tool can read saved frames.
from hal import config as _hal_config

_SNAPSHOT_DIR = _hal_config.SNAPSHOT_DIR
_SNAPSHOT_MAX = 20
_snapshot_paths: list = []


DEFAULT_USER = config.DEFAULT_USER


_DEFAULT_AGENT_NAME = "friend"  # last-resort only; device_type is preferred (see _read_agent_name)


def _stop_current_effect():
    """Signal the running effect thread to stop and wait for it."""
    global _effect_thread, _effect_name, _effect_base_color
    if _effect_thread and _effect_thread.is_alive():
        _effect_stop.set()
        _effect_thread.join(timeout=2.0)
    _effect_thread = None
    _effect_name = None
    _effect_base_color = None


def _cancel_pending_restore():
    """Cancel any pending emotion restore timer."""
    global _restore_timer
    if _restore_timer is not None and _restore_timer.is_alive():
        _restore_timer.cancel()
        _restore_timer = None


# Boot-scoped sidecars (restored on service restart, cleared on reboot).
def _state_path(name: str) -> str:
    return os.path.join(config.STATE_DIR, name)


_LED_STATE_PATH = _state_path("hal-led-state.json")


def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except Exception:
        return ""


def _load_user_led_state() -> Optional[dict]:
    import json
    try:
        with open(_LED_STATE_PATH) as f:
            data = json.load(f)
        if data.get("boot_id") != _boot_id():
            os.unlink(_LED_STATE_PATH)
            return None
        saved = data.get("state")
        # Legacy {"type": "off"} sidecar means no state.
        if saved and saved.get("type") == LST_OFF:
            logger.info("User LED state sidecar held legacy 'off' -- treating as no state")
            return None
        if saved:
            logger.info("User LED state restored from sidecar: %s", saved)
        return saved or None
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("User LED state load failed: %s", e)
        return None


# Restore across service restarts (boot-scoped; a device reboot starts fresh).
_user_led_state = _load_user_led_state()


# One boot-scoped sidecar per user-facing switch. Not persisted: enrollment's transient mute.
_MIC_STATE_PATH = _state_path("hal-mic-state.json")
_SPEAKER_STATE_PATH = _state_path("hal-speaker-state.json")
_CAMERA_STATE_PATH = _state_path("hal-camera-state.json")
# Sleep survives a HAL restart (e.g. an OTA must not wake the device at night).
_SLEEP_STATE_PATH = _state_path("hal-sleep-state.json")


def _save_boot_sidecar(path: str, payload: dict):
    import json

    try:
        with open(path, "w") as f:
            json.dump({"boot_id": _boot_id(), **payload}, f)
    except Exception as e:
        logger.warning("Sidecar save failed (%s): %s", path, e)


def _load_boot_sidecar(path: str) -> Optional[dict]:
    import json

    try:
        with open(path) as f:
            data = json.load(f)
        if data.get("boot_id") != _boot_id():
            os.unlink(path)
            return None
        return data
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("Sidecar load failed (%s): %s", path, e)
        return None


def _persist_mic_state():
    _save_boot_sidecar(
        _MIC_STATE_PATH,
        {"muted": _mic_muted, "manual_override": _mic_manual_override},
    )


def _persist_speaker_state():
    _save_boot_sidecar(_SPEAKER_STATE_PATH, {
        "muted": privacy.speaker_before if privacy.speaker_muted else _speaker_muted,
    })


def _persist_sleep_state():
    """Persist sleep AND the mutes sleep owns (kept out of the mic/speaker sidecars)."""
    _save_boot_sidecar(
        _SLEEP_STATE_PATH,
        {
            "sleeping": _sleeping,
            "auto_muted_mic": _sleepy_auto_muted_mic,
            "auto_muted_speaker": _sleepy_auto_muted_speaker,
        },
    )


def _prune_sleep_log():
    """Drop journal days past retention; swallows its own failures."""
    try:
        cutoff = time.time() - config.SLEEP_LOG_MAX_DAYS * 86400
        for name in os.listdir(config.SLEEP_LOG_DIR):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(config.SLEEP_LOG_DIR, name)
            try:
                if os.stat(path).st_mtime < cutoff:
                    os.unlink(path)
            except OSError:
                continue
    except Exception as e:
        logger.warning("Sleep journal prune failed: %s", e)


def _log_sleep_transition(event: str, emotion: str, source: str):
    """Append one sleep/wake transition to today's journal (never raises)."""
    import json

    from hal.clock import device_fromtimestamp, device_timezone

    try:
        os.makedirs(config.SLEEP_LOG_DIR, exist_ok=True)
        ts = time.time()
        # Device-local time (current /etc/timezone); `ts` stays the ordering key.
        when = device_fromtimestamp(ts)
        zone = device_timezone()
        entry = {
            "ts": round(ts, 2),
            "local": when.isoformat(timespec="seconds"),
            "tz": zone.key if zone else "",
            "date": when.strftime("%Y-%m-%d"),
            "hour": when.hour,
            "event": event,
            "emotion": emotion,
            "source": source,
        }
        path = os.path.join(config.SLEEP_LOG_DIR, f"{entry['date']}.jsonl")
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("Sleep journal: %s (emotion=%s source=%s)", event, emotion, source)
        _prune_sleep_log()
    except Exception as e:
        logger.warning("Sleep journal write failed (%s): %s", event, e)


def start_voice_service(reason: str) -> bool:
    """Start the voice pipeline unless a live enrollment owns the mic (ALSA capture is exclusive).

    Returns True when the pipeline was actually started.
    """
    if voice_service is None or privacy.mic_locked():
        return False
    if _enrolling:
        logger.info("voice_service.start skipped (%s) -- enrollment owns the mic", reason)
        return False
    voice_service.start()
    return True


def _finalize_sleepy_peripherals(mute_mic: bool, mute_speaker: bool):
    """Enter silent sleep immediately without changing manual user mutes."""
    global _sleepy_auto_muted_mic, _sleepy_auto_muted_speaker
    global _mic_muted, _speaker_muted
    if _current_emotion != EMO_SLEEPY:
        return

    if mute_speaker:
        _cancel_scene_speaker_drain()
        _mute_speaker_for_sleep()

    _cancel_pending_restore()
    if rgb_service:
        _stop_current_effect()
        rgb_service.clear()

    if mute_mic and not _mic_muted:
        _mic_muted = True
        _sleepy_auto_muted_mic = True
        if voice_service and voice_service.available:
            voice_service.stop(background=True)

    if mute_speaker:
        # Music must stop as well as queued and active speech.
        if music_service and music_service.playing:
            music_service.stop()

    _persist_sleep_state()
    logger.info("Sleepy finalized: LED off, mic muted, speaker muted")


def _run_speaker_drain(cancel: threading.Event, commit, name: str) -> None:
    """Let the current turn's announcement play, then call `commit` (`cancel` abandons it)."""

    def _drain():
        deadline = time.monotonic() + SLEEPY_SPEAKER_DRAIN_MAX_S
        grace_end = time.monotonic() + SLEEPY_SPEAKER_GRACE_S
        started = False
        while not cancel.is_set() and time.monotonic() < deadline:
            if tts_service and tts_service.speaking:
                started = True
            elif started or time.monotonic() >= grace_end:
                break  # the announcement finished, or none ever came
            cancel.wait(_SLEEPY_DRAIN_POLL_S)
        if not cancel.is_set():
            commit()

    threading.Thread(target=_drain, daemon=True, name=name).start()


def _start_scene_speaker_drain(scene: str):
    """Mute the speaker for `scene` once its confirmation line has played (skipped while sleep owns it)."""
    global _scene_drain_cancel
    _cancel_scene_speaker_drain()
    if _sleeping:
        logger.info("Scene %s: speaker mute left to sleep", scene)
        return
    cancel = threading.Event()
    _scene_drain_cancel = cancel
    _run_speaker_drain(cancel, lambda: _mute_speaker_for_scene(scene), "scene-speaker-drain")
    logger.info("Scene %s: speaker draining", scene)


def _cancel_scene_speaker_drain():
    """Stop a pending scene drain. Safe to call when none is running."""
    global _scene_drain_cancel
    if _scene_drain_cancel is not None:
        _scene_drain_cancel.set()
        _scene_drain_cancel = None


def _mute_speaker_for_scene(scene: str):
    """Commit a scene's deferred mute; re-checked under privacy.lock like sleep."""
    global _speaker_muted
    with privacy.lock:
        if _active_scene != scene:
            logger.info("Scene %s speaker drain: scene gone -- speaker left live", scene)
            return
        if _sleeping:
            logger.info("Scene %s speaker drain: asleep -- mute left to sleep", scene)
            return
        if _speaker_muted:
            return
        _speaker_muted = True
        _persist_speaker_state()
    # The flag only gates playback that has not started; stop a TTS the cap cut.
    if tts_service and tts_service.speaking:
        tts_service.stop()
    logger.info("Scene %s: speaker muted", scene)


def _mute_speaker_for_sleep():
    """Mute immediately, preserving ownership so wake never undoes a manual mute."""
    global _speaker_muted, _sleepy_auto_muted_speaker
    with privacy.lock:
        if not _sleeping or _current_emotion != EMO_SLEEPY:
            logger.info("Sleepy speaker mute: awake again -- speaker left live")
            return
        if not _speaker_muted:
            _speaker_muted = True
            _sleepy_auto_muted_speaker = True
            _persist_sleep_state()

    # Also invalidate synthesis and clear queued speech while playback is idle.
    # Outside the lock: stop() can block on the audio device.
    if tts_service:
        tts_service.stop()
    logger.info("Sleepy speaker muted immediately")


def _wake_sleepy_peripherals():
    """Restore only mic/speaker states that sleepy itself muted."""
    global _sleepy_auto_muted_mic, _sleepy_auto_muted_speaker, _mic_muted, _speaker_muted
    # Restore under the same lock used by the sleep mute.
    with privacy.lock:
        if _sleepy_auto_muted_speaker:
            if privacy.speaker_muted:
                # Wake removes sleep's temporary mute under the lock so privacy can't restore it.
                privacy.speaker_before = False
            else:
                _speaker_muted = False
            _sleepy_auto_muted_speaker = False
            _persist_speaker_state()
    if _sleepy_auto_muted_mic:
        _sleepy_auto_muted_mic = False
        if _hw_mic_switch_muted is not True:
            _mic_muted = False
            _clear_mic_muted_led()
            start_voice_service("sleepy-wake")
    _persist_sleep_state()
    logger.info("Sleepy wake: restored sleepy-owned audio state")


def _persist_camera_state():
    _save_boot_sidecar(
        _CAMERA_STATE_PATH,
        {"disabled": privacy.camera_before if privacy.camera_muted else _camera_disabled,
         "manual_override": _camera_manual_override},
    )


def _load_peripheral_sidecars():
    """Restore the peripheral switches at import (applied where each peripheral boots)."""
    global _mic_muted, _mic_manual_override, _speaker_muted
    global _camera_disabled, _camera_manual_override, _mic_muted_led
    global _sleeping, _sleepy_auto_muted_mic, _sleepy_auto_muted_speaker
    if d := _load_boot_sidecar(_MIC_STATE_PATH):
        _mic_muted = bool(d.get("muted"))
        _mic_manual_override = bool(d.get("manual_override"))
        # Painted at the end of lifespan startup once the RGB service is up.
        _mic_muted_led = _mic_muted
    if d := _load_boot_sidecar(_SPEAKER_STATE_PATH):
        _speaker_muted = bool(d.get("muted"))
    if d := _load_boot_sidecar(_CAMERA_STATE_PATH):
        _camera_disabled = bool(d.get("disabled"))
        _camera_manual_override = bool(d.get("manual_override"))
    if d := _load_boot_sidecar(_SLEEP_STATE_PATH):
        # Mutes come back still sleep-owned; nothing is applied to hardware here.
        _sleeping = bool(d.get("sleeping"))
        if _sleeping and d.get("auto_muted_mic"):
            _mic_muted = True
            _sleepy_auto_muted_mic = True
        if _sleeping and d.get("auto_muted_speaker"):
            _speaker_muted = True
            _sleepy_auto_muted_speaker = True
    if _mic_muted or _speaker_muted or _camera_disabled or _sleeping:
        logger.info(
            "Peripheral switches restored: mic_muted=%s speaker_muted=%s camera_disabled=%s sleeping=%s",
            _mic_muted,
            _speaker_muted,
            _camera_disabled,
            _sleeping,
        )


_load_peripheral_sidecars()


def _save_user_led_state(state: dict):
    """Save the user-set LED state and cancel any pending emotion restore."""
    global _user_led_state
    import json
    logger.info("User LED state saved: %s", state)
    _user_led_state = state
    _cancel_pending_restore()
    try:
        if state is None:
            try:
                os.unlink(_LED_STATE_PATH)
            except FileNotFoundError:
                pass
        else:
            with open(_LED_STATE_PATH, "w") as f:
                json.dump({"boot_id": _boot_id(), "state": state}, f)
    except Exception as e:
        logger.warning("User LED state save failed: %s", e)


def _get_recording_duration(recording_name: str) -> float:
    """Return the playback duration (seconds) of a servo recording CSV."""
    recordings_dir = os.path.join(os.path.dirname(__file__), "recordings")
    path = os.path.join(recordings_dir, f"{recording_name}.csv")
    try:
        with open(path, newline="") as f:
            reader = csv.reader(f)
            next(reader)  # skip header
            rows = list(reader)
        if len(rows) < 2:
            return 3.0
        t0 = float(rows[0][0])
        t1 = float(rows[-1][0])
        return max(0.5, t1 - t0)
    except Exception:
        return 3.0


def _is_nonblack(color) -> bool:
    """Return True if color is a non-black RGB tuple/list (at least one channel > 0)."""
    return color and any(c > 0 for c in color)


def _avg_paint_color(colors) -> Optional[tuple]:
    """Average RGB of a paint pixel list (packed ints included), or None if no usable pixels."""
    rgb = []
    for c in colors:
        if isinstance(c, int):
            rgb.append(((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF))
        elif isinstance(c, (list, tuple)) and len(c) >= 3:
            rgb.append(tuple(c[:3]))
    if not rgb:
        return None
    return tuple(sum(ch) // len(rgb) for ch in zip(*rgb))


def led_should_stay_dark() -> bool:
    """True for an explicitly black solid preference or a dark default resting look.

    Self-initiated painting must check this; explicit commands and information cues must not.
    """
    if _user_led_state is None:
        return ambient_resting_is_dark()
    return (_user_led_state.get("type") == LST_SOLID
            and not any(_user_led_state.get("color", [0, 0, 0])))


def note_user_activity(source: str):
    """Tell presence the user is here (voice turn or gesture); never raises."""
    svc = sensing_service
    if svc is None:
        return
    try:
        svc.presence.on_activity(source)
    except Exception as e:
        logger.warning("Presence activity (%s) failed: %s", source, e)


def note_presence_wake():
    """Restart the presence countdown when the device wakes from sleep; never raises."""
    svc = sensing_service
    if svc is None:
        return
    try:
        svc.presence.on_wake()
    except Exception as e:
        logger.warning("Presence wake reset failed: %s", e)


def _get_current_led_color() -> tuple:
    """Return the current LED color for the speaking wave effect."""
    # Nothing may self-light a dark strip: the wave renders on black.
    if led_should_stay_dark():
        return (0, 0, 0)
    if _user_led_state:
        stype = _user_led_state.get("type")
        if stype == LST_SOLID and _is_nonblack(_user_led_state.get("color")):
            return tuple(_user_led_state["color"])
        if stype == LST_EFFECT and _is_nonblack(_user_led_state.get("color")):
            return tuple(_user_led_state["color"])
        if stype == LST_PAINT:
            avg = _avg_paint_color(_user_led_state.get("colors") or [])
            if _is_nonblack(avg):
                return avg
        if stype == LST_SCENE:
            preset = SCENE_PRESETS.get(_user_led_state.get("scene", ""))
            if preset:
                return tuple(int(c * preset["brightness"]) for c in preset["color"])
    if _is_nonblack(_effect_base_color):
        return _effect_base_color
    # Preserve the device's resting brightness even before its first restore.
    return tuple(AMBIENT_RESTING_LED["color"])


def _get_user_base_color() -> tuple:
    """The user's current LED base color for overlay effects, else (0, 0, 0)."""
    if not _user_led_state:
        return (0, 0, 0)
    stype = _user_led_state.get("type")
    if stype in (LST_SOLID, LST_EFFECT):
        color = _user_led_state.get("color")
        return tuple(color) if color else (0, 0, 0)
    if stype == LST_PAINT:
        return _avg_paint_color(_user_led_state.get("colors") or []) or (0, 0, 0)
    if stype == LST_SCENE:
        preset = SCENE_PRESETS.get(_user_led_state.get("scene", ""))
        if preset:
            return tuple(int(c * preset["brightness"]) for c in preset["color"])
    return (0, 0, 0)


def _mic_muted_led_owns_strip() -> bool:
    """True when the mic-muted indicator is the resting look (yields to scenes, not to a dark strip)."""
    if not _mic_muted_led:
        return False
    if _active_scene or (_user_led_state and _user_led_state.get("type") == LST_SCENE):
        return False
    return True


def _start_preset_effect(preset: dict, thread_name: str):
    """Start a preset-described background effect ({"effect","color","speed"}); display-only."""
    global _restore_timer, _effect_thread, _effect_name, _effect_base_color
    if not rgb_service:
        return
    if _restore_timer is not None and _restore_timer.is_alive():
        _restore_timer.cancel()
        _restore_timer = None
    _stop_current_effect()
    color = tuple(preset["color"])
    if preset["effect"] == LST_SOLID:
        rgb_service.dispatch(RGB_CMD_SOLID, color)
        _effect_base_color = color
        return
    _effect_stop.clear()
    _effect_name = preset["effect"]
    _effect_base_color = color
    _effect_thread = threading.Thread(
        target=_run_effect,
        args=(preset["effect"], color, preset.get("speed", 1.0), None, _effect_stop, rgb_service),
        kwargs={"brightness": preset.get("brightness", 1.0)},
        daemon=True,
        name=thread_name,
    )
    _effect_thread.start()


def _start_mic_muted_effect():
    """Paint the mic-muted indicator (dark red breathing); display-only."""
    if _sleeping:
        logger.info("Mic-muted LED skipped -- sleepy owns the strip")
        return
    if not _mic_muted_led_owns_strip():
        return
    _start_preset_effect(STATUS_LED_PRESETS["mic_muted"], "led-mic-muted")


def _apply_mic_muted_led(force: bool = False):
    """Turn on the mic-muted resting indicator (POST /voice/mute).

    force=True (physical switch) paints now even over a live wave.
    """
    global _mic_muted_led
    if _mic_muted_led and not force:
        return
    _mic_muted_led = True
    logger.info("Mic-muted LED indicator ON%s", " (forced)" if force else "")
    if not force and (_tts_speaking or _music_playing):
        return
    _start_mic_muted_effect()


def _clear_mic_muted_led(force: bool = False):
    """Drop the mic-muted indicator and restore the user's saved LED state.

    force=True also kills a red painted over a live wave.
    """
    global _mic_muted_led, _effect_thread, _effect_name, _effect_base_color
    if not _mic_muted_led and not force:
        return
    _mic_muted_led = False
    logger.info("Mic-muted LED indicator OFF -- restoring user state")
    if _tts_speaking or _music_playing:
        if force and _effect_thread is not None and _effect_thread.name == "led-mic-muted":
            # A forced mute painted red over this wave; stop it now.
            _stop_current_effect()
        return
    # No saved user state: stop our effect and settle on the resting look ourselves.
    if _effect_thread is not None and _effect_thread.name == "led-mic-muted":
        _stop_current_effect()
        if _user_led_state is None and rgb_service:
            if ambient_resting_is_dark():
                rgb_service.dispatch(RGB_CMD_SOLID, (0, 0, 0))
                logger.info("Mic-muted LED cleared -- no user state, resting dark")
                return
            _start_preset_effect(AMBIENT_RESTING_LED, "led-ambient-fallback")
            return
    _restore_user_led()


def _dismiss_mic_muted_led(source: str):
    """Explicit user LED command while muted: the user's ask wins the strip; the mic stays muted."""
    global _mic_muted_led
    if not _mic_muted_led:
        return
    _mic_muted_led = False
    logger.info("Mic-muted LED indicator dismissed by %s (mic stays muted)", source)


def _has_internet() -> bool:
    """True if a 1s TCP connect to a public DNS IP succeeds."""
    import socket

    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=1.0):
            return True
    except OSError:
        return False


def _flash_backend_error():
    """3x amber flash for a TTS/backend failure.

    Skipped when the mic-muted indicator owns the strip, before setup, or with no internet.
    """
    if not rgb_service:
        return
    if _mic_muted_led_owns_strip():
        return
    try:
        from hal.config import _os_cfg_get

        if not _os_cfg_get("set_up_completed"):
            logger.info("backend-error flash skipped -- device not set up")
            return
    except Exception:
        # Missing config = not set up; skip.
        logger.info("backend-error flash skipped -- config unreadable")
        return
    if not _has_internet():
        logger.info("backend-error flash skipped -- no internet (statusled connectivity cue owns feedback)")
        return

    def _run_then_settle():
        from hal.drivers.rgb.effects import notification_flash

        color = LED_BACKEND_ERROR_FLASH
        local_stop = threading.Event()
        try:
            notification_flash(color, 1.0, local_stop, rgb_service)
        except Exception as e:
            logger.warning("backend-error flash failed: %s", e)
        try:
            if _mic_muted_led_owns_strip():
                _start_mic_muted_effect()
            else:
                _restore_user_led()
        except Exception as e:
            logger.warning("backend-error flash restore failed: %s", e)

    logger.info("Flashing backend-error cue (3x amber)")
    threading.Thread(
        target=_run_then_settle,
        daemon=True,
        name="led-backend-error-flash",
    ).start()


def _restore_user_led():
    """Restore LED to user state after emotion animation completes."""
    global _restore_timer
    _restore_timer = None

    # Sleep is terminal: late restores must not repaint the strip.
    if _sleeping:
        if rgb_service:
            _stop_current_effect()
            rgb_service.clear()
        logger.info("LED restore skipped -- sleepy owns the strip")
        return

    if _tts_speaking:
        logger.info("LED restore: skipped -- TTS speaking_wave active")
        return

    if _music_playing:
        logger.info("LED restore: skipped -- music wave active")
        return

    if not rgb_service:
        return

    # Mic muted: the resting look is the privacy red.
    if _mic_muted_led_owns_strip():
        logger.info("LED restore: mic muted -- settling on privacy indicator")
        _start_mic_muted_effect()
        return

    # A realtime turn still waiting owns the strip: repaint the thinking cue.
    if _thinking_cue_active:
        logger.info("LED restore: realtime thinking cue still active -- repainting")
        _apply_emotion_led_display(EMO_THINKING, 0.7, force_led=True)
        return

    from hal.drivers.harness.led import restore as restore_harness_led
    if restore_harness_led():
        return

    state = _user_led_state
    if state is None:
        # Dark resting look: "no user state" settles to black.
        if ambient_resting_is_dark():
            _stop_current_effect()
            rgb_service.clear()
            logger.info("LED restore: no user state -- resting dark, cleared")
            return
        _start_preset_effect(AMBIENT_RESTING_LED, "led-ambient-fallback")
        logger.info("LED restore: no user state -- settling on ambient resting")
        return

    stype = state.get("type")
    logger.info("LED restore: restoring user state type=%s", stype)
    try:
        if stype == LST_SOLID:
            _stop_current_effect()
            rgb_service.dispatch(RGB_CMD_SOLID, tuple(state["color"]))
            logger.info("LED restore: solid color=%s", state["color"])
        elif stype == LST_PAINT:
            _stop_current_effect()
            colors = [
                tuple(c) if isinstance(c, list) else c
                for c in state.get("colors") or []
            ]
            rgb_service.dispatch(RGB_CMD_PAINT, colors)
            logger.info("LED restore: paint %d pixels", len(colors))
        elif stype == LST_EFFECT:
            _stop_current_effect()
            global _effect_thread, _effect_name, _effect_base_color
            color = tuple(state["color"])
            speed = state.get("speed", 1.0)
            effect = state["effect"]
            # Old state files lack "brightness"; they were painted at 1.0.
            brightness = state.get("brightness", 1.0)
            _effect_stop.clear()
            _effect_name = effect
            _effect_base_color = color
            _effect_thread = threading.Thread(
                target=_run_effect,
                args=(effect, color, speed, None, _effect_stop, rgb_service),
                kwargs={"brightness": brightness},
                daemon=True,
                name=f"led-restore-{effect}",
            )
            _effect_thread.start()
            logger.info(
                "LED restore: effect=%s color=%s speed=%s", effect, color, speed
            )
        elif stype == LST_SCENE:
            # LED only: re-aiming on every restore killed running recordings (#314).
            preset = SCENE_PRESETS.get(state["scene"])
            if preset:
                _stop_current_effect()
                scaled = tuple(int(c * preset["brightness"]) for c in preset["color"])
                rgb_service.dispatch(RGB_CMD_SOLID, scaled)
                logger.info(
                    "LED restore: scene=%s color=%s", state["scene"], scaled
                )
            else:
                logger.warning(
                    "LED restore: scene=%s not found in SCENE_PRESETS", state["scene"]
                )
    except Exception as e:
        logger.warning("LED restore failed: %s", e)


def clear_listening_cue() -> bool:
    """Clear a stale listening cue if it still owns the visual state."""
    global _current_emotion
    if _current_emotion != EMO_LISTENING:
        return False
    _current_emotion = EMO_IDLE
    _restore_user_led()
    return True


def show_listening_pending_cue() -> Optional[int]:
    """Show a dim, LED-only acknowledgement while gaze-authorized STT starts; returns a token."""
    global _listening_pending_cue_id, _listening_pending_cue_active_id
    if _sleeping or _tts_speaking or _current_emotion not in (None, EMO_IDLE):
        return None
    with _listening_pending_cue_lock:
        _listening_pending_cue_id += 1
        cue_id = _listening_pending_cue_id
        _listening_pending_cue_active_id = cue_id

    # Direct LED helper: no public emotion API and no servo halt.
    _apply_emotion_led_display(EMO_LISTENING, intensity=0.35, force_led=True)

    timer = threading.Timer(
        _LISTENING_PENDING_CUE_TIMEOUT_S,
        clear_listening_pending_cue,
        kwargs={"cue_id": cue_id},
    )
    timer.daemon = True
    timer.start()
    return cue_id


def clear_listening_pending_cue(cue_id: Optional[int] = None, restore: bool = True) -> bool:
    """Remove a pending gaze cue without disturbing a newer visual state."""
    global _listening_pending_cue_active_id
    with _listening_pending_cue_lock:
        active_id = _listening_pending_cue_active_id
        if active_id is None or (cue_id is not None and cue_id != active_id):
            return False
        _listening_pending_cue_active_id = None

    # Restore only if this cue is still the visible overlay.
    if restore and _current_emotion in (None, EMO_IDLE):
        _restore_user_led()
    return True


def _schedule_led_restore(delay_s: float):
    """Schedule _restore_user_led to run after delay_s seconds."""
    global _restore_timer
    if _restore_timer is not None and _restore_timer.is_alive():
        _restore_timer.cancel()
    t = threading.Timer(delay_s, _restore_user_led)
    t.daemon = True
    t.start()
    _restore_timer = t


def _on_tts_speak_start():
    """Called by TTSService when TTS playback begins."""
    global _tts_speaking, _effect_thread, _effect_name, _effect_base_color
    global _restore_timer
    if not rgb_service:
        return
    if _sleeping:
        logger.info("TTS speaking LED skipped -- sleepy owns the strip")
        return

    color = _get_current_led_color()
    if not any(color):
        # Off is an ambient preference, not a request to hide active speech.
        # Reuse the device's dim listening color without saving a new preference.
        color = tuple(EMOTION_PRESETS[EMO_LISTENING]["color"])
    logger.info("TTS speaking LED start: color=%s", color)

    _tts_speaking = True

    if _restore_timer is not None and _restore_timer.is_alive():
        _restore_timer.cancel()
        _restore_timer = None

    _stop_current_effect()

    _effect_stop.clear()
    _effect_name = FX_SPEAKING_WAVE
    _effect_base_color = color
    _effect_thread = threading.Thread(
        target=_run_effect,
        args=(FX_SPEAKING_WAVE, color, 2.5, None, _effect_stop, rgb_service),
        daemon=True,
        name="led-effect-speaking_wave",
    )
    _effect_thread.start()


def _clear_thinking_after_reply():
    """End the `thinking` face when a genuine agent reply finishes speaking (`realtime_feedback`)."""
    global _thinking_cue_active

    if _current_emotion != EMO_THINKING:
        return
    if not (tts_service and getattr(tts_service, "realtime_feedback", False)):
        return

    logger.info("TTS end: agent reply finished -- clearing thinking face")
    _thinking_cue_active = False
    try:
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        express_emotion(EmotionRequest(emotion=EMO_IDLE))
    except Exception as e:
        logger.warning("Thinking clear after reply failed: %s", e)


def _on_tts_speak_end():
    """Called by TTSService when TTS playback finishes or is interrupted."""
    global _tts_speaking
    if not _tts_speaking:
        return

    _tts_speaking = False
    logger.info("TTS speaking LED end: stopping effect and restoring")

    _clear_thinking_after_reply()

    _stop_current_effect()

    _restore_user_led()


def _on_music_play_start():
    """Called when MusicService starts streaming (ffmpeg has begun output)."""
    global _music_playing, _effect_thread, _effect_name, _effect_base_color
    global _restore_timer
    if not rgb_service:
        return
    if _sleeping:
        logger.info("Music wave skipped -- sleepy owns the strip")
        return
    if _tts_speaking:
        # TTS wave owns the strip; don't overwrite it.
        logger.info("Music wave skipped -- TTS speaking_wave active")
        return
    if _music_playing:
        return

    state = _user_led_state
    if state is None:
        effect = FX_SPEAKING_WAVE_RAINBOW
        color = (0, 0, 0)  # ignored; each segment computes its own hue
        name = "led-music-speaking_wave_rainbow"
    else:
        effect = FX_SPEAKING_WAVE
        color = _get_current_led_color()
        name = "led-music-speaking_wave"
    logger.info("Music play LED start: effect=%s color=%s", effect, color)

    _music_playing = True

    if _restore_timer is not None and _restore_timer.is_alive():
        _restore_timer.cancel()
        _restore_timer = None

    _stop_current_effect()

    _effect_stop.clear()
    _effect_name = effect
    _effect_base_color = color
    _effect_thread = threading.Thread(
        target=_run_effect,
        args=(effect, color, 2.5, None, _effect_stop, rgb_service),
        # Rainbow's level comes from the music_strong preset's "brightness".
        kwargs={"brightness": EMOTION_PRESETS[EMO_MUSIC_STRONG].get("brightness", 1.0)},
        daemon=True,
        name=name,
    )
    _effect_thread.start()


def _on_music_play_end():
    """Called when MusicService finishes streaming (natural end, stop, or TTS preempt)."""
    global _music_playing
    if not _music_playing:
        return

    if _tts_speaking:
        # TTS wave already took over; clear flag but don't disturb the strip.
        logger.info("Music wave end deferred -- TTS speaking_wave owns strip")
        _music_playing = False
        return

    _music_playing = False
    logger.info("Music play LED end: stopping effect and restoring")

    _stop_current_effect()

    _restore_user_led()


def _apply_emotion_led_display(
    emotion: str, intensity: float = 1.0, force_led: bool = False
) -> Optional[list]:
    """Apply LED effect + display for an emotion; returns the scaled LED color or None.

    force_led bypasses only the idle background-emotion guard, never sleep.
    """
    preset = EMOTION_PRESETS.get(emotion)
    if not preset:
        return None
    if _sleeping and emotion != EMO_SLEEPY:
        logger.info("Emotion LED skipped (%s) -- sleepy owns the strip", emotion)
        return None
    if _tts_speaking:
        logger.info("Emotion LED skipped (%s) -- TTS speaking_wave active", emotion)
        if display_service:
            try:
                display_service.set_expression(emotion)
            except Exception as e:
                logger.warning("Emotion display failed: %s", e)
        return None
    led_color = None
    # Background emotions must not override the user's ambient color.
    if not force_led and emotion in _BACKGROUND_EMOTIONS and _user_led_state is not None:
        logger.info("Emotion LED skipped (%s) -- respecting user saved state", emotion)
        if display_service:
            try:
                display_service.set_expression(emotion)
            except Exception as e:
                logger.warning("Emotion display failed: %s", e)
        return None
    if rgb_service and preset.get("color"):
        scaled = [int(c * intensity) for c in preset["color"]]
        try:
            if preset.get("effect"):
                # Emotion effects run on a black base; overlay-on-user is for transient driver effects.
                _stop_current_effect()
                global _effect_thread, _effect_name, _effect_base_color
                _effect_stop.clear()
                _effect_name = preset["effect"]
                _effect_base_color = tuple(scaled)
                _effect_thread = threading.Thread(
                    target=_run_effect,
                    args=(
                        preset["effect"],
                        tuple(scaled),
                        preset.get("speed", 1.0),
                        None,
                        _effect_stop,
                        rgb_service,
                    ),
                    # Rainbow ignores intensity; start_at_peak is opt-in per preset.
                    kwargs={
                        "brightness": preset.get("brightness", 1.0),
                        "start_at_peak": preset.get("start_at_peak", False),
                    },
                    daemon=True,
                    name=f"led-emotion-{emotion}",
                )
                _effect_thread.start()
            else:
                rgb_service.dispatch(RGB_CMD_SOLID, tuple(scaled))
                _effect_base_color = tuple(scaled)
            led_color = scaled
        except Exception as e:
            logger.warning("Emotion LED failed: %s", e)
    if display_service:
        try:
            display_service.set_expression(emotion)
        except Exception as e:
            logger.warning("Emotion display failed: %s", e)
    return led_color


@privacy.serialized
def _auto_camera_off(reason: str) -> bool:
    """Auto-disable camera. Respects manual override + active tracking."""
    global _camera_disabled
    if privacy.camera_muted:
        return False
    if _camera_manual_override:
        logger.debug(
            "Auto camera off skipped -- manual override active (reason: %s)", reason
        )
        return False
    # Tracking needs the frame stream; ignore auto-off while tracking.
    if tracker_service and tracker_service.is_tracking:
        logger.info("Auto camera off skipped -- tracking active (reason: %s)", reason)
        return False
    if not camera_capture or _camera_disabled:
        return False
    _camera_disabled = True
    camera_capture.stop()
    _persist_camera_state()
    logger.info("Camera auto-disabled (reason: %s)", reason)
    return True


@privacy.serialized
def _auto_camera_on(reason: str) -> bool:
    """Auto-enable camera. Respects manual override."""
    global _camera_disabled
    if privacy.camera_muted:
        return False
    if _camera_manual_override:
        logger.debug(
            "Auto camera on skipped -- manual override active (reason: %s)", reason
        )
        return False
    if not camera_capture or not _camera_disabled:
        return False
    _camera_disabled = False
    camera_capture.start()
    _persist_camera_state()
    logger.info("Camera auto-enabled (reason: %s)", reason)
    return True


def _read_agent_name() -> str:
    """Resolve the active runtime's name through its context manager layout."""
    from hal.realtime.context_manager import CONTEXT_MANAGERS, OpenClawContextManager

    context_cls = CONTEXT_MANAGERS.get(_hal_config.AGENT_GATEWAY, OpenClawContextManager)
    name = context_cls.read_agent_name(_hal_config.ACTIVE_AGENT_WORKSPACE_DIR)
    if name:
        return name
    # No explicit name -> device type, not a hardcoded "lamp".
    try:
        from hal.config import resolve_device_type
        device_type = resolve_device_type()
        if device_type:
            return device_type
    except Exception:
        pass
    return _DEFAULT_AGENT_NAME


def _build_wake_words(name: str) -> list[str]:
    """Generate wake word variants from agent name."""
    n = name.lower()
    return [
        f"{prefix} {n}"
        for prefix in ("hello", "hey", "hi", "alo", "okay", "ok", "wake up")
    ]


def _stt_boost_terms() -> list[str]:
    """Names STT must not mangle (agent name, device type, "autonomous").

    Returned as Deepgram `keyword:intensifier`, deduplicated.
    """
    from hal.config import resolve_device_type

    seen: set[str] = set()
    terms: list[str] = []
    for name in (_read_agent_name(), resolve_device_type(), "autonomous"):
        name = (name or "").strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        terms.append(f"{name}:3")
    return terms


def _find_audio_device(output: bool = True) -> Optional[int]:
    """Find audio device index by known hardware names, with USB fallback."""
    try:
        import sounddevice as sd
    except ImportError:
        return None
    if not sd:
        return None
    output_names = ["seeed", "cd002"]
    input_names = ["seeed", "webcam"]
    input_skip = ["camera", "video"]
    names = output_names if output else input_names
    try:
        import re
        import subprocess

        devices = list(sd.query_devices())
        for keyword in names:
            for i, d in enumerate(devices):
                name = d["name"].lower()
                if keyword not in name:
                    continue
                if output and d["max_output_channels"] > 0:
                    return i
                if not output and d["max_input_channels"] > 0:
                    return i
        for i, d in enumerate(devices):
            name = d["name"].lower()
            if "usb" not in name:
                continue
            if not output and any(s in name for s in input_skip):
                continue
            if output and d["max_output_channels"] > 0:
                logger.info(
                    "Audio fallback: using USB device %d '%s' for output", i, d["name"]
                )
                return i
            if not output and d["max_input_channels"] > 0:
                logger.info(
                    "Audio fallback: using USB device %d '%s' for input", i, d["name"]
                )
                return i
        if not output:
            for i, d in enumerate(devices):
                name = d["name"].lower()
                if "usb" in name and d["max_input_channels"] > 0:
                    logger.info(
                        "Audio last-resort: using %d '%s' for input", i, d["name"]
                    )
                    return i
        alsa_cmd = ["aplay", "-l"] if output else ["arecord", "-l"]
        try:
            result = subprocess.run(alsa_cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if not line.startswith("card "):
                        continue
                    m = re.search(r"card \d+: \S+ \[(.+?)\]", line)
                    if not m:
                        continue
                    card_label = m.group(1).lower()
                    if any(s in card_label for s in ("hdmi", "spdif", "iec958")):
                        continue
                    label_words = [w.lower() for w in m.group(1).split() if len(w) > 2]
                    for i, d in enumerate(devices):
                        dname = d["name"].lower()
                        if any(w in dname for w in label_words):
                            if output and d["max_output_channels"] > 0:
                                logger.info(
                                    "ALSA probe: device %d '%s' for output",
                                    i,
                                    d["name"],
                                )
                                return i
                            if not output and d["max_input_channels"] > 0:
                                logger.info(
                                    "ALSA probe: device %d '%s' for input", i, d["name"]
                                )
                                return i
        except Exception:
            pass
        skip = ["hdmi", "spdif", "iec958"]
        for i, d in enumerate(devices):
            dname = d["name"].lower()
            if any(s in dname for s in skip):
                continue
            if output and d["max_output_channels"] > 0:
                logger.info(
                    "Audio fallback (any): device %d '%s' for output", i, d["name"]
                )
                return i
            if not output and d["max_input_channels"] > 0:
                logger.info(
                    "Audio fallback (any): device %d '%s' for input", i, d["name"]
                )
                return i
    except Exception:
        pass
    return None
