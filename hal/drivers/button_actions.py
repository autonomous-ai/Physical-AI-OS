"""Shared button/touch actions."""

import logging
import random
import subprocess
import threading
import time

import requests

import hal.app_state as state
from hal.i18n import (
    HEAD_PAT_PHRASES_BY_LANG,
    MIC_MUTED_PHRASES_BY_LANG,
    MIC_UNMUTED_PHRASES_BY_LANG,
    PHRASE_LISTENING,
    PHRASE_REBOOT,
    PHRASE_SLEEP,
    PHRASE_SHUTDOWN,
    PHRASES_BY_LANG,
)
from hal.presets import DEFAULT_LANG, normalize_language
from hal.drivers.button_gestures import (
    DOUBLE_CLICK_WINDOW,
    FACTORY_RESET_DURATION,
    LONG_PRESS_DURATION,
    SLEEP_HOLD_DURATION,
)

logger = logging.getLogger(__name__)

OS_SENSING_URL = "http://127.0.0.1:5000/api/sensing/event"

OS_SPEECH_CANCEL_URL = "http://127.0.0.1:5000/api/agent/speech/cancel"

HEAD_PAT_NOTIFY_WINDOW_S = 60.0
_head_pat_lock = threading.Lock()
_head_pat_last_notify_ts: float = 0.0
_head_pat_suppressed: int = 0


def _notify_head_pat(spoken: str):
    """Tell the OS server that the device was just stroked. Called from the head-pat TTS
    thread *after* speak_cached actually played a phrase.
    """
    global _head_pat_last_notify_ts, _head_pat_suppressed
    with _head_pat_lock:
        now = time.monotonic()
        if (now - _head_pat_last_notify_ts) < HEAD_PAT_NOTIFY_WINDOW_S:
            _head_pat_suppressed += 1
            return
        batched = _head_pat_suppressed
        _head_pat_suppressed = 0
        _head_pat_last_notify_ts = now
    message = f'You were petted and responded: "{spoken}"'
    if batched:
        message += f" (and {batched} more pat(s) since the last report)"
    try:
        requests.post(
            OS_SENSING_URL,
            json={"type": "touch.head_pat", "message": message},
            timeout=0.5,
        )
    except Exception:
        pass


def _cancel_agent_speech(source: str):
    """Tell the OS server to stop speaking for every turn currently in flight.

    Fire-and-forget on its own thread: the click's felt latency is the whole point of
    the gesture (see the sca-trace timings below), and a stalled OS server must not
    delay the local stop.
    """

    try:
        from hal.telemetry import voice_metrics

        voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)
    except Exception:
        logger.exception("[voice-metrics] stop boundary hook failed")

    def _post():
        try:
            requests.post(OS_SPEECH_CANCEL_URL, json={}, timeout=1.0)
        except Exception as e:
            logger.warning("%s speech-cancel call failed: %s", source, e)

    threading.Thread(target=_post, daemon=True, name=f"{source}-speech-cancel").start()


def _current_lang() -> str:
    try:
        from hal.config import _os_cfg_get
        return normalize_language(_os_cfg_get("stt_language"))
    except Exception:
        return ""


def _phrase(key: str) -> str:
    """Return the localized phrase for `key` based on the device's stt_language."""
    pool = PHRASES_BY_LANG.get(key, {})
    return pool.get(_current_lang()) or pool.get(DEFAULT_LANG, "")


def _random_from(pools: dict) -> str:
    """Pick a random phrase for the current language from a per-language pool."""
    pool = pools.get(_current_lang()) or pools.get(DEFAULT_LANG, [])
    return random.choice(pool) if pool else ""


def _random_head_pat_phrase() -> str:
    """Pick a random pet-response phrase for the current language."""
    return _random_from(HEAD_PAT_PHRASES_BY_LANG)


def _announce_listening(capture_state=None):
    """Speak the localized listening cue, preempting any in-flight TTS. speak_cached() uses
    a non-blocking acquire — if the service is busy and the current speech wasn't marked
    interruptible, the cue is silently dropped.
    """
    text = _phrase(PHRASE_LISTENING)
    if capture_state is None:
        capture_state = getattr(state.tts_service, "input_capture_state", (False, 0))
    if capture_state[0]:
        logger.info("listening cue dropped -- user capture already active")
        return
    prepare = getattr(state.tts_service, "prepare_listening_cue", None)
    if prepare is not None:
        if not prepare(capture_state):
            return
    else:
        state.tts_service.stop()
    # First attempt is immediate: when TTS is idle (the common case — mic unmute path)
    # the cue plays with zero added delay. Backoff only kicks in when the lock is still
    # held by winding-down playback.
    for delay in (0, 0.15, 0.4, 0.8, 1.6, 3.0):
        if delay:
            time.sleep(delay)
        if getattr(state.tts_service, "input_capture_state", (False, 0)) != capture_state:
            logger.info("listening cue dropped -- user started speaking during retry")
            return
        if state.tts_service.speak_cached(text, interruptible=True):
            return
    logger.warning("listening cue dropped: TTS busy after retries")


