"""
Shared mutable state for the HAL server.

All service references, flags, and cross-route helpers live here so route
modules can import them without circular dependencies (routes never import
from server; server imports routes).
"""

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

# Background emotions don't override user's saved LED state. They still
# fire servo + display, just skip LED to keep user ambient color visible.
# Foreground emotions (listening, happy, excited, shock, etc.) always
# fire LED — they're visible responses the user expects to see.
_BACKGROUND_EMOTIONS = {EMO_IDLE, EMO_THINKING}
from hal.drivers.rgb.effects import run_effect as _run_effect

logger = logging.getLogger("hal.server")

# --- Service references (set during lifespan) ---

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
# A logging-only PolicyService until a body supplies a safety-gated learned
# policy executor.  See hal/policy/service.py.
policy_service = None

# Resolved SAFETY.md bounds (hal.safety.policy.SafetyPolicy), or None when the
# device declares none. Routes consult it as a deterministic gate before
# actuating (e.g. music suppressed during audio quiet hours). The LED gate gets
# the same policy via RGBService(safety_policy=...).
safety_policy = None

# Thermal fail-safe state, updated by the background SoC-temp monitor (server.py)
# when `thermal` bounds are declared. thermal_over flips True on over-temp and
# clears on cool-down (hysteresis); soc_temp_c is the last reading (None if not
# monitored / unreadable). Surfaced at GET /health.
thermal_over = False
soc_temp_c = None

# --- Audio devices ---

audio_output_device: Optional[int] = None
audio_input_device: Optional[int] = None
# Simulation exposes an in-memory microphone/speaker pair. Keeping this state
# here lets the audio routes preserve their HTTP contract without querying the
# developer's macOS devices.
simulation_audio: bool = config.SIMULATE and config.SIM_MEDIA != "host"
simulation_volume: int = 65

# --- Simulation media mode (HAL_SIM_MEDIA) ---
#
# `sim_media_requested` is what the developer asked for on the command line;
# `sim_media_camera` / `sim_media_audio` are what each subsystem ACTUALLY runs
# after boot, because a host device can be missing, busy, or permission-denied.
# The two are kept apart on purpose: a Mac with a working webcam but a denied
# microphone must report host video and virtual audio, not one blurred answer.
# `sim_media_reasons` carries the human-actionable why for each downgrade and is
# surfaced at GET /simulator/state so the page never shows a still image while
# claiming to be live.
sim_media_requested: str = config.SIM_MEDIA
sim_media_camera: str = sim_media_requested
sim_media_audio: str = sim_media_requested
sim_media_reasons: dict = {}


def sim_media_fallback(kind: str, reason: str) -> None:
    """Downgrade one simulated media subsystem to the virtual device.

    `kind` is "camera" or "audio". Idempotent: the first reason wins, so a
    retry loop cannot overwrite the original cause with a vaguer one.
    """
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

# --- Camera state ---

_camera_disabled = False
_camera_manual_override = False

# Most recent frame captured by the realtime `look` tool, persisted to disk so a
# turn that delegates / falls back to the main agent can hand off the SAME image
# (by file path) instead of making the agent snapshot again — faster, and the
# agent answers about the exact frame the user pointed at. Set in the realtime
# orchestrator's look handler; consumed + cleared once in turn_dispatch (strictly
# per-turn). Path is a file in _SNAPSHOT_DIR; ts is time.monotonic() of capture.
realtime_look_frame_path: Optional[str] = None
# Servable copy of the same frame (under /var/lib/hal/snapshots/sensing_look/)
# so the turn the user sees can show it as a thumbnail. Consumed by turn_dispatch.
realtime_look_monitor_path: Optional[str] = None
realtime_look_frame_ts: float = 0.0

# --- Voice identity (speaker-ID as a presence signal) ---
#
# Face identity is STATEFUL: every camera frame refreshes `current_user`, so it
# is always populated while someone is visible. Speaker-ID was the opposite — a
# per-turn value that evaporated when the turn ended — so a device with no
# camera (or nobody in frame) had NO identity at all, even right after
# recognizing an enrolled speaker.
#
# These slots give voice the same shape as face: a last-known label that ages
# out. Written ONLY on a confident match (see set_voice_user); an unknown /
# rejected / failed recognition must never overwrite a good value, and must
# never invent one.
#
# `_voice_user` is the NORMALIZED label (e.g. "long") — the same shape face
# reports and the same slug the per-user folders use, so downstream attribution
# and the UI's photo lookup work identically for both modalities.
# `_voice_user_display` is the human spelling ("Long") for display only.
_voice_user: Optional[str] = None
_voice_user_display: Optional[str] = None
_voice_user_ts: float = 0.0
_voice_user_lock = threading.RLock()


def set_voice_user(label: str, display: Optional[str] = None) -> None:
    """Record the speaker resolved for the turn that just finished.

    Call ONLY with a confident speaker-ID match. `label` is the normalized
    name from the recognizer (result["name"]), not the decorated transcript
    prefix and not the display spelling.
    """
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
    """Return (label, display, age_s) for the current voice user.

    ("", "", 0.0) when nobody has spoken recently — either nothing was ever
    matched, or the last match aged past VOICE_USER_FORGET_S.
    """
    with _voice_user_lock:
        if not _voice_user:
            return "", "", 0.0
        age = time.time() - _voice_user_ts
        if age > config.VOICE_USER_FORGET_S:
            return "", "", 0.0
        return _voice_user, _voice_user_display or _voice_user, age


