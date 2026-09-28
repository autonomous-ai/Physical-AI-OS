"""Shared button/touch actions.

Reused by any input device that maps to the same three gestures:
- single_click_action(): stop object tracking and speaker / unmute mic + speaker + announce listening
- triple_click_action(): map a resolved click gesture to reboot_action()
- hold_release_action(): map a resolved hold duration to sleep / shutdown / factory reset

Callers (GPIO button, touchpad, future remotes) only need to detect the
gesture and invoke the matching function — the destructive sequencing
(TTS announce → servo park → shutdown/reboot) lives here so every input
path gets the same safe behavior.
"""

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
from hal.presets import DEFAULT_LANG
from hal.drivers.button_gestures import (
    DOUBLE_CLICK_WINDOW,
    FACTORY_RESET_DURATION,
    LONG_PRESS_DURATION,
    SLEEP_HOLD_DURATION,
)

logger = logging.getLogger(__name__)

# OS server sensing endpoint. Head-pat notify is fire-and-forget — the
# OS server appends a NO_REPLY hint so the agent records the event in
# conversation history without speaking back.
OS_SENSING_URL = "http://127.0.0.1:5000/api/sensing/event"

# OS server speech-cancel endpoint. stop_tts() only silences what HAL already
# holds (the sentence playing plus the pre-synthesised queue); the OS server
# keeps handing over the sentences of every turn still in flight, so without
# this call the device goes quiet for one sentence and then talks on. The OS
# server marks those turns as no-longer-allowed-to-speak — they keep running,
# they just lose the speaker. Turns started AFTER the click are unaffected, so
# the user can click and immediately say something new.
OS_SPEECH_CANCEL_URL = "http://127.0.0.1:5000/api/agent/speech/cancel"

# Agent-notify batching for petting. Every notify is a full LLM turn on the
# OS server side (the NO_REPLY hint suppresses speech, not the turn), and the
# local phrase playback alone would allow one notify every ~2-4s during a
# sustained petting session. One turn per window carries the same
# information; extra pats ride along as a count on the next notify.
HEAD_PAT_NOTIFY_WINDOW_S = 60.0
_head_pat_lock = threading.Lock()
_head_pat_last_notify_ts: float = 0.0
_head_pat_suppressed: int = 0


def _notify_head_pat(spoken: str):
    """Tell the OS server that the device was just stroked. Called from the
    head-pat TTS thread *after* speak_cached actually played a phrase.
    TTS-busy strokes are dropped silently and never notify, which is the
    right behaviour: the agent only learns about petting moments the user
    actually heard a response to. At most one notify per
    HEAD_PAT_NOTIFY_WINDOW_S — pats in between are batched into a count.

    `spoken` is the exact phrase the agent just said (incl. eleven_v3 audio
    tags like [laughs] / [whispers]) so the agent can read its own tone
    and weave it into memory — "I laughed and said tickles" lands
    differently than "I sighed and asked them to stop"."""
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

    Fire-and-forget on its own thread: the click's felt latency is the whole
    point of the gesture (see the sca-trace timings below), and a stalled OS
    server must not delay the local stop. The local stop_tts() runs anyway, so
    a lost call degrades to "quiet for one sentence" rather than to nothing."""

    # Voice metrics: the explicit stop boundary (different semantics from the
    # automatic supersession one — see hal/telemetry/voice_metrics.py).
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
        return (_os_cfg_get("stt_language") or "").strip()
    except Exception:
        return ""


def _phrase(key: str) -> str:
    """Return the localized phrase for `key` based on the device's stt_language.
    Falls back to DEFAULT_LANG when the config can't be read or the
    language is empty/unknown."""
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
    """Speak the localized listening cue, preempting any in-flight TTS.
    speak_cached() uses a non-blocking acquire — if the service is busy
    and the current speech wasn't marked interruptible, the cue is
    silently dropped. stop() flips stop_event but only the playback loop
    checks it; if the previous speech is in the render phase (live TTS
    round-trip, 2-5s), the lock won't free until render + short play
    break finish. Retry with backoff so the cue lands as soon as the
    lock releases. ~6s total cap covers a worst-case fresh render before
    giving up silently."""
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
    # First attempt is immediate: when TTS is idle (the common case — mic
    # unmute path) the cue plays with zero added delay. Backoff only kicks
    # in when the lock is still held by winding-down playback.
    # interruptible=True so any follow-up speech (agent reply, gesture
    # announce) preempts a stale cue instead of being busy-skipped.
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
    """If the device is currently sleeping, fire a stretching wake emotion so a
    click pulls her out of sleep before the listening cue lands. Calls
    the /emotion handler in-process — it clears `_sleeping`, cancels the
    sleepy auto-release timer, plays the wake animation, and auto-deactivates
    any active scene (e.g. Night mode).

    Called from single_click_action and from swipe_action, so the log line says
    which gesture woke her rather than assuming a click."""
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

    Non-interrupting by design — a gesture ack must never truncate a reply the
    user is listening to. Silent no-op when TTS is unavailable or the speaker is
    muted, which is also correct: a muted speaker means the user chose silence.
    """
    if not text or not _tts_available():
        return
    threading.Thread(
        target=lambda: state.tts_service.speak_cached(text, interruptible=True),
        daemon=True,
        name=f"{source}-gesture-ack",
    ).start()


