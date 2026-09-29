"""Physical privacy policy: per-device camera/speaker locks overlaid on user preferences."""

from functools import wraps
import logging
import threading

logger = logging.getLogger(__name__)
lock = threading.RLock()
camera_muted = False
speaker_muted = False
camera_before = None
speaker_before = None


def serialized(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with lock:
            return fn(*args, **kwargs)
    return wrapped


@serialized
def apply(muted, config):
    """Apply peripheral locks even when the microphone already matches."""
    global camera_muted, speaker_muted, camera_before, speaker_before
    from hal import app_state as state

    want_camera = muted and config.disable_camera_on_mute
    want_speaker = muted and config.mute_speaker_on_mute
    # Publish both gates before potentially slow device teardown.
    if want_camera and not camera_muted:
        camera_before = state._camera_disabled
    if want_speaker and not speaker_muted:
        speaker_before = state._speaker_muted
    camera_muted, speaker_muted = want_camera, want_speaker

    if camera_muted:
        state._camera_disabled = True
    if speaker_muted:
        state._speaker_muted = True

    # Each peripheral is independent: a camera failure must not leave audio on.
    actions = []
    if speaker_muted:
        actions.extend(("stop " + name, service.stop) for name, service in (
            ("TTS", state.tts_service), ("music", state.music_service),
        ) if service is not None)
    elif speaker_before is not None:
        state._speaker_muted = speaker_before
        speaker_before = None

    if camera_muted and state.camera_capture is not None:
        actions.append(("stop camera", state.camera_capture.stop))
    elif not camera_muted and camera_before is not None:
        state._camera_disabled = camera_before
        camera_before = None
        if not state._camera_disabled and state.camera_capture is not None:
            actions.append(("restore camera", state.camera_capture.start))

    for name, action in actions:
        try:
            action()
        except Exception:
            logger.exception("Privacy switch: failed to %s", name)
    state._persist_camera_state()
    state._persist_speaker_state()
    logger.info("Privacy switch: muted=%s camera_locked=%s speaker_locked=%s",
                muted, camera_muted, speaker_muted)


def mic_locked():
    """The hardware microphone switch is authoritative on every device."""
    from hal import app_state as state
    return state._hw_mic_switch_muted is True


def prepare(config):
    """Keep configured peripherals closed until the initial GPIO read completes."""
    if config and (config.disable_camera_on_mute or config.mute_speaker_on_mute):
        from hal import app_state as state
        state._hw_mic_switch_muted = True
        state._mic_muted = True
        state._mic_muted_led = True
        apply(True, config)


class GuardedCamera:
    """Gate every capture consumer, including temporary starts by realtime look."""

    def __init__(self, capture):
        object.__setattr__(self, "_capture", capture)

    def __getattr__(self, name):
        return getattr(self._capture, name)

    def __setattr__(self, name, value):
        setattr(self._capture, name, value)

    @serialized
    def start(self):
        if camera_muted:
            logger.info("Camera start blocked by privacy switch")
            return
        self._capture.start()

    @serialized
    def stop(self):
        self._capture.stop()
        self._capture.last_response = None

    @property
    def last_frame(self):
        if camera_muted:
            return None
        frame = self._capture.last_frame
        return None if camera_muted else frame

    @property
    def last_frame_ts(self):
        return 0.0 if camera_muted else self._capture.last_frame_ts

    @property
    def last_response(self):
        return None if camera_muted else self._capture.last_response

    @property
    def last_frame_description(self):
        return None if camera_muted else self._capture.last_frame_description

    def capture(self, *args, **kwargs):
        if camera_muted:
            return None
        result = self._capture.capture(*args, **kwargs)
        return None if camera_muted else result