def face_user() -> tuple[str, float]:
    """Return (label, age_s) for the face-derived user; ("", 0.0) for nobody.

    age_s is seconds since that person was last actually in frame — NOT 0 just
    because a face answered. `current_user()` keeps returning a friend for
    FACE_OWNER_FORGET_S (1h) after they leave, so the age is what distinguishes
    "standing here" from "left 50 minutes ago".

    Calls the perception object's ``current_user()`` — which RECOMPUTES from the
    live people map — rather than reading the cached
    ``_perception_state.current_user.data`` mirror. The mirror only advances when
    a frame is processed, which made it wrong in two ways: it survived
    ``/face/cooldowns/reset`` (the reset clears the people map, not the mirror),
    and it froze at the last person seen whenever frames stopped (camera
    disabled, perception stopped). Either way identity kept reporting a face
    user, and since face wins, the voice speaker was never consulted — breaking
    exactly the camera-off case this resolution exists for.

    Same accessor `/face/current-user` uses (``routes/sensing.py``), so the two
    endpoints agree by construction.

    "" when face perception never started (no `presence` capability, no camera),
    when nobody is in frame, or if the lookup fails — all of which correctly let
    the voice speaker fill the slot.
    """
    try:
        if not sensing_service:
            return "", 0.0
        fr = sensing_service._perception_orchestrator._processors.face_recognizer
        if fr is None:
            return "", 0.0
        # getattr: an older perception.py (partial file sync) has only
        # current_user() — degrade to a nameless age rather than raising.
        with_age = getattr(fr, "current_user_with_age", None)
        if with_age is None:
            return fr.current_user() or "", 0.0
        label, age = with_age()
        return label or "", age
    except Exception:
        logger.exception("[identity] face current_user lookup failed")
    return "", 0.0


def resolve_current_user() -> tuple[str, str, str, float]:
    """Resolve who the device is with right now → (label, display, source, age_s).

    **Face always wins.** A visible face is continuously re-proven by the
    camera, while a voice proves only that someone spoke at one instant, so
    voice is consulted ONLY when face reports nobody. That ordering also keeps
    existing camera devices behaving exactly as before: voice can fill an empty
    slot, never displace a live face.

    source is "face", "voice", or "" when neither modality has anyone.
    age_s is seconds since that identity was last positively observed — last
    seen in frame for face, last spoken for voice — so the two are comparable.

    This is the single definition of the rule — every producer (sensing events,
    voice turns, the identity route) calls this rather than re-deriving it.
    """
    face, face_age = face_user()
    if face:
        return face, face, "face", face_age
    label, display, age = voice_user()
    if label:
        return label, display, "voice", age
    return "", "", "", 0.0

# --- LED effect state ---

_effect_thread: Optional[threading.Thread] = None
_effect_stop: threading.Event = threading.Event()
_effect_name: Optional[str] = None
_effect_base_color: Optional[tuple] = None
_active_scene: Optional[str] = None

# --- User LED state tracking (for emotion restore) ---

_user_led_state: Optional[dict] = None
_restore_timer: Optional[threading.Timer] = None
_sleeping: bool = False
_current_emotion: Optional[str] = None
# True while the realtime turn is showing its `thinking` cue. A dead-air
# filler is TTS, so it stops the pulse and _restore_user_led would settle on
# the user state — leaving the rest of the wait (often the longest part, the
# reason the filler fired at all) with no visual at all. While this is set,
# restore repaints the cue instead. Cleared by the same code that clears the
# cue (hal/drivers/voice/_internal/realtime_turn.py).
_thinking_cue_active: bool = False
# Fires release_servos after sleepy stays active continuously. Cancelled
# the moment the emotion changes away from sleepy (see routes/emotion.py).
_sleepy_release_timer: Optional[threading.Timer] = None
# Speaker drain for sleepy. os-server POSTs the reply text ~100ms after the
# `sleepy` marker (fireHWCallsSync's budget) and HAL takes about as long to
# reach _finalize_sleepy_peripherals, so muting there swallowed the
# going-to-sleep line more often than not.
SLEEPY_SPEAKER_GRACE_S = 2.0       # wait this long for an announcement to START
SLEEPY_SPEAKER_DRAIN_MAX_S = 15.0  # hard cap on the whole drain
_SLEEPY_DRAIN_POLL_S = 0.1
_sleepy_drain_cancel: Optional[threading.Event] = None
# Same drain for scenes with speaker "off": the /scene marker lands before the
# reply text, so an inline mute swallowed the scene's own line. Sleepy takes
# the drain over when both arrive in one reply.
_scene_drain_cancel: Optional[threading.Event] = None
# Fires idle again after a still emotion (a preset with servo=None) halted the
# animation loop, so the body never stays frozen once the moment has passed.
# Cancelled on every /emotion (see routes/emotion.py).
_still_idle_timer: Optional[threading.Timer] = None
# Fires idle after `thinking` has been held continuously for too long — the
# last-resort net for a turn that never produced the emotion that would have
# replaced it. Cancelled/re-armed on every /emotion (see routes/emotion.py).
_thinking_reset_timer: Optional[threading.Timer] = None
# Gaze has already established intent by the time VAD confirms speech, but STT
# needs another 1.5-2.5s before its first partial. This short, LED-only cue
# bridges that gap without claiming the full listening state or halting motion.
_LISTENING_PENDING_CUE_TIMEOUT_S = 3.0
_listening_pending_cue_id = 0
_listening_pending_cue_active_id: Optional[int] = None
_listening_pending_cue_lock = threading.Lock()
# Set once sleepy has released torque. Servo routes honor this lock until a
# wake emotion explicitly resumes the motion service.
_sleep_servo_released = False
_sleep_servo_lock = threading.RLock()
# These flags track only mutes owned by sleepy; a wake must never undo a
# manual user mute.
_sleepy_auto_muted_mic = False
_sleepy_auto_muted_speaker = False