def play_ack_chime(source: str = "button"):
    """Instant audible acknowledgment (~120ms ping) that a physical gesture
    registered. Humans need sub-200ms feedback to feel 'it heard me' — the
    spoken cue can never get there (it waits out gesture disambiguation
    windows), the chime can. Neutral by design: valid ack for a tap, the
    first stroke of a pet, or the start of a triple-click burst. Silent
    no-op when TTS is unavailable or the speaker is muted."""
    tts = state.tts_service
    if tts is None:
        return
    try:
        tts.play_ack_chime()
    except Exception as e:
        logger.debug("%s ack chime failed: %s", source, e)


def announce_listening_cue(source: str = "button"):
    """Fire the listening-cue TTS off-thread. Split from single_click_action
    so callers that resolve gestures in two steps (GPIO button: floor-grab
    on release, cue after the click window) can defer just the audible part
    — a cue talking over the user mid-triple-click disrupts their rhythm."""
    # Same HW kill-switch guard as single_click_action: gpio_button.py's
    # _on_click_timeout calls this DIRECTLY (bypassing single_click_action)
    # after the click window closes, so the guard has to live here too or
    # "I'm listening" still fires while the mic is physically off. Guarding
    # only single_click_action leaves the GPIO-button path leaky.
    if state._hw_mic_switch_muted is True:
        logger.info("%s listening cue skipped -- HW mic switch is off", source)
        return
    if _tts_available():
        threading.Thread(
            target=_announce_listening,
            args=(getattr(state.tts_service, "input_capture_state", (False, 0)),),
            daemon=True,
            name=f"{source}-single-click-tts",
        ).start()