def _tts_available() -> bool:
    return bool(
        state.tts_service
        and state.tts_service.available
        and not state._speaker_muted
    )


def _wake_if_sleepy(source: str):
    """If the device is currently sleeping, fire a stretching wake emotion so a click pulls
    her out of sleep before the listening cue lands.
    """
    if not state._sleeping:
        return
    logger.info("%s -- waking from sleep", source)
    try:
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion
        express_emotion(EmotionRequest(emotion="stretching"), source=source)
    except Exception as e:
        logger.warning("Wake emotion call failed: %s", e)


def _speak_gesture_ack(text: str, source: str):
    """Speak a short confirmation for a resolved physical gesture, off-thread.

    Non-interrupting by design — a gesture ack must never truncate a reply the user is
    listening to.
    """
    if not text or not _tts_available():
        return
    threading.Thread(
        target=lambda: state.tts_service.speak_cached(text, interruptible=True),
        daemon=True,
        name=f"{source}-gesture-ack",
    ).start()


def play_ack_chime(source: str = "button"):
    """Instant audible acknowledgment (~120ms ping) that a physical gesture registered.

    spoken cue can never get there (it waits out gesture disambiguation windows), the
    chime can.
    """
    tts = state.tts_service
    if tts is None:
        return
    try:
        tts.play_ack_chime()
    except Exception as e:
        logger.debug("%s ack chime failed: %s", source, e)


def play_pet_chime(source: str = "TTP223"):
    """A soft tactile cue for head petting, without interrupting speech."""
    tts = state.tts_service
    if tts is None:
        return
    try:
        tts.play_pet_chime()
    except Exception as e:
        logger.debug("%s pet chime failed: %s", source, e)


def announce_listening_cue(source: str = "button"):
    """Keep gesture callers intact while the spoken listening cue is disabled."""
    # Same HW kill-switch guard as single_click_action. Guarding only
    # single_click_action leaves the GPIO-button path leaky.
    if state._hw_mic_switch_muted is True:
        logger.info("%s listening cue skipped -- HW mic switch is off", source)
        return
    # Tap/wake latency experiment: the spoken cue sets TTS.speaking, which makes
    # VAD discard the user's first words. Keep the original call for rollback;
    # the existing short ack chime does not set TTS.speaking.
    # if _tts_available():
    #     threading.Thread(
    #         target=_announce_listening,
    #         args=(getattr(state.tts_service, "input_capture_state", (False, 0)),),
    #         daemon=True,
    #         name=f"{source}-single-click-tts",
    #     ).start()
    logger.info("%s listening TTS disabled -- keeping the short tap chime", source)


def _stop_active_tracking(source: str):
    """Stop object tracking when a single click asks the device for attention."""
    try:
        from hal.drivers.motors.range_demo import request_abort as _abort_demo
        from hal.drivers.tracking.aim import request_abort as _abort_aim
        from hal.drivers.tracking.search import request_abort as _abort_search

        _abort_aim()
        _abort_search()
        _abort_demo()
    except Exception as e:
        logger.debug("%s single click -- aim/search/demo abort unavailable: %s", source, e)

    tracker = state.tracker_service
    if not tracker or not tracker.is_tracking:
        return
    try:
        logger.info("%s single click -- stopping object tracking", source)
        tracker.stop()
    except Exception as e:
        # A tracker failure must not block the click's microphone/speaker action.
        logger.warning("%s single click -- failed to stop object tracking: %s", source, e)


def _grant_wakeword_focus(source: str):
    """Let a single click stand in for the wake phrase."""
    voice = state.voice_service
    if not voice:
        return
    try:
        voice.grant_wakeword_focus(source)
    except Exception as e:
        # Never let the focus grant block the rest of the click.
        logger.warning("%s single click -- wake-word focus grant failed: %s", source, e)