# --- TTS speaking LED state ---

_tts_speaking: bool = False

# --- Music playback LED state ---

_music_playing: bool = False

# --- Mic / Speaker mute state ---

_mic_muted = False
_mic_manual_override = False
_speaker_muted = False

# Hardware mic-mute slide switch position (Intern v2 Pro's PD1 kill switch).
# None on devices without the switch (Lamp) — the web UI uses that to decide
# whether to show the "HW switch is off" hint at all. True/False mirrors the
# physical position, published by hal.drivers.privacy_button on every reconcile.
# When True, /voice/unmute rejects with 409 and single_click_action bails
# early: the slide switch is the authority whenever it is physically muted.
_hw_mic_switch_muted: "bool | None" = None

# Mic-muted idle LED indicator (STATUS_LED_PRESETS["mic_muted"], dark red
# breathing). Set by /voice/mute, cleared by /voice/unmute. It is the strip's
# RESTING look while muted: emotions/effects/waves still run normally on top,
# but every _restore_user_led lands back on the red instead of the user state
# — "nothing happening + red breathing" tells the user the mic is muted.
# An explicit user LED command (/led/solid|off|effect|paint) dismisses it
# (the user's ask wins; mic stays muted). Yields to active scenes only.
_mic_muted_led = False

# True only while a live voice enrollment is recording. record-enroll sets
# _speaker_muted as a transient guard (keep TTS out of the captured WAV), which
# is NOT a user preference — this flag lets the single-click "unmute speaker"
# gesture skip it so a stray click can't relax the mute mid-recording.
_enrolling = False

# --- Music cancel watermark ---
#
# time.monotonic() of the last cancel gesture (single click). The Go-side
# speech/cancel watermark only mutes TTS — the cancelled turn keeps running and
# its pending music tool call still reaches /audio/play, so a click could be
# followed seconds later by music the user just killed (the yt-dlp resolve takes
# 1-5s, and MusicService.play() clears its own _stop_event, so a point-in-time
# audio_stop() cannot win that race).
#
# /audio/play refuses any request landing within MUSIC_CANCEL_GUARD_S of the
# mark. Monotone like the Go watermark: a later click only moves it forward.
# 0.0 = no cancel yet.
_music_cancel_ms: float = 0.0

# Guard window after a cancel click during which /audio/play is refused.
# Sized to cover the cancelled turn's in-flight tool call (agent → OS server →
# HAL is well under a second) while staying below the floor of a genuinely NEW
# music request: after a click the user still has to speak, be transcribed, and
# have the LLM emit the tool call — never under ~3s in practice.
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


# Set True by destructive button actions (long_press, factory_reset) right
# before they kick `shutdown`/`reboot`/OS-server reset. The lifespan shutdown
# handler in server.py checks this flag so it doesn't speak a second
# "shutting down" when systemd's SIGTERM lands seconds later.
_shutdown_announced = False

# --- Snapshot state ---

# Agent-aware (config.SNAPSHOT_DIR follows AGENT_GATEWAY → the active runtime's
# media root, so the agent's image tool can read the saved frame). See config.py.
from hal import config as _hal_config

_SNAPSHOT_DIR = _hal_config.SNAPSHOT_DIR
_SNAPSHOT_MAX = 20
_snapshot_paths: list = []

# --- Default user ---

DEFAULT_USER = config.DEFAULT_USER

# --- Agent workspace ---

_DEFAULT_AGENT_NAME = "friend"  # last-resort only; device_type is preferred (see _read_agent_name)


# ---------------------------------------------------------------------------
# Cross-route helper functions (used by multiple route groups)
# ---------------------------------------------------------------------------


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


# Boot-scoped sidecar for the user LED state — same pattern as the scene /
# presence / motion sidecars. Without it a HAL service restart wipes
# _user_led_state, and the os-server's post-boot POST /led/restore then finds
# "no user state" and CLEARS the strip — the lamp goes dark for ~45s until
# ambient breathing kicks in, instead of coming back in the user's color.
# Every boot-scoped sidecar below lives here. The directory itself is declared
# in config.py with the rest of the env-driven settings — this module reads it,
# it does not own it.
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
        # Legacy sidecar written before "off" stopped being its own state:
        # {"type": "off"} now means the same thing as no state at all, so
        # normalise it away instead of carrying a type nothing understands.
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