def _stop_active_tracking(source: str):
    """Stop object tracking when a single click asks the device for attention."""
    # A look-aim moves the head without going through TrackerService, so the
    # is_tracking guard below would miss it — the user would press the button to
    # stop the lamp moving and it would keep turning. Abort it unconditionally.
    try:
        from hal.drivers.motors.range_demo import request_abort as _abort_demo
        from hal.drivers.tracking.aim import request_abort as _abort_aim
        from hal.drivers.tracking.search import request_abort as _abort_search

        _abort_aim()
        _abort_search()
        # The range demo moves the body AND narrates it, so a click that only
        # stopped the arm would leave the lamp describing legs it is no longer
        # performing.
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
    """Let a single click stand in for the wake phrase.

    With wake word enabled, an utterance is only dispatched to the agent when
    it starts with the wake phrase or falls inside the follow-up window. The
    click already announced "I'm listening", so it has to open that window
    itself — otherwise the user answers the cue and nothing happens. No-op
    when wake word is disabled: every utterance dispatches already."""
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
    """Stop active tracking and in-flight speech / unmute mic + speaker.

    Then open the wake-word window (if wake word is on) and announce the
    listening cue.
    announce=False skips the cue (caller fires announce_listening_cue later).
    chime=False skips the ack ping (caller already chimed at gesture start).
    unmute_output=False leaves speaker restoration to the privacy switch."""
    # A touch proves someone is at the device, even when the gesture itself is
    # refused below. Runs before the wake: presence only records the moment and
    # leaves the strip to sleep until the wake emotion takes over.
    state.note_user_activity(source)
    # Stopping movement is safe even with the hardware mic kill switch off: it
    # does not wake or unmute the microphone, but still lets the user cancel an
    # active follow session with the same direct-attention gesture.
    _stop_active_tracking(source)
    # Hardware mic-mute switch is the authority: while it is physically off,
    # taps on the GPIO button / TTP223 touchpad must NOT wake, unmute, or
    # announce — the whole gesture flow would violate the kill-switch promise.
    # Skip silently (no chime, no cue): the red mic-muted LED is already the
    # visual "off" indicator; a chime here would read as "action accepted"
    # when nothing happened. privacy_button.py's own unmute-path call flips the
    # flag to False BEFORE calling this, so the slide switch's own unmute is
    # not blocked. None = device has no HW switch (Lamp) → always fall through.
    if state._hw_mic_switch_muted is True:
        logger.info("%s single click ignored -- HW mic switch is off", source)
        return

    from hal.routes.music import audio_stop, unmute_speaker
    from hal.routes.voice import stop_tts, unmute_mic

    t_start = time.monotonic()
    # Dispatched first and off-thread so the mute reaches the OS server while
    # the local wake/unmute steps below are still running. Fired on both
    # branches, not just the stopping-speaker one: a click that unmutes the mic
    # is the user taking the floor too, and a backlog of turns queued up while
    # the mic was muted must not start talking over them.
    _cancel_agent_speech(source)
    # Stamp the music cancel watermark BEFORE audio_stop(): the cancelled turn
    # keeps running server-side and its pending music tool call can land on
    # /audio/play right after this, which a bare stop cannot beat. Stamping
    # first closes that window (routes/music.audio_play refuses inside it).
    state.note_music_cancel()
    # Stop music on BOTH branches below. This is the "give me the floor"
    # gesture, and the mic-muted branch used to unmute the mic while leaving
    # music playing — a click that visibly did nothing about the loudest thing
    # in the room. Also kills a play still in its yt-dlp resolve phase, since
    # MusicService.playing stays True while the music thread holds the lock.
    audio_stop()
    _wake_if_sleepy(source)
    logger.info("[sca-trace] wake done +%.0fms", (time.monotonic() - t_start) * 1000)

    # A single click is a "give me the floor" gesture, so relax a user/scene
    # speaker mute too — otherwise the listening cue stays silent and the reply
    # the user just asked for would be inaudible. Skip while a voice enrollment
    # is recording: that mute is a transient guard against TTS bleeding into the
    # captured WAV (see routes/speaker.py record-enroll), not a user preference.
    # Must run before the _tts_available() check below so the cue can play.
    if unmute_output and state._speaker_muted and not state._enrolling:
        logger.info("%s single click -- unmuting speaker", source)
        t = time.monotonic()
        unmute_speaker()
        logger.info("[sca-trace] unmute_speaker done +%.0fms", (time.monotonic() - t) * 1000)

    if state._mic_muted:
        # Hardware physical switch/button is the source of truth for mute state:
        # a click unmutes regardless of who set the mute (web UI, API, touchpad
        # double tap, sleep auto-mute). This keeps the web UI in sync with the
        # physical control — a slide switched back ON matches what the operator
        # expects, and the earlier "leave deliberate mute in place" guard
        # (b79dec4a6, 2026-08-27) caused announce_listening_cue below to fire
        # while the mic actually stayed muted, which the operator reads as a
        # broken toggle.
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
    # Announce the listening cue so the user hears confirmation of the
    # click — both for unmute (mic just opened) and for stop-speaker (the
    # device was talking, user wants the floor). The cue itself preempts
    # in-flight TTS via stop() + speak_cached retry, so calling stop_tts()
    # above is fine — _announce_listening handles the lock handoff.
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
        # speak_cached is async; reboot kicks the OS before audio plays
        # without this. ~5s covers the cached "Rebooting now" clip
        # (matches the shutdown-action delay).
        time.sleep(5)
    reboot_os()


def triple_click_action(source: str = "button"):
    """Map the resolved physical triple-click gesture to a reboot action."""
    logger.info("%s triple click -- reboot armed", source)
    reboot_action(source)


