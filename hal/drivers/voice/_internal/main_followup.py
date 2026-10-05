"""Keep the user's answer to a main-agent question with the main agent (#564).

The main agent owns multi-turn tasks such as face or voice enrollment. When its
heard reply ends in a question, the next answer must reach main as a live turn.
Realtime still hears the audio; this window only steers routing and never
grants wake authorization.
"""

import logging
import threading
import time

import hal.config as hal_config

logger = logging.getLogger("hal.voice")

_TRAILING = " \t\r\n\"'”’»)]*_~"
_lock = threading.Lock()
_deadline = 0.0
_question = ""


def ends_with_question(text: str) -> bool:
    """Whether the reply's final sentence is a question."""
    return text.rstrip(_TRAILING).endswith(("?", "？"))


def note_main_reply(text: str, *, heard: bool) -> None:
    """Open the window on a heard question; any other main reply closes it."""
    global _deadline, _question
    ttl = float(hal_config.REALTIME_MAIN_FOLLOWUP_S)
    with _lock:
        if heard and ttl > 0 and ends_with_question(text):
            _deadline, _question = time.monotonic() + ttl, text.strip()
            logger.info("[realtime] Main agent asked a question — next answer belongs to main (%.0fs)", ttl)
        else:
            _deadline, _question = 0.0, ""


def pending_main_question() -> str:
    with _lock:
        return _question if time.monotonic() < _deadline else ""


def take_main_followup() -> bool:
    """Return True once per open window, then close it."""
    global _deadline, _question
    with _lock:
        active = time.monotonic() < _deadline
        _deadline, _question = 0.0, ""
        return active


def reset_main_followup() -> None:
    global _deadline, _question
    with _lock:
        _deadline, _question = 0.0, ""
