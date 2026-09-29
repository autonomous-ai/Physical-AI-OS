"""Music route handlers -- /audio/play, /audio/stop, /audio/status, /audio/history, /speaker/mute|unmute."""

import os
import random

from fastapi import APIRouter, HTTPException

import hal.app_state as state
from hal import privacy
from hal.safety.policy import audio_quiet_now
from hal.models import (
    MusicPlayRequest,
    MusicStatusResponse,
    StatusResponse,
)
from hal.i18n import MUSIC_BACKCHANNEL_POOLS, PHRASE_QUIET_HOURS, localized_phrase
from hal.drivers.voice.tts import PROVIDER_ELEVENLABS
from hal.presets import (
    DEFAULT_LANG,
    EMO_CURIOUS,
    EMO_EXCITED,
    EMO_HAPPY,
    EMO_SLEEPY,
    RGB_CMD_SOLID,
    SERVO_CMD_MUSIC_START,
    SERVO_CMD_MUSIC_STOP,
    SERVO_MUSIC_CHILL,
    SERVO_MUSIC_CLASSICAL,
    SERVO_MUSIC_GROOVE,
    SERVO_MUSIC_HIPHOP,
    SERVO_MUSIC_HYPE,
    SERVO_MUSIC_JAZZ,
    SERVO_MUSIC_ROCK,
    SERVO_MUSIC_WALTZ,
)

router = APIRouter()


_MUSIC_STYLE_KEYWORDS: list[tuple[str, list[str]]] = [
    (SERVO_MUSIC_JAZZ, ["jazz", "swing", "blues", "soul", "funk", "bossa nova"]),
    (
        SERVO_MUSIC_CLASSICAL,
        [
            "classical",
            "orchestra",
            "symphony",
            "beethoven",
            "mozart",
            "chopin",
            "bach",
            "opera",
            "concerto",
            "sonata",
            "piano",
            "violin",
        ],
    ),
    (SERVO_MUSIC_HIPHOP, ["hip hop", "hiphop", "hip-hop", "rap", "trap", "rnb", "r&b"]),
    (SERVO_MUSIC_ROCK, ["rock", "metal", "punk", "grunge", "heavy", "guitar", "band"]),
    (SERVO_MUSIC_WALTZ, ["waltz", "tango", "ballroom", "foxtrot"]),
    (SERVO_MUSIC_CHILL, ["chill", "lofi", "lo-fi", "lo fi", "ambient", "relax", "mellow", "study", "calm", "sleep"]),
    (SERVO_MUSIC_HYPE, ["hype", "edm", "electronic", "dance", "rave", "party", "upbeat", "electro", "techno", "house", "festival"]),
]

_MUSIC_STYLE_EMOTION: dict[str, str] = {
    SERVO_MUSIC_GROOVE: EMO_HAPPY,
    SERVO_MUSIC_JAZZ: EMO_HAPPY,
    SERVO_MUSIC_CLASSICAL: EMO_CURIOUS,
    SERVO_MUSIC_HIPHOP: EMO_EXCITED,
    SERVO_MUSIC_ROCK: EMO_EXCITED,
    SERVO_MUSIC_WALTZ: EMO_HAPPY,
    SERVO_MUSIC_CHILL: EMO_SLEEPY,
    SERVO_MUSIC_HYPE: EMO_EXCITED,
}


# yt-dlp + ffmpeg take 1-3s to start; a cached TTS line fills the gap (pools in hal/i18n.py).

# Excluded from the next pick; -1 = nothing spoken yet.
_last_backchannel_idx: int = -1


def _active_stt_language() -> str:
    """Read stt_language from the OS server's config.json. Empty/missing → ""."""
    try:
        from hal.config import _os_cfg_get
        return (_os_cfg_get("stt_language") or "").strip()
    except Exception:
        return ""


def _backchannel_pool() -> list[str]:
    """Phrase pool for the active language x TTS provider (falls back to DEFAULT_LANG / plain pool)."""
    is_elevenlabs = getattr(state.tts_service, "_provider", "") == PROVIDER_ELEVENLABS
    lang = _active_stt_language()
    pool = MUSIC_BACKCHANNEL_POOLS.get((lang, is_elevenlabs))
    if pool is None:
        pool = MUSIC_BACKCHANNEL_POOLS[(DEFAULT_LANG, is_elevenlabs)]
    return pool


