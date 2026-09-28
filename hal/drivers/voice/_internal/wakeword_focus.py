"""Short-lived follow-up focus for wake-word conversations."""

import threading
import time
import logging
from collections.abc import Callable

logger = logging.getLogger("hal.voice")


class WakeWordFocus:
    """Track a monotonic idle deadline shared by successive mic sessions."""

    def __init__(self, timeout_s: float, clock: Callable[[], float] = time.monotonic,
                 pending_speech: Callable[[str], bool] | None = None):
        self._timeout_s = max(0.0, timeout_s)
        self._clock = clock
        self._until = 0.0
        self._lock = threading.Lock()
        self._pending_speech = pending_speech or (lambda owner: False)
        self._turns = {}

    def begin(self, interaction_id: str) -> bool:
        """Hold an already-authorized turn; callers must enforce the wake gate.

        A five-minute lease bounds lost terminal events. Repeated observations
        of the same turn must not renew that lease or resurrect a finished turn.
        """
        if not interaction_id or self._timeout_s <= 0:
            return False
        with self._lock:
            self._settle()
            if interaction_id not in self._turns:
                self._turns[interaction_id] = {
                    "expires": self._clock() + 300, "local": True,
                    "main": False, "run": "", "done": False, "cancelled": False,
                }
            return not self._turns[interaction_id]["done"]

    def activity(self, interaction_id: str, run_id: str, phase: str) -> bool:
        """Bind/release main processing without granting an unknown voice turn."""
        with self._lock:
            self._settle()
            turn = self._turns.get(interaction_id)
            if turn is None or not run_id or phase not in ("start", "end", "cancel"):
                return False
            if turn["run"] and turn["run"] != run_id:
                return False
            if phase == "start":
                if turn["done"]:
                    return False
                turn["run"] = run_id
                turn["main"] = True
            elif turn["run"] == run_id:
                if phase == "cancel":
                    turn["done"] = True
                    turn["cancelled"] = True
                else:
                    turn["main"] = False
            else:
                return False
            self._settle()
            return True

    def finish(self, interaction_id: str, *, cancelled: bool = False) -> None:
        with self._lock:
            turn = self._turns.get(interaction_id)
            if turn is not None:
                if cancelled:
                    turn["done"] = True
                    turn["cancelled"] = True
                else:
                    turn["local"] = False
            self._settle()

    def playback_finished(self) -> None:
        """Called after TTS drains; unrelated playback cannot open focus."""
        with self._lock:
            self._settle()

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()
            self._until = 0.0

    def _settle(self) -> None:
        now = self._clock()
        for iid, turn in list(self._turns.items()):
            if now >= turn["expires"]:
                del self._turns[iid]
                continue
            if turn["done"] or turn["local"] or turn["main"]:
                continue
            owners = (f"interaction:{iid}", f"run:{iid}", f"run:{turn['run']}")
            if any(self._pending_speech(owner) for owner in owners):
                continue
            turn["done"] = True
            turn["expires"] = now + self._timeout_s
            logger.info("[wake] Follow-up idle window started for %.0fs after processing/playback (interaction_id=%s)",
                        self._timeout_s, iid)

    def is_active(self) -> bool:
        with self._lock:
            self._settle()
            if any(not turn["cancelled"] for turn in self._turns.values()):
                return True
            if self._until <= self._clock():
                self._until = 0.0
                return False
            return True

    def refresh(self, timeout_s: float | None = None) -> bool:
        """Extend focus from now; false when follow-up focus is disabled.

        `timeout_s` grants a SHORTER window than the default, for an opener
        that is inferred rather than deliberate. It never lengthens one: a
        wake word and a button click are explicit acts and keep the full
        window, while a gaze wake is a guess about intent and should not be
        able to claim more floor than the gestures it sits beside.
        """
        if self._timeout_s <= 0:
            return False
        window = self._timeout_s if timeout_s is None else min(self._timeout_s, max(0.0, timeout_s))
        if window <= 0:
            return False
        with self._lock:
            until = self._clock() + window
            # A later refresh must not SHORTEN a window already granted — a
            # deliberate wake mid-conversation would otherwise be cut back to a
            # gaze-sized one.
            self._until = max(self._until, until)
        return True


def is_addressed(
    wakeword_enabled: bool,
    wake_word_heard: bool,
    focus_latched_at_session_start: bool,
    focus_active_now: bool,
) -> bool:
    """Whether the sentence being spoken has been shown to be for this device.

    Asked by everything that claims to be the addressee — the listening cue,
    the backchannel — so the device does not acknowledge a conversation it was
    never part of.

    ``focus_active_now`` is read LIVE, and that is the point: gaze can open the
    follow-up window in the MIDDLE of the sentence it is meant to acknowledge.
    Device-observed 04/09/2026 on lamp-0c89 — at speech start the camera had no
    face evidence ("of 0" samples) so the session-start latch was False, and the
    watcher only confirmed the user 3.6s later at speech END. The whole turn ran
    with no listening cue: the device sat dark through the sentence and lit up
    only for the next one.

    Live focus can only ADD an addressed turn, never remove one — the latch is
    still passed in and still wins, so a window that EXPIRES mid-sentence cannot
    retract a turn from someone already speaking.
    """
    if not wakeword_enabled:
        return True
    return wake_word_heard or focus_latched_at_session_start or focus_active_now
