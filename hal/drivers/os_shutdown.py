"""OS-shutdown announce for the lifespan shutdown path."""

import logging
import subprocess
import threading
import time

import hal.app_state as state
from hal.i18n import (
    PHRASE_REBOOT,
    PHRASE_SERVICE_RESTART,
    PHRASE_SHUTDOWN,
    PHRASES_BY_LANG,
)
from hal.presets import DEFAULT_LANG

logger = logging.getLogger(__name__)


def _is_os_stopping() -> bool:
    try:
        out = subprocess.run(
            ["systemctl", "is-system-running"],
            capture_output=True, text=True, timeout=2.0,
        )
        return out.stdout.strip() == "stopping"
    except Exception:
        return False


def _is_reboot_pending() -> bool:
    try:
        out = subprocess.run(
            ["systemctl", "list-jobs", "--no-legend"],
            capture_output=True, text=True, timeout=2.0,
        ).stdout
        return "reboot.target" in out or "kexec.target" in out
    except Exception:
        return False


def _phrase(key: str) -> str:
    try:
        from hal.config import _os_cfg_get
        lang = (_os_cfg_get("stt_language") or "").strip()
    except Exception:
        lang = ""
    pool = PHRASES_BY_LANG.get(key, {})
    return pool.get(lang) or pool.get(DEFAULT_LANG, "")


def announce_os_shutdown():
    """Speak the appropriate cue + park servos."""
    if state._shutdown_announced:
        logger.info("shutdown already announced by button action -- skip TTS")
        return

    if _is_os_stopping():
        is_reboot = _is_reboot_pending()
        kind = "reboot" if is_reboot else "shutdown"
        text = _phrase(PHRASE_REBOOT if is_reboot else PHRASE_SHUTDOWN)
    else:
        kind = "service_restart"
        text = _phrase(PHRASE_SERVICE_RESTART)

    logger.info("lifespan announce: kind=%s text=%r", kind, text)

    # Park servo before systemd kills the process, otherwise the body slams down
    # mid-pose.
    def _park_servos():
        try:
            from hal.routes.servo import release_servos
            release_servos()
        except Exception as e:
            logger.warning("servo release before shutdown failed: %s", e)

    park = threading.Thread(target=_park_servos, daemon=True, name="shutdown-park")
    park.start()

    if state.tts_service and state.tts_service.available and not state._speaker_muted and text:
        state.tts_service.speak_cached(text)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not state._tts_speaking:
            time.sleep(0.1)
        while time.monotonic() < deadline and state._tts_speaking:
            time.sleep(0.1)

    park.join(timeout=6)