def _fire_music_backchannel() -> None:
    """Speak a random short cue unless muted, speaking, playing music, or mid-STT."""
    global _last_backchannel_idx
    if state._speaker_muted:
        return
    tts = state.tts_service
    if tts is None or not getattr(tts, "available", False):
        return
    if tts.speaking:
        return
    if state.music_service and state.music_service.playing:
        return
    if state.voice_service and state.voice_service.listening:
        state.logger.info("Music backchannel suppressed: STT session active")
        return
    pool = _backchannel_pool()
    candidates = [i for i in range(len(pool)) if i != _last_backchannel_idx]
    idx = random.choice(candidates)
    phrase = pool[idx]
    try:
        tts.speak_cached(phrase)
        _last_backchannel_idx = idx
    except Exception as e:
        state.logger.warning("Music backchannel speak failed: %s", e)


def _announce_quiet_hours() -> None:
    """Speak the localized quiet-hours notice so a suppressed music request isn't silent (best-effort)."""
    tts = state.tts_service
    if tts is None or not getattr(tts, "available", False) or tts.speaking:
        return
    phrase = localized_phrase(PHRASE_QUIET_HOURS)
    if not phrase:
        return
    try:
        tts.speak(phrase)
    except Exception as e:
        state.logger.warning("quiet-hours TTS notice failed: %s", e)


def _detect_music_style(query: str) -> str:
    """Return recording name matching the genre keywords in query, else music_groove."""
    q = query.lower()
    for recording, keywords in _MUSIC_STYLE_KEYWORDS:
        if any(k in q for k in keywords):
            return recording
    return SERVO_MUSIC_GROOVE


def _on_music_complete():
    """Restore state after music ends (servo, LED, display)."""
    if state.animation_service:
        state.animation_service.dispatch(SERVO_CMD_MUSIC_STOP, None)

    # Stop music wave (if active) before restoring underlying LED state.
    # Idempotent: no-op if _music_playing already False (e.g. double-fire
    # from explicit /audio/stop + thread finally).
    state._on_music_play_end()

    # Sleep is terminal: a late music end must not start idle breathing.
    if state._sleeping:
        state.logger.info("Music stop: LED restore skipped -- sleepy owns the strip")
    else:
        user_state = state._user_led_state
        state.logger.info("Music stop: restoring state type=%s", user_state.get("type") if user_state else None)
        if user_state is not None and user_state.get("type") != "off":
            state._restore_user_led()
        elif state.rgb_service:
            # Settle on the ambient resting look, not the `idle` preset.
            from hal.presets import AMBIENT_RESTING_LED, ambient_resting_is_dark

            try:
                state._stop_current_effect()
                if ambient_resting_is_dark():
                    state.rgb_service.dispatch(RGB_CMD_SOLID, (0, 0, 0))
                    state.logger.info("Music stop: no user state -- resting dark, cleared")
                else:
                    state._start_preset_effect(AMBIENT_RESTING_LED, "led-music-idle")
                    state.logger.info("Music stop: no user state -- ambient resting")
            except Exception as e:
                state.logger.warning("Music stop LED failed: %s", e)

    if state.display_service:
        try:
            state.display_service.set_expression("neutral")
        except Exception as e:
            state.logger.warning("Music stop display failed: %s", e)


