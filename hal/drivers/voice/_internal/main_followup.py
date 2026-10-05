"""Keep the user's answer to a main-agent question with the main agent (#564).

The main agent owns multi-turn tasks such as face or voice enrollment. When it
asks a heard question, the user's next answer must reach main as a live turn.
Only that answer, a newer question or the safety cap ends the wait: replies to
sensing events and unheard replies leave it open. Realtime still hears the
audio; this window only steers routing and never grants wake authorization.
"""

import logging
import re
import threading
import time

import hal.config as hal_config

logger = logging.getLogger("hal.voice")

_TRAILING = " \t\r\n\"'”’»)]*_~"
_SENTENCE = re.compile(r"[^.!?。！？]+[.!?。！？]*")
_lock = threading.Lock()
_deadline = 0.0
_question = ""


def ends_with_question(text: str) -> bool:
    """Whether one of the reply's last two sentences is a question.

    Covers a short trailing instruction after the question ("What name? Just say it.").
    """
    sentences = [m.group().rstrip(_TRAILING) for m in _SENTENCE.finditer(text) if m.group().strip(_TRAILING)]
    return any(s.endswith(("?", "？")) for s in sentences[-2:])


def note_main_reply(text: str, *, heard: bool) -> None:
    """Open (or replace) the window on a heard question.

    Any other reply leaves it alone: a spoken reply to a sensing event, a read-back,
    or a muted/cancelled reply does not answer main's pending question.
    """
    global _deadline, _question
    ttl = float(hal_config.REALTIME_MAIN_FOLLOWUP_S)
    if not (heard and ttl > 0 and ends_with_question(text)):
        return
    with _lock:
        _deadline, _question = time.monotonic() + ttl, text.strip()
    logger.info("[realtime] Main agent asked a question — next answer belongs to main (cap %.0fs)", ttl)


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
