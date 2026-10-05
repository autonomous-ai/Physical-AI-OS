"""TTP223 capacitive touchpad handler (dog-head touch surface).

Destructive gestures are OFF: the IC runs in FastMode (output drops ~50ms after touch),
so a true hold is impossible without rewiring the FM pin.
"""

import logging
import os
import threading
import time

from hal.board.board import TouchConfig
from hal.drivers import touch_debug
from hal.drivers.button_actions import (
    head_pat_action,
    play_ack_chime,
)

logger = logging.getLogger(__name__)

# TTP223 wiring is injected from hal/board/ttp223.py at startup.

SESSION_GAP_S = 0.2

# Decision window: after a session ends, wait this long for more sessions before
# classifying as a single tap.
DECISION_WINDOW_S = 1.2

PET_SESSION_THRESHOLD = 2

PET_COOLDOWN_S = 1.5

# Settle window after claiming the lines. Ignore all edges for this long after claim so
# the startup transient never starts a session.
SETTLE_S = 0.5

SWIPE_ENABLED = os.environ.get("HAL_TOUCH_SWIPE", "true").lower() in ("1", "true", "yes")

# Absolute floor on how many pads a swipe must span.
SWIPE_MIN_PADS = 2

# The movement floor, and the one number every rule derives from: gaps at or above it
# mean the hand travelled between pads, below it mean several fingers arrived together.
SWIPE_MIN_GAP_MS = float(os.environ.get("HAL_TOUCH_SWIPE_MIN_GAP_MS", "35"))

SWIPE_MAX_GAP_MS = float(os.environ.get("HAL_TOUCH_SWIPE_MAX_GAP_MS", "150"))

# How long the surface must stay empty before a new touch counts as the hand ARRIVING
# again rather than sliding between pads.
PRESS_MIN_EMPTY_MS = float(os.environ.get("HAL_TOUCH_PRESS_MIN_EMPTY_MS", "15"))

# Pull-down hold before reading a candidate line in the detect probe. A wired TTP223
# drives its idle-HIGH output through the weak pull-down; a bare header pin falls to 0.
PROBE_SETTLE_S = 0.01