# --- Peripheral switch sidecars (boot-scoped, one file per peripheral) ---
# Mic/speaker mute and camera disable are user-facing switches that must
# survive a HAL service restart (OTA, deploy, config change) — without these
# every restart silently unmutes the mic, re-enables the speaker and turns
# the camera back on. One sidecar per peripheral so each switch is written,
# inspected and cleared independently. Same boot_id rule as the LED sidecar:
# a full device reboot starts fresh (and on switch-equipped devices the
# physical mic switch re-applies itself at boot regardless).
# NOT persisted: record-enroll's transient speaker mute (routes/speaker.py
# writes the flag directly and never calls the persist helpers — by design).
_MIC_STATE_PATH = _state_path("hal-mic-state.json")
_SPEAKER_STATE_PATH = _state_path("hal-speaker-state.json")
_CAMERA_STATE_PATH = _state_path("hal-camera-state.json")
# Sleep is the same class of user-facing switch: someone (or a night scene) put
# the device to sleep, and a HAL restart must not undo that. An OTA restarts HAL
# — so before this sidecar, updating HAL while the device slept woke it up, with
# the strip back on and the mic listening, in the middle of the night.
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
    """Persist sleep AND the mutes sleep itself owns.

    The mute flags are deliberately absent from the mic/speaker sidecars:
    _finalize_sleepy_peripherals marks them sleep-owned so waking restores the
    user's own choice, and writing them there would leak sleep's mute into that.
    But in-memory-only meant a restart came back with the mic listening and the
    speaker live on a sleeping device — a turn still in flight then spoke out
    loud. So they ride with sleep, in sleep's own sidecar, and are restored
    together with it."""
    _save_boot_sidecar(
        _SLEEP_STATE_PATH,
        {
            "sleeping": _sleeping,
            "auto_muted_mic": _sleepy_auto_muted_mic,
            "auto_muted_speaker": _sleepy_auto_muted_speaker,
        },
    )