@router.post("/audio/play", response_model=StatusResponse, tags=["Audio"])
def audio_play(req: MusicPlayRequest):
    """Search YouTube and play audio through the speaker."""
    if state._speaker_muted:
        state.logger.info("POST /audio/play: suppressed -- speaker muted (query='%s')", req.query[:80])
        return {"status": "suppressed"}
    # Safety gate (SAFETY.md audio.quiet_hours) and cancel gate (see app_state.MUSIC_CANCEL_GUARD_S).
    if state.music_cancel_active():
        state.logger.info("POST /audio/play: suppressed -- cancelled by recent click (query='%s')", req.query[:80])
        return {"status": "suppressed"}
    if audio_quiet_now(state.safety_policy):
        state.logger.info("POST /audio/play: suppressed -- audio quiet hours (query='%s')", req.query[:80])
        _announce_quiet_hours()
        return {"status": "suppressed"}
    if not state.music_service:
        raise HTTPException(503, "Music service not available")
    if not state.music_service.available:
        raise HTTPException(
            503, "Music service not available -- missing sounddevice or numpy"
        )

    from hal.drivers.voice.music_service import canonicalize_person

    raw_person = req.person.strip()
    person = canonicalize_person(raw_person) if raw_person else ""
    if raw_person and person != raw_person.lower():
        state.logger.info("POST /audio/play: canonicalized person '%s' -> '%s'", raw_person, person)
    state.logger.info("POST /audio/play: query='%s' person='%s'", req.query[:80], person)
    style = _detect_music_style(req.query)
    state.logger.info("music style detected: %s", style)
    emotion = _MUSIC_STYLE_EMOTION.get(style, EMO_HAPPY)

    def _on_audio_started():
        if state.animation_service:
            state.animation_service.dispatch(SERVO_CMD_MUSIC_START, style)
        state._on_music_play_start()

    if req.query.startswith("/") and os.path.isfile(req.query):
        started = state.music_service.play_file(req.query, on_started=_on_audio_started, person=person)
    else:
        started = state.music_service.play(req.query, on_started=_on_audio_started, person=person)
    if not started:
        raise HTTPException(409, "Music is busy playing")
    state._apply_emotion_led_display(emotion)
    return {"status": "ok"}


@router.post("/audio/stop", response_model=StatusResponse, tags=["Audio"])
def audio_stop():
    """Stop current music playback."""
    if state.music_service and state.music_service.playing:
        state.music_service.stop()
    _on_music_complete()
    return {"status": "ok"}


@router.post("/speaker/mute", response_model=StatusResponse, tags=["Audio"])
@privacy.serialized
def mute_speaker():
    """Mute all audio output -- TTS, music, backchannel suppressed."""
    if privacy.speaker_muted:
        privacy.speaker_before = True
    if state._sleepy_auto_muted_speaker:
        # An explicit mute replaces sleep's temporary ownership; wake must preserve it.
        state._sleepy_auto_muted_speaker = False
        state._persist_sleep_state()
    if state._speaker_muted:
        state._persist_speaker_state()
        return {"status": "already_muted"}
    state._speaker_muted = True
    if state.tts_service and state.tts_service.speaking:
        state.tts_service.stop()
    if state.music_service and state.music_service.playing:
        state.music_service.stop()
    state._persist_speaker_state()
    state.logger.info("Speaker muted")
    return {"status": "ok"}


@router.post("/speaker/unmute", response_model=StatusResponse, tags=["Audio"])
@privacy.serialized
def unmute_speaker():
    """Unmute audio output."""
    if privacy.speaker_muted:
        raise HTTPException(409, "Privacy switch is on -- flip the switch to unmute speaker")
    # Drop a pending scene drain, or it would re-mute seconds later.
    state._cancel_scene_speaker_drain()
    if not state._speaker_muted:
        return {"status": "already_unmuted"}
    state._speaker_muted = False
    state._persist_speaker_state()
    state.logger.info("Speaker unmuted")
    return {"status": "ok"}


@router.get("/audio/status", response_model=MusicStatusResponse, tags=["Audio"])
def audio_status():
    """Get music playback status."""
    return {
        "available": state.music_service is not None and state.music_service.available,
        "playing": state.music_service.playing if state.music_service else False,
        "title": state.music_service.current_title if state.music_service else None,
        "speaker_muted": state._speaker_muted,
    }


@router.get("/audio/history", tags=["Audio"])
def audio_history(date: str | None = None, person: str = "", last: int = 50):
    """Return music playback history for AI to learn user preferences."""
    from hal.drivers.voice.music_service import canonicalize_person, query_play_history

    raw_person = person.strip()
    norm_person = canonicalize_person(raw_person) if raw_person else state.DEFAULT_USER
    entries = query_play_history(person=norm_person, date_str=date, last=min(last, 500))
    return {"date": date or "today", "person": norm_person, "entries": entries, "count": len(entries)}