class TTP223Handler:
    def __init__(self, config: TouchConfig | None):
        self._config = config
        self._lgpio = None
        self._handle = None
        self._callbacks = []
        self._chip = 0
        self._lines = []
        self._axis = None
        # Lines with a pad actually wired. None = every configured line (no detect).
        self._active = None
        # Per-contact first-touch order for the current gesture cycle: [[(line, ts),
        # ...], ...], one inner list per contact. NOT flattened — device-measured
        # 2026-08-27.
        self._contacts = []
        self._contact = []
        self._released = set()
        self._held = set()
        self._presses = 0
        self._emptied_at_ms = None
        self._lock = threading.Lock()
        self._session_end_timer = None
        self._decision_timer = None
        self._session_count = 0
        self._pet_cooldown_until = 0.0
        self._ignore_edges_until = 0.0

    def start(self):
        config = self._config
        if config is None:
            logger.info("TTP223 disabled: no active device/board touch wiring")
            return

        import lgpio

        self._chip = config.chip
        self._lines = list(config.lines)
        self._axis = list(config.axis) if config.axis is not None else None
        self._lgpio = lgpio

        self._ignore_edges_until = time.monotonic() + SETTLE_S

        try:
            self._handle = lgpio.gpiochip_open(self._chip)
        except Exception as e:
            logger.warning("TTP223 gpiochip_open(%d) failed: %s", self._chip, e)
            return

        if config.detect:
            self._active = self._probe_wired()
            logger.info(
                "TTP223 detect: wired pads %s of candidates %s (others learned on first touch)",
                sorted(self._active), self._lines,
            )
            # The probe flips bias on every line; restart the settle window after it.
            self._ignore_edges_until = time.monotonic() + SETTLE_S

        for line in self._lines:
            try:
                lgpio.gpio_claim_alert(
                    self._handle, line, lgpio.BOTH_EDGES, lgpio.SET_PULL_UP
                )
                cb = lgpio.callback(
                    self._handle, line, lgpio.BOTH_EDGES, self._on_edge
                )
                self._callbacks.append(cb)
            except Exception as e:
                logger.warning("TTP223 claim line %d failed: %s", line, e)

        if not self._callbacks:
            logger.warning("TTP223 no lines claimed -- disabled")
            return

        logger.info(
            "TTP223 ready on gpiochip%d lines %s (session %dms, decision %dms, pet>=%d sessions)",
            self._chip,
            self._lines,
            int(SESSION_GAP_S * 1000),
            int(DECISION_WINDOW_S * 1000),
            PET_SESSION_THRESHOLD,
        )

    def _probe_wired(self):
        """Return the candidate lines with a TTP223 driving them.

        A pad touched during the probe reads LOW and is missed; the first real touch
        adds it back (see _on_edge), so a miss only delays that pad.
        """
        lgpio = self._lgpio
        wired = set()
        for line in self._lines:
            try:
                lgpio.gpio_claim_input(self._handle, line, lgpio.SET_PULL_DOWN)
                time.sleep(PROBE_SETTLE_S)
                if lgpio.gpio_read(self._handle, line) == 1:
                    wired.add(line)
            except Exception as e:
                logger.warning("TTP223 probe line %d failed: %s", line, e)
            finally:
                try:
                    lgpio.gpio_free(self._handle, line)
                except Exception:
                    pass
        return wired

    def _wired(self):
        """Pads a swipe must cover: the detected set, or every configured line."""
        return set(self._active) if self._active else set(self._lines)

    def _on_edge(self, chip, gpio, level, tick):
        if time.monotonic() < self._ignore_edges_until:
            # Traced anyway, flagged: a suppressed edge and a pad that never
            # fired look identical in a trace that omits them.
            touch_debug.start_cycle(self._chip, self._lines)
            touch_debug.note_edge(gpio, level, suppressed=True)
            return
        touch_debug.start_cycle(self._chip, self._lines, self._axis)
        touch_debug.note_edge(gpio, level)
        with self._lock:
            if level == 0 and self._active is not None and gpio not in self._active:
                self._active.add(gpio)
                logger.info("TTP223 pad on line %d learned from touch; wired %s",
                            gpio, sorted(self._active))
            if level == 0:
                # A PRESS is the surface going from nothing-held to held: the hand
                # arriving. During a swipe the finger reaches the far pad before the
                # near one auto-releases, so the surface never empties and the whole
                # gesture is ONE press.
                now_ms = time.monotonic() * 1000.0
                if not self._held:
                    empty_for = (
                        now_ms - self._emptied_at_ms
                        if self._emptied_at_ms is not None
                        else None
                    )
                    if empty_for is None or empty_for >= PRESS_MIN_EMPTY_MS:
                        self._presses += 1
                    self._emptied_at_ms = None
                self._held.add(gpio)
                repeat = bool(self._contact) and self._contact[-1][0] == gpio
                if not repeat or gpio in self._released:
                    self._contact.append((gpio, time.monotonic()))
                self._released.discard(gpio)
            else:
                self._released.add(gpio)
                self._held.discard(gpio)
                if not self._held:
                    self._emptied_at_ms = time.monotonic() * 1000.0
        with self._lock:
            if self._session_end_timer is not None:
                self._session_end_timer.cancel()
            self._session_end_timer = threading.Timer(
                SESSION_GAP_S, self._on_session_end
            )
            self._session_end_timer.daemon = True
            self._session_end_timer.start()

    def _close_contact(self):
        """Move the open contact into the cycle's list. Called at session end."""
        with self._lock:
            if self._contact:
                self._contacts.append(self._contact)
            self._contact = []

    def _classify(self):
        """Read the cycle as (is_swipe, moved, gaps, n_contacts, revisited, landed, presses)."""
        with self._lock:
            contacts = [list(c) for c in self._contacts]
            if self._contact:
                contacts.append(list(self._contact))
        axis = self._axis or self._lines
        pos_of = {line: i for i, line in enumerate(axis)}

        def gaps_of(c):
            return [(b - a) * 1000.0 for (_, a), (_, b) in zip(c, c[1:])]

        # Reported on every trace, not only when a swipe was found — the whole point of
        # recording it is to see how near the floor a gesture landed.
        all_gaps = [x for c in contacts for x in gaps_of(c)]

        landed = any(
            (b - a) * 1000.0 < SWIPE_MIN_GAP_MS
            for c in contacts
            for (_, a), (_, b) in zip(c, c[1:])
        )

        sets_nonempty = [c for c in contacts if c]
        is_swipe = False
        moved_within = False
        # A stroke goes back over ground it already covered: more steps than distinct
        # pads means the finger returned to a pad it had left.
        revisited = any(len(c) > len({l for l, _ in c}) for c in contacts)
        for c in contacts:
            g = gaps_of(c)
            if any(x >= SWIPE_MIN_GAP_MS for x in g):
                moved_within = True
            positions = [pos_of[l] for l, _ in c if l in pos_of]
            pads = {l for l, _ in c}
            # EVERY wired pad, not a fixed count. Compared against the detected pads
            # (or the board's own line list) so the rule follows the hardware.
            if len(pads) < SWIPE_MIN_PADS or pads != self._wired():
                continue
            if len(positions) < SWIPE_MIN_PADS:
                continue
            deltas = [b - a for a, b in zip(positions, positions[1:]) if b != a]
            if not deltas or any((a > 0) != (b > 0) for a, b in zip(deltas, deltas[1:])):
                continue
            if len(sets_nonempty) == 1 and self._presses <= 1 and any(
                SWIPE_MIN_GAP_MS <= x <= SWIPE_MAX_GAP_MS for x in g
            ):
                is_swipe = True

        sets = [{l for l, _ in c} for c in contacts if c]
        disjoint = len(sets) >= 2 and not set.intersection(*sets)
        with self._lock:
            presses = self._presses
        return (is_swipe, (moved_within or disjoint),
                (min(all_gaps), max(all_gaps)) if all_gaps else (0.0, 0.0), len(sets),
                revisited, landed, presses)

    def _pad_name(self, line):
        """Label a line the way the tracer does, so both read alike."""
        return touch_debug._pad(line)

    def _reset_cycle(self):
        """Clear the per-gesture contacts once a gesture has resolved."""
        with self._lock:
            self._contacts = []
            self._contact = []
            self._released = set()
            self._held = set()
            self._presses = 0
            self._emptied_at_ms = None

    def _on_session_end(self):
        fire_pet = False
        grab_floor = False
        swallowed = False
        # Close the contact first, then read the cycle. Both take the lock
        # themselves, so they run before entering the block below.
        self._close_contact()
        (_is_swipe, _moved, _min_gap, _n, _revisited,
         _landed, _presses) = self._classify()
        pet_now = False
        with self._lock:
            self._session_end_timer = None
            now = time.monotonic()
            if now < self._pet_cooldown_until:
                self._pet_cooldown_until = now + PET_COOLDOWN_S
                if self._decision_timer is not None:
                    self._decision_timer.cancel()
                    self._decision_timer = None
                logger.debug("TTP223 session ignored (pet cooldown)")
                swallowed = True
                count = self._session_count
            else:
                self._session_count += 1
                count = self._session_count
                # Pet's fast path needs the hand to have MOVED between contacts, and at
                # least two of them.
                pet_now = SWIPE_ENABLED and not _landed and (
                    _revisited or (_moved and count >= PET_SESSION_THRESHOLD)
                )
                # First session of a burst: play the acknowledgement chime.
                # Checked outside the lock because it does I/O.
                grab_floor = count == 1
                logger.debug("TTP223 session ended (count=%d)", count)
                if SWIPE_ENABLED:
                    # Reversal is decidable the moment it happens and cannot later
                    # become a swipe, so pet keeps its fast path. Checked outside the
                    # lock below.
                    fire_pet = pet_now
                    if not fire_pet:
                        if self._decision_timer is not None:
                            self._decision_timer.cancel()
                        self._decision_timer = threading.Timer(
                            DECISION_WINDOW_S, self._on_decision
                        )
                        self._decision_timer.daemon = True
                        self._decision_timer.start()
                    else:
                        if self._decision_timer is not None:
                            self._decision_timer.cancel()
                            self._decision_timer = None
                        self._session_count = 0
                        self._pet_cooldown_until = now + PET_COOLDOWN_S
                elif count >= PET_SESSION_THRESHOLD:
                    if self._decision_timer is not None:
                        self._decision_timer.cancel()
                        self._decision_timer = None
                    self._session_count = 0
                    self._pet_cooldown_until = now + PET_COOLDOWN_S
                    fire_pet = True
                else:
                    if self._decision_timer is not None:
                        self._decision_timer.cancel()
                    self._decision_timer = threading.Timer(
                        DECISION_WINDOW_S, self._on_decision
                    )
                    self._decision_timer.daemon = True
                    self._decision_timer.start()
        # Tracing and actions stay outside the lock: both do I/O, and the
        # session state above is already committed.
        touch_debug.note_session_end(count)
        if swallowed:
            touch_debug.note_decision(
                "IGNORED", "session inside pet cooldown; cooldown extended", count
            )
            touch_debug.finish("IGNORED-pet_cooldown")
            self._reset_cycle()
            return
        if grab_floor:
            self._ack_first_session()
        if fire_pet:
            reason = (
                f"{_n} contacts with no pad in common -- the hand moved"
                if pet_now
                else f"session count reached {PET_SESSION_THRESHOLD}"
            )
            touch_debug.note_classifier(
                revisited=_revisited, landed=_landed, presses=_presses, is_swipe=_is_swipe, moved=_moved,
                gap_min_ms=round(_min_gap[0], 1), gap_max_ms=round(_min_gap[1], 1),
                contacts=_n, move_floor_ms=SWIPE_MIN_GAP_MS,
                contact_pads=[[self._pad_name(l) for l, _ in c] for c in self._contacts],
            )
            touch_debug.note_decision("PET", reason, count)
            touch_debug.note_action("head_pat_action", "TTP223")
            touch_debug.finish("PET")
            self._reset_cycle()
            head_pat_action(source="TTP223")

    def _ack_first_session(self):
        """Acknowledge the first contact without interrupting speech."""

        def _run():
            try:
                play_ack_chime(source="TTP223")
            except Exception as e:
                logger.warning("TTP223 first-session ack failed: %s", e)

        threading.Thread(
            target=_run, daemon=True, name="ttp223-touch-ack"
        ).start()

    def _on_decision(self):
        with self._lock:
            count = self._session_count
            self._session_count = 0
            self._decision_timer = None
        self._close_contact()
        (is_swipe, moved, gaps, n_contacts, revisited,
         landed, presses) = self._classify()

        if count < 1:
            touch_debug.note_decision("NONE", "decision timer fired at count=0", count)
            touch_debug.finish("IGNORED-no_sessions")
            self._reset_cycle()
            return

        if SWIPE_ENABLED:
            if is_swipe:
                self._dispatch(
                    "SWIPE", f"one contact traversed all pads, gaps {gaps[0]:.0f}-{gaps[1]:.0f}ms",
                    count, "head_pat_action", head_pat_action,
                )
                return

            if (revisited and landed) or (presses >= 2 and not revisited):
                self._dispatch(
                    "DOUBLE_TAP",
                    ("revisited a pad, and two pads lit together" if revisited
                     else f"the hand arrived on the surface {presses} times"),
                    count, "head_pat_action", head_pat_action,
                )
                return

            if (revisited and not landed) or (
                count >= PET_SESSION_THRESHOLD and moved and not landed
            ):
                self._dispatch(
                    "PET",
                    "revisited a pad with no landing -- travel throughout" if revisited
                    else f"{count} contacts with no pad in common",
                    count, "head_pat_action", head_pat_action,
                )
                return

            if count >= PET_SESSION_THRESHOLD:
                self._dispatch(
                    "DOUBLE_TAP", f"{count} contacts, no revisit and no movement",
                    count, "head_pat_action", head_pat_action,
                )
                return

        self._dispatch(
            "TAP", self._tap_reason(count),
            count, "head_pat_action", head_pat_action,
        )

    def _tap_reason(self, count):
        """Why this resolved to TAP — naming the rule that declined, not the
        branch that caught it.
        """
        try:
            with self._lock:
                contacts = [list(c) for c in self._contacts]
                if self._contact:
                    contacts.append(list(self._contact))
            live = [c for c in contacts if c]
            base = "fallback -- no other gesture matched"
            if not live:
                return f"{base}; no pads recorded"
            if len(live) > 1:
                return f"{base}; {len(live)} contacts"
            c = live[0]
            pads = {l for l, _ in c}
            wired = self._wired()
            if len(pads) < len(wired):
                return (
                    f"{base}; touched {len(pads)} of {len(wired)} pads -- "
                    "not an end-to-end pass"
                )
            gaps = [(b - a) * 1000.0 for (_, a), (_, b) in zip(c, c[1:])]
            in_band = [g for g in gaps if SWIPE_MIN_GAP_MS <= g <= SWIPE_MAX_GAP_MS]
            if gaps and not in_band and min(gaps) > SWIPE_MAX_GAP_MS:
                return (
                    f"{base}; every pad, single pass, but slowest gap "
                    f"{min(gaps):.1f}ms > {SWIPE_MAX_GAP_MS:.0f}ms ceiling -- "
                    "read as the hand lifting and coming back, not travelling"
                )
            if gaps and max(gaps) < SWIPE_MIN_GAP_MS:
                return (
                    f"{base}; every pad, single pass, but max gap "
                    f"{max(gaps):.1f}ms < {SWIPE_MIN_GAP_MS:.0f}ms floor -- "
                    "read as fingers landing together, not a hand moving"
                )
            return f"{base}; decision window expired at count={count}"
        except Exception:
            return f"decision window expired at count={count}"

    def _dispatch(self, gesture, reason, count, fn_name, fn, **trace_fields):
        """Record the verdict, CLOSE THE TRACE, then run the action."""
        is_swipe, moved, gaps, n, revisited, landed, presses = self._classify()
        touch_debug.note_classifier(
            revisited=revisited, landed=landed, presses=presses,
            is_swipe=is_swipe, moved=moved,
            gap_min_ms=round(gaps[0], 1), gap_max_ms=round(gaps[1], 1),
            contacts=n, move_floor_ms=SWIPE_MIN_GAP_MS,
            contact_pads=[[self._pad_name(l) for l, _ in c] for c in self._contacts],
        )
        touch_debug.note_decision(gesture, reason, count)
        touch_debug.note_action(fn_name, "TTP223", **trace_fields)
        touch_debug.finish(gesture)
        self._reset_cycle()
        with self._lock:
            self._pet_cooldown_until = time.monotonic() + PET_COOLDOWN_S
        fn(source="TTP223")