def _prune_sleep_log():
    """Drop journal days past the retention window. Cheap to run inline: a
    device produces a handful of transitions a day, so this lists one small
    directory a few times per day, not per turn.

    Swallows its own failures rather than letting them surface as the caller's:
    an un-prunable old file is a disk-space question, not a reason to report
    that a transition went unrecorded when it did not.
    """
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
    """Append one sleep/wake transition to today's journal.

    The sleep sidecar cannot answer "how many times have I slept": it holds a
    single record, every transition overwrites it, and a reboot deletes it. So
    the device had no way to know it had ever slept -- the agent asked and got
    nothing, because nothing was ever written down.

    Called from the ONE place every route into and out of sleep converges,
    routes/emotion.py, so the physical button is recorded as faithfully as an
    agent's marker. That matters: os-server's flow events only see transitions
    it fired itself, which is roughly half of them, and the half they miss is
    the half a person caused by hand.

    Never raises. A journal that cannot be written is worth strictly less than
    the sleep it describes, so a failure here logs and lets sleep proceed.
    """
    import json

    from hal.clock import device_fromtimestamp, device_timezone

    try:
        os.makedirs(config.SLEEP_LOG_DIR, exist_ok=True)
        ts = time.time()
        # Local wall-clock, resolved through hal.clock so it follows the CURRENT
        # /etc/timezone -- the agent and the web UI can both change the zone at
        # runtime, and datetime.now() would keep the one glibc cached when HAL
        # started. `local` is the field a reader actually wants; `ts` stays as
        # the ordering key and the only value safe to subtract across a zone
        # change. `tz` names the zone so a row can be re-read years later, and
        # is empty exactly when the zone could not be resolved and the device
        # fell back to naive local time -- a wrong clock then shows up in the
        # data instead of hiding in it.
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
    """Start the voice pipeline unless a live enrollment owns the mic.

    ALSA capture is exclusive. record-enroll stops the pipeline, records with
    arecord, then restarts it from its own finally block. A concurrent start()
    on any other path steals the capture device mid-recording and BOTH sides
    lose: the enroll dies with "audio open error: Device or resource busy" (a
    500 to the web UI) and the voice loop dies on the same error. Every caller
    except record-enroll's own restore must go through this gate.

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
    """Enter silent sleep immediately without changing manual user mutes.

    Sleep owns the resting LED state. Teardown callbacks from an emotion, TTS,
    or music session can arrive after this function returns, so they must not
    revive the mic-muted indicator or a previous user/effect state.
    """
    global _sleepy_auto_muted_mic, _sleepy_auto_muted_speaker
    global _mic_muted, _speaker_muted
    if _current_emotion != EMO_SLEEPY:
        return

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
        # Music is never an announcement. The speaker goes to the drain, which
        # also means in-flight TTS is no longer cut off mid-word.
        if music_service and music_service.playing:
            music_service.stop()
        if not _speaker_muted:
            # A pending scene drain yields to sleep so wake can restore the mute.
            _cancel_scene_speaker_drain()
            _start_sleepy_speaker_drain()

    _persist_sleep_state()
    logger.info("Sleepy finalized: LED off, mic muted, speaker draining")


def _run_speaker_drain(cancel: threading.Event, commit, name: str) -> None:
    """Let the current turn's announcement play, then call `commit`.

    Waits SLEEPY_SPEAKER_GRACE_S for TTS to start (well above the ~100ms
    marker→text gap), lets it finish, and gives up at SLEEPY_SPEAKER_DRAIN_MAX_S.
    Setting `cancel` abandons the drain without committing.
    """

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


def _start_sleepy_speaker_drain():
    """Mute the speaker once the going-to-sleep announcement has played.

    A wake cancels the drain.
    """
    global _sleepy_drain_cancel
    _cancel_sleepy_speaker_drain()
    cancel = threading.Event()
    _sleepy_drain_cancel = cancel
    _run_speaker_drain(cancel, _mute_speaker_for_sleep, "sleepy-speaker-drain")


def _cancel_sleepy_speaker_drain():
    """Stop a pending drain. Safe to call when none is running."""
    global _sleepy_drain_cancel
    if _sleepy_drain_cancel is not None:
        _sleepy_drain_cancel.set()
        _sleepy_drain_cancel = None


def _start_scene_speaker_drain(scene: str):
    """Mute the speaker for `scene` once its confirmation line has played.

    Skipped while sleep owns the speaker (asleep or sleepy drain pending).
    """
    global _scene_drain_cancel
    _cancel_scene_speaker_drain()
    if _sleeping or _sleepy_drain_cancel is not None:
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
    """Commit the deferred mute.

    Re-reads the sleep state under privacy.lock, the same lock the wake path
    takes. Checking it unlocked was not enough: a wake cancels the drain, but
    the drain can already be past that check, and it would then re-mute a
    device that has just woken — silent, marked sleep-owned, with the restore
    already run and nothing left to undo it.
    """
    global _speaker_muted, _sleepy_auto_muted_speaker
    with privacy.lock:
        if not _sleeping or _current_emotion != EMO_SLEEPY:
            logger.info("Sleepy speaker drain: awake again -- speaker left live")
            return
        if _speaker_muted:
            return
        _speaker_muted = True
        _sleepy_auto_muted_speaker = True
        _persist_sleep_state()

    # The flag only gates playback that has not STARTED yet (every check in
    # drivers/voice/tts/service.py is an entry guard), so a TTS still speaking
    # when the cap fires would talk on past it — exactly what
    # SLEEPY_SPEAKER_DRAIN_MAX_S exists to prevent. Stopping it is what makes
    # the cap a real bound. Outside the lock: stop() can block on the audio
    # device. A no-op on the normal path, where the drain already waited for
    # the announcement to finish.
    if tts_service and tts_service.speaking:
        tts_service.stop()
    logger.info("Sleepy speaker drain done -- speaker muted")


def _wake_sleepy_peripherals():
    """Restore only mic/speaker states that sleepy itself muted."""
    global _sleepy_auto_muted_mic, _sleepy_auto_muted_speaker, _mic_muted, _speaker_muted
    # Cancel and restore under ONE lock. Cancelling outside it left a drain
    # that was already past its own guard free to re-mute the speaker after
    # the restore had run, on a device that is awake by then.
    with privacy.lock:
        _cancel_sleepy_speaker_drain()
        if _sleepy_auto_muted_speaker:
            if privacy.speaker_muted:
                # The privacy overlay may have captured sleep's temporary mute
                # during HAL startup. Wake removes that mute underneath the
                # lock, so releasing privacy cannot restore an expired mute.
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
    """Restore the switches at import. APPLYING them happens where each
    peripheral boots: routes/voice.py start_voice skips opening the mic,
    server.py lifespan skips starting the camera and re-paints the mic-muted
    LED indicator. The speaker flag needs no apply step — TTS checks it at
    speak time."""
    global _mic_muted, _mic_manual_override, _speaker_muted
    global _camera_disabled, _camera_manual_override, _mic_muted_led
    global _sleeping, _sleepy_auto_muted_mic, _sleepy_auto_muted_speaker
    if d := _load_boot_sidecar(_MIC_STATE_PATH):
        _mic_muted = bool(d.get("muted"))
        _mic_manual_override = bool(d.get("manual_override"))
        # Indicator follows the restored mute; painted at the end of
        # lifespan startup once the RGB service is up.
        _mic_muted_led = _mic_muted
    if d := _load_boot_sidecar(_SPEAKER_STATE_PATH):
        _speaker_muted = bool(d.get("muted"))
    if d := _load_boot_sidecar(_CAMERA_STATE_PATH):
        _camera_disabled = bool(d.get("disabled"))
        _camera_manual_override = bool(d.get("manual_override"))
    if d := _load_boot_sidecar(_SLEEP_STATE_PATH):
        # Restoring the flag re-arms every gate that reads it (sensing, LED,
        # servo, music). The mutes come back with it, still marked sleep-owned
        # so the next wake hands the mic and speaker back to whatever the USER
        # had chosen. Nothing is applied to hardware here: the body is already
        # in the sleep pose and stays limp (AnimationService.start(skip_wake)),
        # and TTS/voice consult these flags at speak/listen time.
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
    """Average RGB of a paint pixel list (packed-int pixels included).

    Paint states have no single color, but presence dimming and overlay
    effects need one base color — the average is the closest stand-in.
    Returns None when the list has no usable pixels.
    """
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
    """True when nothing may light the strip of its own accord.

    THE single source of truth for "leave the lamp alone": no user colour, and
    a dark resting look (see AMBIENT_RESTING_LED). This is the state the device
    boots into — the user LED sidecar is boot-scoped — and the state
    POST /led/off returns it to.

    Anything that paints the strip WITHOUT the user asking right now — the
    speaking waves, the resting settle, presence, ambient breathing — must
    check this. An explicit user/agent command must NOT: that IS the user
    asking, and it overwrites the saved state. Neither must a cue that carries
    information the user needs (a status LED, the mic-muted indicator): those
    earn their light.
    """
    return _user_led_state is None and ambient_resting_is_dark()


def note_user_activity(source: str):
    """Tell presence the user is here: a voice turn or a physical gesture.

    Presence otherwise only hears the camera, so a user talking with the camera
    off or outside the frame timed out to AWAY and the device went to sleep
    mid-conversation. Never raises: presence is a courtesy to the caller.
    """
    svc = sensing_service
    if svc is None:
        return
    try:
        svc.presence.on_activity(source)
    except Exception as e:
        logger.warning("Presence activity (%s) failed: %s", source, e)


def note_presence_wake():
    """Restart the presence countdown when the device wakes from sleep.

    Called from the one place every wake converges (express_emotion), so the
    web UI, API and agent wakes reset it too, not only a face on camera.
    Never raises: a presence failure must not break the wake.
    """
    svc = sensing_service
    if svc is None:
        return
    try:
        svc.presence.on_wake()
    except Exception as e:
        logger.warning("Presence wake reset failed: %s", e)


def _get_current_led_color() -> tuple:
    """Return the current LED color for the speaking wave effect."""
    # Nothing may self-light a dark strip → the wave renders on black
    # (inaudible-equivalent: invisible) instead of the warm fallback below.
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
    # Last-resort warm white. Only reachable when the resting look is LIT (a
    # dark resting look returns black at the top of this function), i.e. the
    # device is configured to glow at rest and the wave should match it.
    return (255, 180, 100)


def _get_user_base_color() -> tuple:
    """Return the user's current LED base color for overlay effects.

    Falls back to (0, 0, 0) when the strip has no active user state — pulse
    then behaves like the original wavefront-on-black animation.
    """
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
    """True when the mic-muted indicator is the strip's current resting look.

    Yields to an active scene (reading/focus lighting is functional). The flag
    stays set, so leaving the scene while still muted brings the red back on
    the next restore. It does NOT yield to a dark strip: the indicator is the
    only signal that the mic is off, so it outranks "the lamp is resting"."""
    if not _mic_muted_led:
        return False
    if _active_scene or (_user_led_state and _user_led_state.get("type") == LST_SCENE):
        return False
    return True


def _start_preset_effect(preset: dict, thread_name: str):
    """Start a preset-described background effect ({"effect","color","speed"}).

    Display-only: never touches _user_led_state. Cancels any pending
    restore timer and running effect first."""
    global _restore_timer, _effect_thread, _effect_name, _effect_base_color
    if not rgb_service:
        return
    if _restore_timer is not None and _restore_timer.is_alive():
        _restore_timer.cancel()
        _restore_timer = None
    _stop_current_effect()
    color = tuple(preset["color"])
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
    """Paint the mic-muted indicator (dark red breathing). Display-only:
    never touches _user_led_state, so unmute restores the saved user look."""
    if _sleeping:
        logger.info("Mic-muted LED skipped -- sleepy owns the strip")
        return
    if not _mic_muted_led_owns_strip():
        return
    _start_preset_effect(STATUS_LED_PRESETS["mic_muted"], "led-mic-muted")


def _apply_mic_muted_led(force: bool = False):
    """Turn on the mic-muted resting indicator (called by POST /voice/mute).

    Paints immediately unless a TTS/music wave owns the strip — then the
    wave-end restore lands on the red (see _restore_user_led).

    force=True (physical mic switch) paints NOW even over a live wave: a
    hardware throw is the most deliberate mute there is, so its feedback
    must not wait out the current utterance. TTS audio keeps playing —
    only the wave visual is replaced; the wave-end restore then keeps
    settling on the red as usual."""
    global _mic_muted_led
    if _mic_muted_led and not force:
        return
    _mic_muted_led = True
    logger.info("Mic-muted LED indicator ON%s", " (forced)" if force else "")
    if not force and (_tts_speaking or _music_playing):
        return
    _start_mic_muted_effect()


def _clear_mic_muted_led(force: bool = False):
    """Drop the mic-muted indicator and restore the user's saved LED state
    (called by POST /voice/unmute and scene mic-unmute paths).

    force=True (physical mic switch) also kills a red painted over a live
    wave (see _apply_mic_muted_led force) so the strip never shows "muted"
    while the mic is actually hot — the wave-end restore then lands on the
    user state."""
    global _mic_muted_led, _effect_thread, _effect_name, _effect_base_color
    if not _mic_muted_led and not force:
        return
    _mic_muted_led = False
    logger.info("Mic-muted LED indicator OFF -- restoring user state")
    if _tts_speaking or _music_playing:
        if force and _effect_thread is not None and _effect_thread.name == "led-mic-muted":
            # A forced mute painted red over this wave; stop it now. The
            # strip stays dark for the rest of the utterance and the
            # wave-end restore repaints the user state.
            _stop_current_effect()
        return
    # With no saved user state (fresh boot, user never picked a color)
    # _restore_user_led is a no-op ("keeping emotion color") and would
    # leave the red breathing running. If the active effect is ours, stop
    # it and settle on the ambient resting look ourselves — the Go ambient
    # loop thinks its effect is still running and won't re-light the strip
    # until its next pause/resume cycle.
    # A dark resting look means the strip stays cleared — see AMBIENT_RESTING_LED.
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
    """Explicit user LED command while muted: the user's ask wins the strip
    and restores stop re-asserting the red. The mic itself STAYS muted.
    Caller paints right after, so no restore here."""
    global _mic_muted_led
    if not _mic_muted_led:
        return
    _mic_muted_led = False
    logger.info("Mic-muted LED indicator dismissed by %s (mic stays muted)", source)


def _has_internet() -> bool:
    """Fast liveness check: 1s TCP connect to public DNS (IP literal, no DNS
    lookup so a broken resolver can't stall us). Returns True iff we can reach
    the internet from this device right now. Used by _flash_backend_error to
    suppress the amber cue when a TTS failure is really just "no network"."""
    import socket

    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=1.0):
            return True
    except OSError:
        return False


def _flash_backend_error():
    """3x amber flashes signalling a TTS/backend failure. Called from the
    TTS service when a speak() returns 0 samples (Cloudflare 524, upstream
    timeout, all retries exhausted) — the user hears no reply and would
    otherwise be left wondering; the flash makes the failure visible.

    Runs a short notification_flash (~1s) on its own thread and then hands
    the strip back via _restore_user_led / mic-muted repaint, so it never
    leaves the strip stuck at the last flash frame. Skips silently when
    the mic-muted privacy indicator owns the strip — a hardware kill-switch
    red must survive any error signal. Also skips when:
    - Device hasn't finished setup (set_up_completed=false): the TTS
      backend URL is unconfigured / in AP mode, failures are expected,
      and statusled's "setup" white already owns the strip.
    - No internet is reachable at all: statusled fires the "connectivity"
      orange breathing for that; layering an amber flash on top is
      redundant noise for a failure the user already sees a cue for."""
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
        # Missing config = not set up; err on the side of skipping so the
        # cue never fires on a half-provisioned device.
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
        # Hand the strip back to whatever the resting look should be. The
        # notification_flash never sets _effect_thread etc. (it runs
        # standalone here), so nothing to clear — just repaint.
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

    # Sleep is a terminal visual state. TTS/music teardown and previously
    # scheduled emotion restores may arrive after sleepy cleared the strip;
    # do not let any of them repaint the mic-muted indicator or user state.
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

    # Mic muted → the resting look is the privacy red, not the user state:
    # whatever just finished (emotion, wave, transient) settles back onto it.
    if _mic_muted_led_owns_strip():
        logger.info("LED restore: mic muted -- settling on privacy indicator")
        _start_mic_muted_effect()
        return

    # A realtime turn still waiting on the model owns the strip: repaint the
    # thinking cue the filler's speaking_wave just overwrote.
    if _thinking_cue_active:
        logger.info("LED restore: realtime thinking cue still active -- repainting")
        _apply_emotion_led_display(EMO_THINKING, 0.7, force_led=True)
        return

    from hal.drivers.harness.led import restore as restore_harness_led
    if restore_harness_led():
        return

    state = _user_led_state
    if state is None:
        # A dark resting look means "no user state" settles to black, exactly
        # like an explicit off — otherwise whatever just ran (speaking wave,
        # emotion) freezes on its last frame and the strip stays lit with a
        # colour nobody asked for. Keeping the emotion colour only makes sense
        # when the device is configured to glow at rest.
        if ambient_resting_is_dark():
            _stop_current_effect()
            rgb_service.clear()
            logger.info("LED restore: no user state -- resting dark, cleared")
            return
        logger.info("LED restore: no active user state -- keeping emotion color")
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
            # Saved before rainbow had a level: old state files have no
            # "brightness", and 1.0 is what they were painted at.
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
            # LED only. This used to re-aim the head to the scene direction as
            # well, on every one of the six paths that restore the LED — after
            # almost every emotion, at each TTS end, on mic unmute, 3s after an
            # STT session opens. `aim_servo` honours nothing but the sleep lock,
            # so each one killed the running recording mid-frame and parked the
            # arm as `__aim_hold__` for 5s, at a pose blended from wherever the
            # animation happened to be (#314). Scene ACTIVATION still aims —
            # that is a deliberate user action, and it is the only one that is.
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
    """Clear a stale voice-listening cue without leaving the idle LED active.

    A rejected wake-word turn never starts TTS, so it cannot rely on the normal
    TTS-complete restore. Only clear the cue when it still owns the visual state;
    another concurrent emotion must remain untouched.
    """
    global _current_emotion
    if _current_emotion != EMO_LISTENING:
        return False
    _current_emotion = EMO_IDLE
    _restore_user_led()
    return True


def show_listening_pending_cue() -> Optional[int]:
    """Show a dim, LED-only acknowledgement while gaze-authorized STT starts.

    This deliberately does not claim ``_current_emotion``: it must not freeze
    the body like the real ``listening`` emotion does. The returned token lets
    the owning STT session clear only its own cue if it ends before a partial.
    """
    global _listening_pending_cue_id, _listening_pending_cue_active_id
    if _sleeping or _tts_speaking or _current_emotion not in (None, EMO_IDLE):
        return None
    with _listening_pending_cue_lock:
        _listening_pending_cue_id += 1
        cue_id = _listening_pending_cue_id
        _listening_pending_cue_active_id = cue_id

    # Reuse the established listening hue/effect at a deliberately restrained
    # level. Calling the LED helper directly keeps this out of the public
    # emotion API and avoids the servo halt used by EMO_LISTENING.
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

    # A real emotion/TTS may already own the LED. Restore only when this cue
    # was still the transient overlay visible to the user.
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
    logger.info("TTS speaking LED start: color=%s", color)

    _tts_speaking = True

    if _restore_timer is not None and _restore_timer.is_alive():
        _restore_timer.cancel()
        _restore_timer = None

    _stop_current_effect()
    # DISABLED 2026-05-26: black-flash before speaking_wave caused visible "LED off"
    # blip (50-200ms) every TTS start. NeoPixel is stateless — new dispatch overwrites
    # directly. Re-enable if residual pixels from old effect's last frame become visible.
    # rgb_service.dispatch(RGB_CMD_SOLID, (0, 0, 0))

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
    """End the `thinking` face when the reply that answered it finishes speaking.

    `thinking` is set at the start of a wait and has no natural end: only the
    emotion the reply expresses replaces it. A turn whose reply carries no
    emotion marker — common for delegated turns answered by the main agent —
    therefore leaves the face (and, through `_thinking_cue_active`, every later
    LED restore) on the pulse long after the device finished talking. Speaking
    the reply IS the end of the wait, so use it as the signal.

    Only genuine agent replies count: `realtime_feedback` is set exclusively by
    the runtime's own output, so dead-air fillers, mumble and system notices —
    the TTS that plays *during* the wait, which the cue exists to survive — are
    skipped. The caller restores the user LED state right after, and dropping
    the flag first is what lets that restore settle instead of repainting the
    pulse.
    """
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

    # DISABLED 2026-05-26: black-flash before restore caused visible "LED off" blip
    # at TTS end. See _on_tts_speak_start note.
    # if rgb_service:
    #     rgb_service.dispatch(RGB_CMD_SOLID, (0, 0, 0))

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
    # DISABLED 2026-05-26: black-flash before music wave caused visible "LED off" blip
    # at music start. See _on_tts_speak_start note.
    # rgb_service.dispatch(RGB_CMD_SOLID, (0, 0, 0))

    _effect_stop.clear()
    _effect_name = effect
    _effect_base_color = color
    _effect_thread = threading.Thread(
        target=_run_effect,
        args=(effect, color, 2.5, None, _effect_stop, rgb_service),
        # speaking_wave_rainbow generates its own hue, so no color scales it —
        # its level comes from the music_strong preset's "brightness", the one
        # per-device knob for "how bright is the rainbow during music". The
        # plain speaking_wave branch ignores this: it is scaled by the user's
        # own color, which the device palette already governs.
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

    # DISABLED 2026-05-26: black-flash before restore caused visible "LED off" blip
    # at music end. See _on_tts_speak_start note.
    # if rgb_service:
    #     rgb_service.dispatch(RGB_CMD_SOLID, (0, 0, 0))

    _restore_user_led()


def _apply_emotion_led_display(
    emotion: str, intensity: float = 1.0, force_led: bool = False
) -> Optional[list]:
    """Apply LED effect + display expression for an emotion. Returns scaled LED color or None.

    force_led bypasses ONLY the background-emotion guard below (user saved
    color wins over idle/thinking). The realtime voice turn uses it for its
    thinking cue: a deliberate, once-per-turn, always-cleared overlay — unlike
    the per-message agent-hook thinking spam the guard was built against.
    The user-LED-off and TTS-speaking guards still apply."""
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
    # ADDED 2026-05-26: generalize the idle skip to all background emotions.
    # emotion-acknowledge hook fires `thinking` on every preprocessed message;
    # without this guard, thinking's purple pulse overrides user's ambient
    # color every turn. Original idle-only check kept its behavior unchanged
    # (idle is in _BACKGROUND_EMOTIONS). Re-narrow this set if a background
    # emotion needs LED feedback again.
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
                # Emotion-driven effects run on a black base, not the user's
                # ambient color: the agent is expressing a feeling and the
                # user should see it clearly. Overlay-on-user is reserved
                # for transient driver effects (e.g. Buddy busy pulse) via
                # the /led/effect transient=true path.
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
                    # rainbow has no color to scale, so `intensity` cannot dim
                    # it — the preset's own `brightness` is its only level.
                    # start_at_peak is opt-in per preset (listening only): a
                    # cue that answers the user has to be visible on its first
                    # frame, not after the breath has risen. See effects.py.
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
            led_color = scaled
            if sensing_service:
                sensing_service.presence.set_last_color(tuple(scaled))
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
    # Guard against sleepy-emotion / scene-change turning the camera off
    # mid-tracking. Tracker needs the frame stream, so any auto-off
    # triggered while tracking is active must be ignored.
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
    # No explicit identity name → use the device type (lamp/dog/intern) so an unnamed
    # device is addressed by its class instead of a hardcoded "lamp".
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
    """Names STT must not mangle: everything that can open the wake-word gate.

    STT decides whether a turn is heard at all, and it mis-hears proper nouns it
    has no reason to expect — "hi lamp" came back as "hi lance", "hello rachel"
    as "hello risa", and each miss silently drops the whole turn. Boosting only
    the agent name is not enough: the device type and the permanent "autonomous"
    alias arm the same gate (see _build_wake_words and the DEFAULT_WAKE_WORDS
    the voice service merges in).

    Returned in Deepgram's `keyword:intensifier` form. The Flux and nova-3 paths
    strip the weight — their `keyterm` parameter takes plain terms. Duplicates
    are dropped so an unnamed device, whose agent name falls back to the device
    type, does not boost the same word twice.
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
