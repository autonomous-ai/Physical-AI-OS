"""Serialized voice-only transitions; hardware services and privacy stay intact."""

import os
import threading

from hal import config

_transition_lock = threading.Lock()


def _publish(mode, wakeword):
    from hal.drivers.voice._internal import config as voice_config, live_playback

    live = mode == "automatic" and os.environ.get("HAL_LIVE_MODE", "false").lower() in (
        "1", "true", "yes",
    )
    detection = os.environ.get("HAL_REALTIME_TURN_DETECTION", "off")
    if mode == "tap_to_talk":
        detection = "off"
    elif live and detection.strip().lower() in ("off", "none", ""):
        detection = "server_vad"
    config.VOICE_INPUT_MODE = mode
    config.WAKEWORD_ENABLED = mode == "automatic" and wakeword
    config.LIVE_MODE = voice_config.LIVE_MODE = live
    config.REALTIME_TURN_DETECTION = detection
    live_playback.ENABLED = live_playback._enabled()


def _resume_previous(service, workers):
    revision = service._lifecycle_revision
    service._input_mode_resume_pending = True

    def resume():
        try:
            service._resume_after_teardown(revision, *workers)
        finally:
            with service._lifecycle_lock:
                if service._lifecycle_revision == revision:
                    service._input_mode_resume_pending = False

    threading.Thread(target=resume, daemon=True, name="voice-mode-rollback").start()


def apply_to_service(service, mode, wakeword):
    from hal import app_state
    from hal.drivers.voice._internal import live_playback
    from hal.drivers.voice._internal.live_gate import AdaptiveLiveGate
    from hal.drivers.voice._internal.live_reply import LiveReplyGuard

    with service._lifecycle_lock:
        old_mode, old_wake = config.VOICE_INPUT_MODE, config.WAKEWORD_ENABLED
        if old_mode == mode and old_wake == (mode == "automatic" and wakeword):
            if getattr(service, "_input_mode_resume_pending", False) or (
                not service._running and any(
                    worker is not None and worker.is_alive()
                    for worker in (service._thread, service._realtime_stop_thread)
                )
            ):
                raise RuntimeError("Voice teardown is still pending")
            return
        was_running = service._running or getattr(service, "_input_mode_resume_pending", False)
        service._input_mode_resume_pending = False
        service._lifecycle_revision += 1
        service._running = False
        service._stop_locked(summarize=False)
        workers = (service._thread, service._realtime_stop_thread)
        if any(worker is not None and worker.is_alive() for worker in workers):
            # Resume the old mode only after teardown, and only if a later
            # privacy/lifecycle request has not invalidated this revision.
            if was_running:
                _resume_previous(service, workers)
            raise RuntimeError("Voice teardown is still pending; input mode unchanged")
        _publish(mode, wakeword)
        service._live_gate = AdaptiveLiveGate() if live_playback.ENABLED else None
        service._aec_live_replies = LiveReplyGuard() if live_playback.ENABLED else None
        if (was_running and not app_state.privacy.mic_locked() and not app_state._mic_muted
                and not app_state._sleeping and not app_state._enrolling):
            service._input_mode_ready = threading.Event()
            try:
                service._start_locked()
                if not service._input_mode_ready.wait(3):
                    raise RuntimeError("Voice input loop did not become ready")
            except Exception:
                service._running = False
                service._stop_locked(summarize=False)
                _publish(old_mode, old_wake)
                service._live_gate = AdaptiveLiveGate() if live_playback.ENABLED else None
                service._aec_live_replies = LiveReplyGuard() if live_playback.ENABLED else None
                _resume_previous(service, (service._thread, service._realtime_stop_thread))
                raise


def set_input_mode(mode, wakeword):
    """Apply config even before voice initialization, without opening a muted mic."""
    from hal import app_state

    with _transition_lock:
        service = app_state.voice_service
        if service is None or app_state.simulation_audio:
            _publish(mode, wakeword)
        else:
            service.set_input_mode(mode, wakeword)