def single_click_action(source: str = "button", announce: bool = True, chime: bool = True,
                        unmute_output: bool = True):
    """Stop active tracking and in-flight speech / unmute mic + speaker."""
    state.note_user_activity(source)
    # Stopping movement is safe even with the hardware mic kill switch off: it
    # does not wake or unmute the microphone, but still lets the user cancel an
    # active follow session with the same direct-attention gesture.
    _stop_active_tracking(source)
    # Hardware mic-mute switch is the authority: while it is physically off, taps on the
    # GPIO button / TTP223 touchpad must NOT wake, unmute, or announce.
    if state._hw_mic_switch_muted is True:
        logger.info("%s single click ignored -- HW mic switch is off", source)
        return

    from hal.routes.music import audio_stop, unmute_speaker
    from hal.routes.voice import stop_tts, unmute_mic

    t_start = time.monotonic()
    # Dispatched first and off-thread so the mute reaches the OS server while the local
    # wake/unmute steps below are still running.
    _cancel_agent_speech(source)
    state.note_music_cancel()
    # Stop music on BOTH branches below. Also kills a play still in its yt-dlp resolve
    # phase, since MusicService.playing stays True while the music thread holds the
    # lock.
    audio_stop()
    _wake_if_sleepy(source)
    logger.info("[sca-trace] wake done +%.0fms", (time.monotonic() - t_start) * 1000)

    # A single click is a "give me the floor" gesture, so relax a user/scene speaker
    # mute too — otherwise the listening cue stays silent and the reply the user just
    # asked for would be inaudible.
    if unmute_output and state._speaker_muted and not state._enrolling:
        logger.info("%s single click -- unmuting speaker", source)
        t = time.monotonic()
        unmute_speaker()
        logger.info("[sca-trace] unmute_speaker done +%.0fms", (time.monotonic() - t) * 1000)

    if state._mic_muted:
        # Hardware physical switch/button is the source of truth for mute state: a click
        # unmutes regardless of who set the mute (web UI, API, touchpad double tap,
        # sleep auto-mute).
        logger.info("%s single click -- unmuting mic", source)
        t = time.monotonic()
        unmute_mic()
        logger.info("[sca-trace] unmute_mic done +%.0fms", (time.monotonic() - t) * 1000)
    else:
        logger.info("%s single click -- stopping speaker", source)
        stop_tts()
    _grant_wakeword_focus(source)
    # Ack ping AFTER the stop: stop_tts frees the persistent stream lock
    # within ~10ms, so the chime sounds effectively at gesture time.
    if chime:
        t = time.monotonic()
        play_ack_chime(source)
        logger.info("[sca-trace] chime done +%.0fms", (time.monotonic() - t) * 1000)
    # Announce the listening cue so the user hears confirmation of the click — both for
    # unmute (mic just opened) and for stop-speaker (the device was talking, user wants
    # the floor).
    if announce:
        t = time.monotonic()
        announce_listening_cue(source)
        logger.info("[sca-trace] announce_listening_cue dispatched +%.0fms (total_sca=%.0fms)", (time.monotonic() - t) * 1000, (time.monotonic() - t_start) * 1000)


