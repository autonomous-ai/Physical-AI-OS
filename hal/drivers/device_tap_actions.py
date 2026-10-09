"""Physical tap routing and actions for device tap-to-talk input."""

import time

import hal.app_state as state
from hal.drivers import button_actions


def device_tap_mode(snapshot) -> bool:
    """Select manual device gestures only with a known Harness-off snapshot."""
    from hal import config
    return (getattr(config, "VOICE_INPUT_MODE", "automatic") == "tap_to_talk"
            and snapshot is not None and snapshot.get("enabled") is False
            and not snapshot.get("unavailable", False))


def physical_short_tap(source: str = "button", announce: bool = False):
    """Route real taps separately from startup and privacy-switch wake actions."""
    from hal import config
    if getattr(config, "VOICE_INPUT_MODE", "automatic") != "tap_to_talk":
        button_actions.single_click_action(source=source, announce=announce)
        return
    from hal.drivers.voice._internal.harness_voice import read_voice_mode
    snapshot = read_voice_mode()
    if snapshot.get("unavailable"):
        return
    if not device_tap_mode(snapshot):
        button_actions.single_click_action(source=source, announce=announce)
        return
    state.note_user_activity(source)
    button_actions._stop_active_tracking(source)
    voice = state.voice_service
    if state._hw_mic_switch_muted is True or state._enrolling:
        if voice:
            voice.device_input.cancel()
        return
    if state._sleeping:
        if voice:
            voice.device_input.cancel()
        button_actions._wake_if_sleepy(source)
        button_actions.play_ack_chime(source)
        return
    from hal.routes.voice import stop_tts, unmute_mic
    if state.tts_service and state.tts_service.speaking:
        if voice:
            voice.device_input.cancel()
        button_actions._cancel_agent_speech(source)
        stop_tts()
        button_actions.play_ack_chime(source)
        return
    if voice is None:
        return
    if voice.device_input.active:
        voice.device_input.finish()
        return
    from hal.routes.music import audio_stop, unmute_speaker
    # Taking the floor supersedes pending replies, even before playback starts.
    # Cancel locally before reserving B; remote suppression is timestamp-scoped.
    voice.device_input.cancel()
    cutoff = int(time.time() * 1000)
    from hal.drivers.voice.tts.turn_supersession import suppress_before
    suppress_before(cutoff)
    button_actions._cancel_agent_speech(source, before_ms=cutoff)
    stop_tts()
    state.note_music_cancel()
    if state._music_playing or (state.music_service and state.music_service.playing):
        audio_stop()
    if state._speaker_muted:
        unmute_speaker()
    if state._mic_muted:
        unmute_mic()
    voice.device_input.start(after_ms=cutoff)