def head_pat_action(source: str = "touch"):
    """Speak a random pet response. Non-interrupting: if TTS is busy
    (the device already talking), drop silently so petting mid-speech doesn't
    truncate her sentence. After the phrase actually plays, ping the OS server
    so the agent records the petting moment (silent — NO_REPLY)."""
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

    Direction is not read, and neither is device state. Both were considered and
    dropped:

    * **Direction** — the surface yields one usable traversal gesture, and
      keying sleep to one direction and wake to the other would make a swipe the
      "wrong" way do nothing, with no feedback saying why. Left-to-right and
      right-to-left are the same gesture here.
    * **State** — an earlier version woke a sleeping device instead of sleeping
      it. That made one gesture mean two things depending on a state the user
      cannot see, and waking is already covered by tap and double tap, which is
      the behaviour the device shipped with. A swipe on a sleeping lamp now
      reaches `sleep_action`, which returns early on "already sleeping".

    Non-destructive by construction: sleep is reversible with a single tap.
    Shutdown / reboot / factory-reset stay on the mechanical button, because
    FastMode cannot measure a hold and a mis-detected swipe must never be able
    to strand the device.
    """
    logger.info("%s swipe -- sleeping", source)
    sleep_action(source)


def mic_toggle_action(source: str = "touch"):
    """Map a resolved double tap to the mic mute toggle.

    Respect the configured hardware switch when present. The mic-muted LED
    reflects the resulting state, and both routes below repaint it themselves.
    """
    state.note_user_activity(source)
    # The HW kill switch is the authority when configured. Devices without one
    # report None and fall through; touch gestures cannot override a muted
    # physical switch. Guarding here as well as in unmute_mic
    # keeps the refusal quiet rather than raising the route's 409.
    if state._hw_mic_switch_muted is True:
        logger.info("%s double tap ignored -- HW mic switch is off", source)
        return
    # A voice enrollment is recording into a WAV. Toggling the mic mid-capture
    # truncates it, and the user gets a silently corrupt enrollment.
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
    # Drawn from a pool in the lamp's own voice rather than one fixed line — the
    # same sentence every time is what reads as a machine. Every line in both
    # pools still says which way the toggle went; see the note in i18n.py.
    # Muting the microphone does not touch the speaker, so this is audible on
    # both legs. Spoken AFTER the state change deliberately: if the toggle
    # raised, the user should not be told about a mute that did not happen.
    #
    # Off-thread and non-interrupting, like head_pat_action: this must not add
    # latency to the mute itself, and it must not talk over a reply in flight.
    # When TTS is busy the confirmation drops and the mic-muted LED carries the
    # feedback alone — the same trade the pet giggle makes.
    _speak_gesture_ack(_random_from(pool), source)


def sleep_action(source: str = "button"):
    """Announce sleep, then enter sleepy mode through the normal pipeline."""
    if state._sleeping:
        logger.info("%s sleep hold -- already sleeping", source)
        return
    from hal.routes.emotion import harness_blocks_sleep
    if harness_blocks_sleep():
        logger.info("%s sleep hold -- ignored, Harness is on", source)
        return

    logger.info("%s sleep hold -- announcing sleepy emotion", source)
    if _tts_available():
        state.tts_service.speak_cached(_phrase(PHRASE_SLEEP))
        # Like reboot/shutdown, allow the cached announcement to play before
        # sleepy mutes the speaker and stops any active TTS.
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

    # Suppress the lifespan-shutdown re-announce — we're about to speak the
    # PHRASE_SHUTDOWN line, and systemd's SIGTERM will arrive a few seconds
    # later. Without this flag, server.py lifespan would say "shutting down"
    # again on top of the cached clip still playing.
    state._shutdown_announced = True

    # Step 1: TTS announce.
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

    # Step 3: shutdown OS.
    shutdown_os()


def hold_release_action(held_s: float, source: str = "button", *, factory_reset: bool = True):
    """Map a released hold duration to its explicit device action.

    Input drivers supply released hold durations. This mapping shares the
    sleep/shutdown/factory-reset decision tree across GPIO and MPR121 inputs.
    Inputs that must not factory-reset (MPR121) pass factory_reset=False and
    keep any longer hold at shutdown.
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
    if lang.startswith("vi"):
        return "Đang khôi phục cài đặt gốc. Đang khởi động lại."
    if lang.startswith("zh"):
        return "正在恢复出厂设置，即将重新启动。"
    return "Factory reset starting. Rebooting now."


def factory_reset_action(source: str = "button"):
    """Announce + POST /api/system/factory-reset on the OS server. The OS server
    wipes per-device state (config, API keys, enrollments, WiFi) and reboots
    into AP setup mode. HAL does NOT touch state itself — single source of
    truth for what gets wiped lives in the OS server's deviceWipePaths.

    Authoritative because of physical presence: a deliberate configured hold + the
    /api/system/factory-reset endpoint allows loopback origin without Bearer
    (see os-server server.go adminOrLoopbackAuth)."""
    logger.info("%s factory-reset hold -- triggering soft reset", source)
    logger.info("%s LED: red solid (factory-reset armed)", source)

    # Suppress the lifespan-shutdown re-announce — same reason as
    # shutdown_action: os-server's reboot ~5s later will SIGTERM hal
    # and the lifespan handler would otherwise speak PHRASE_SHUTDOWN on top
    # of the factory-reset clip still playing.
    state._shutdown_announced = True

    # Step 1: TTS announce so the user knows the gesture registered. Brief —
    # the reboot lands ~5s after the OS server accepts the POST, we want the
    # announce + 3s settle window to fit inside that.
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
    """Consume accepted hold tiers without blocking the hardware poll loop.

    A single worker owns RGB calls, including commit colors. Release cancels
    future blinking immediately; commit waits for any in-flight RGB call so an
    old blink cannot overwrite the ensuing sleep/shutdown/reset action.
    """

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