def reboot_os():
    """Start the raw operating-system reboot command."""
    subprocess.Popen(
        ["sudo", "reboot"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def reboot_action(source: str = "button"):
    """Announce a reboot, then restart the operating system."""
    logger.info("%s reboot action", source)
    if _tts_available():
        state.tts_service.speak_cached(_phrase(PHRASE_REBOOT))
        time.sleep(5)
    reboot_os()


def triple_click_action(source: str = "button"):
    """Map the resolved physical triple-click gesture to a reboot action."""
    logger.info("%s triple click -- reboot armed", source)
    reboot_action(source)


def head_pat_action(source: str = "touch"):
    """Speak a random pet response."""
    state.note_user_activity(source)
    text = _random_head_pat_phrase()
    logger.info("%s head pat -- %r", source, text)
    if not _tts_available() or not text:
        return

    def _speak_then_notify():
        if state.tts_service.speak_cached(text):
            _notify_head_pat(text)

    threading.Thread(
        target=_speak_then_notify,
        daemon=True,
        name=f"{source}-head-pat-tts",
    ).start()


def swipe_action(source: str = "touch"):
    """Map a resolved touchpad swipe to sleep. Always sleep — one meaning.

    Shutdown / reboot / factory-reset stay on the mechanical button, because FastMode
    cannot measure a hold and a mis-detected swipe must never be able to strand the
    device.
    """
    logger.info("%s swipe -- sleeping", source)
    sleep_action(source)


def mic_toggle_action(source: str = "touch"):
    """Map a resolved double tap to the mic mute toggle."""
    state.note_user_activity(source)
    if state._hw_mic_switch_muted is True:
        logger.info("%s double tap ignored -- HW mic switch is off", source)
        return
    if state._enrolling:
        logger.info("%s double tap ignored -- voice enrollment in progress", source)
        return

    from hal.routes.voice import mute_mic, unmute_mic

    try:
        if state._mic_muted:
            logger.info("%s double tap -- unmuting mic", source)
            unmute_mic()
            pool = MIC_UNMUTED_PHRASES_BY_LANG
        else:
            logger.info("%s double tap -- muting mic", source)
            mute_mic()
            pool = MIC_MUTED_PHRASES_BY_LANG
    except Exception as e:
        # Never let a mute failure escape into the lgpio callback path.
        logger.warning("%s double tap -- mic toggle failed: %s", source, e)
        return

    # Speak the resulting STATE, after the flip, so the voice and the LED agree.
    # Off-thread and non-interrupting, like head_pat_action: this must not add latency
    # to the mute itself, and it must not talk over a reply in flight.
    _speak_gesture_ack(_random_from(pool), source)


def sleep_action(source: str = "button"):
    """Announce sleep, then enter sleepy mode through the normal pipeline."""
    if state._sleeping:
        logger.info("%s sleep hold -- already sleeping", source)
        return
    from hal.routes.emotion import harness_blocks_sleep
    if harness_blocks_sleep():
        logger.info("%s sleep hold -- ignored, Harness is on or unavailable", source)
        return

    logger.info("%s sleep hold -- announcing sleepy emotion", source)
    if _tts_available():
        state.tts_service.speak_cached(_phrase(PHRASE_SLEEP))
        time.sleep(5)

    try:
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        # Reuse /emotion so sleep keeps one authoritative implementation for
        # servo animation/release, LED off, camera off, and audio mute -- and,
        # with `source`, one authoritative record of who asked for it.
        express_emotion(EmotionRequest(emotion="sleepy"), source=source)
    except Exception as e:
        logger.warning("%s sleep hold failed: %s", source, e)


def shutdown_os():
    """Start the raw operating-system shutdown command."""
    subprocess.Popen(
        ["sudo", "shutdown", "-h", "now"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def shutdown_action(source: str = "button"):
    """Announce, release servos, then shut down the operating system."""
    logger.info("%s shutdown action", source)

    state._shutdown_announced = True

    if _tts_available():
        state.tts_service.speak_cached(_phrase(PHRASE_SHUTDOWN))
        time.sleep(5)

    # Step 2: park servo in safe pose then cut torque, otherwise the
    # body slams down when systemd kills the process mid-pose.
    try:
        from hal.routes.servo import release_servos

        logger.info("%s shutdown action -- releasing servo before shutdown", source)
        release_servos()
    except Exception as e:
        logger.warning(f"Servo release before shutdown failed: {e}")

    shutdown_os()


def hold_release_action(held_s: float, source: str = "button", *, factory_reset: bool = True):
    """Map a released hold duration to its explicit device action.

    Inputs that must not factory-reset (MPR121) pass factory_reset=False and keep any
    longer hold at shutdown.
    """
    if factory_reset and held_s >= FACTORY_RESET_DURATION:
        factory_reset_action(source)
    elif held_s >= LONG_PRESS_DURATION:
        shutdown_action(source)
    elif held_s >= SLEEP_HOLD_DURATION:
        sleep_action(source)


def button_hold_tier(held_s, *, behavior="standard", hold_s=5.0, factory_reset=True):
    """Select shared feedback for normal and dedicated reset buttons."""
    if behavior == "factory_reset":
        return 3 if held_s >= hold_s else 0
    return (3 if factory_reset and held_s >= FACTORY_RESET_DURATION else
            2 if held_s >= LONG_PRESS_DURATION else
            1 if held_s >= SLEEP_HOLD_DURATION else 0)


def button_hold_release_action(held_s, feedback, *, behavior="standard", hold_s=5.0,
                               source="button", factory_reset=True):
    """Commit the actual policy's LED and action using a released duration."""
    if not button_hold_tier(held_s, behavior=behavior, hold_s=hold_s, factory_reset=factory_reset):
        return
    if behavior == "factory_reset":
        if feedback.commit_tier(3) is not False:
            factory_reset_action(source)
    elif feedback.commit(held_s, factory_reset=factory_reset) is not False:
        hold_release_action(held_s, source=source, factory_reset=factory_reset)


def _factory_reset_phrase() -> str:
    """Inline i18n until PHRASE_FACTORY_RESET lands in i18n.py."""
    lang = _current_lang()
    if lang.startswith("ja"):
        return "工場出荷時の設定に戻します。再起動します。"
    if lang.startswith("vi"):
        return "Đang khôi phục cài đặt gốc. Đang khởi động lại."
    if lang.startswith("zh"):
        return "正在恢复出厂设置，即将重新启动。"
    return "Factory reset starting. Rebooting now."


def factory_reset_action(source: str = "button"):
    """Announce + POST /api/system/factory-reset on the OS server."""
    logger.info("%s factory-reset hold -- triggering soft reset", source)
    logger.info("%s LED: red solid (factory-reset armed)", source)

    state._shutdown_announced = True

    if _tts_available():
        state.tts_service.speak_cached(_factory_reset_phrase())
        time.sleep(3)

    # Step 2: park servo before reboot, same reasoning as shutdown_action —
    # systemd will kill us mid-pose otherwise and the body slams.
    try:
        from hal.routes.servo import release_servos

        release_servos()
    except Exception as e:
        logger.warning(f"Servo release before factory-reset failed: {e}")

    # Step 3: trigger the Go-side wipe. Loopback bypasses admin auth (see
    # os-server server.go adminOrLoopbackAuth) so this works even on devices that
    # never completed setup (no llm_api_key in config).
    try:
        requests.post(
            "http://127.0.0.1:5000/api/system/factory-reset",
            json={},
            timeout=3.0,
        )
    except Exception as e:
        logger.error("factory-reset HTTP call failed: %s", e)


# Hold LED feedback shared by GPIO and capacitive button inputs.
LED_OFF = (0, 0, 0)
LED_BLINK_HALF_PERIOD_S = 0.25


def warn_color(tier):
    """Read the live device-overridden preset on every dispatch."""
    from hal.presets import BUTTON_LED_PRESETS

    return tuple(BUTTON_LED_PRESETS[tier]["color"])


def dispatch_led(color, *, still_current=None):
    """Preempt emotion effects exactly as the GPIO button does."""
    import hal.app_state as state
    from hal.drivers.base import Priority
    from hal.presets import RGB_CMD_SOLID

    rgb = state.rgb_service
    if rgb is None:
        return
    try:
        state._stop_current_effect()
        if still_current is None or still_current():
            rgb.dispatch(RGB_CMD_SOLID, color, priority=Priority.HIGH)
    except Exception:
        logger.warning("Button LED dispatch failed", exc_info=True)


class HoldLEDFeedback:
    """Consume accepted hold tiers without blocking the hardware poll loop."""

    def __init__(self):
        self._condition = threading.Condition()
        self._thread = None
        self._closed = False
        self._generation = 0
        self._completed = 0
        self._tier = 0
        self._committing = False

    def _request(self, tier, committing=False):
        with self._condition:
            if self._closed:
                return None
            self._generation += 1
            self._tier = tier
            self._committing = committing
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, daemon=True, name="button-hold-led",
                )
                self._thread.start()
            self._condition.notify_all()
            return self._generation

    def set_tier(self, tier):
        self._request(tier)

    def release(self):
        with self._condition:
            if self._thread is None:
                return
            self._request(0)

    def commit(self, held_s, *, factory_reset=True):
        tier = 3 if factory_reset and held_s >= FACTORY_RESET_DURATION else 2 if held_s >= LONG_PRESS_DURATION else 0
        return self.commit_tier(tier)

    def commit_tier(self, tier):
        """Commit a semantic tier without inventing a hold duration."""
        generation = self._request(tier, committing=True)
        if generation is None:
            return False
        with self._condition:
            self._condition.wait_for(
                lambda: self._closed or self._completed >= generation,
            )

            return not self._closed and self._generation == generation

    def _is_current(self, generation):
        with self._condition:
            return not self._closed and self._generation == generation

    def stop(self):
        # Do not join here: poll cleanup must never wait for an RGB effect.
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _run(self):
        generation = -1
        blink_on = False
        while True:
            with self._condition:
                if self._closed:
                    return
                changed = generation != self._generation
                if changed:
                    generation = self._generation
                    blink_on = False
                tier, committing = self._tier, self._committing
            try:
                if tier:
                    name = {1: "sleep_warn", 2: "shutdown_warn", 3: "factory_reset"}[tier]
                    blink_on = not blink_on
                    color = warn_color(name)
                    dispatch_led(
                        color if committing or tier == 3 or blink_on else LED_OFF,
                        still_current=lambda: self._is_current(generation),
                    )
            except Exception:
                # Missing RGB/presets must never prevent the semantic action.
                logger.warning("Button hold LED feedback failed", exc_info=True)
            with self._condition:
                self._completed = max(self._completed, generation)
                self._condition.notify_all()
                if self._closed:
                    return
                timeout = LED_BLINK_HALF_PERIOD_S if tier in (1, 2) and not committing else None
                self._condition.wait_for(
                    lambda: self._closed or self._generation != generation,
                    timeout=timeout,
                )
