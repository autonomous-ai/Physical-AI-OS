"""TTP223 capacitive touchpad handler (dog-head touch surface).

Pad wiring is supplied by the device configuration loader from ttp223.json,
with legacy board defaults when absent. The driver owns gesture detection;
device declarations own chip, lines and optional spatial order.

Destructive gestures (reboot / shutdown) are intentionally OFF on TTP223
because the IC on this board runs in FastMode: output drops LOW within
~50ms of touch even with finger still on the pad, so a true "hold 5s"
is impossible without rewiring the FM pin. GPIO button still owns those.

LAYER 1 — SESSION. Any edge, either direction, any pad, restarts a 200ms timer.
When it lapses the "contact" ends. This coalesces the burst of cross-talk and
FastMode auto-release edges that one finger produces. A contact is one touch of
the surface — but NOT one gesture: a continuous stroke never lets the timer
lapse, so a whole 1s pet arrives as a single contact, and two fast taps arrive
as one contact too. Everything below was written after learning that on device.

LAYER 2 — GESTURE. On by default (SWIPE_ENABLED); HAL_TOUCH_SWIPE=false makes the driver
resolve only tap and count-based pet, exactly as it always did.

The signal is WHEN pads fire, not which. Device-measured on orange-lamp
2026-08-27, inter-pad gaps inside one contact:

    fingers landing together    1 .. 23 ms
    a finger travelling        53 .. 322 ms

Nothing in between, and SWIPE_MIN_GAP_MS sits in the gap. From that one
threshold everything else follows:

  SWIPE       one contact, every pad, monotonic along the axis, gaps above the
              floor. One contact only — each leg of a back-and-forth stroke is
              itself a clean pass, so allowing any leg to carry the verdict
              turns every pet into a swipe.
  DOUBLE TAP  two or more tight multi-pad BURSTS overlapping in place. Fast taps
              share a contact; slow taps arrive as separate contacts. Checked
              before pet, because the second tap re-touches the same pads.
  PET         the finger revisited a pad it had left (more steps than distinct
              pads), or contacts landed in different places. A stroke's steps are
              evenly spread, so it has no bursts to be mistaken for taps.
  TAP         anything else, including several fingers landing at once — that
              lights every pad but within ~20ms, which is not movement.

All resolved gestures trigger head_pat_action: tap, double tap, swipe and pet
are affection on the headpad. MPR121/GPIO own control gestures. The first
contact keeps its acknowledgement chime, but never stops speech. Classification
is retained for diagnostics and early pet recognition; it does not select a
mic, sleep or stop action. Every response arms the same pet cooldown.
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

# Session gap: edges within this window of the previous edge belong to
# the same session. 200ms comfortably exceeds the observed burst length
# (~30-100ms across the pads a single touch reaches) while staying below a
# natural inter-tap gap. That figure was measured on green-lamp; HAL_TOUCH_DEBUG
# re-measures it per unit (see hal/drivers/touch_debug.py).
SESSION_GAP_S = 0.2

# Decision window: after a session ends, wait this long for more
# sessions before classifying as a single tap. Field-measured stroke
# pace on this hardware is 0.8-1.2s per beat (FastMode forces a
# tap-tap-tap rhythm rather than continuous motion). 1.2s catches the
# slowest natural stroke. Cost: single tap responds 1.2s after release
# — this groups the start of a pet motion into one response.
DECISION_WINDOW_S = 1.2

# Contacts needed before the COUNT-based rules apply — the pet fallback for a
# stroke with no readable traversal, and the slow double tap. With SWIPE_ENABLED
# the spatial rules resolve most gestures before these are reached; with it off
# this is the whole of pet detection, and two taps under ~0.9s apart fire pet
# rather than two separate pet responses.
PET_SESSION_THRESHOLD = 2

# After head_pat fires, swallow further sessions for this long so a
# continuous stroke does not produce repeated pet responses. Every session inside the window extends the
# window — petting is finished only when the user stops touching for
# PET_COOLDOWN_S consecutively.
PET_COOLDOWN_S = 1.5

# Settle window after claiming the lines. lgpio reports each line's current
# level as an initial edge the moment the alert callback is registered. The
# pads rest HIGH, so without this guard those startup reports are read as a
# real touch and fire a phantom pet response ~DECISION_WINDOW after every HAL
# start (Restart=always makes it recur). Ignore all edges for this long after
# claim so the startup transient never starts a session.
SETTLE_S = 0.5

# --- Gesture classification (swipe / double tap / spatial pet) --------------
#
# Classification remains optional for diagnostics and early pet recognition.
# Both flag states map every resolved gesture to the same head-pat action.
SWIPE_ENABLED = os.environ.get("HAL_TOUCH_SWIPE", "true").lower() in ("1", "true", "yes")

# Absolute floor on how many pads a swipe must span. The real requirement is
# EVERY wired pad (checked against the board profile in _classify); this only
# guards a hypothetical 1-pad board, where "end to end" is meaningless.
#
# 2 since 2026-08-28, when the lamp dropped its middle pad and runs 96/100 only.
# Note what that costs. With three pads a swipe needed TWO independent things:
# spatial (all three, none twice) and temporal (a gap above the floor). With two
# pads "touched both pads once" is also what a single press does through
# cross-talk, so the spatial half is gone and SWIPE_MIN_GAP_MS decides alone.
# Measured on the two-pad lamp the same day the populations are still far apart
# — landings 2-27 ms, journeys 69-80 ms — but nothing is left to catch a slow
# firm press if they ever converge.
SWIPE_MIN_PADS = 2

# The movement floor, and the one number every rule derives from: gaps at or
# above it mean the hand travelled between pads, below it mean several fingers
# arrived together.
#
# 35, lowered from 40 on 2026-08-28. The first measurement (orange-lamp,
# 2026-08-27) read fingers-together at 1-23 ms and travelling at 53-322 ms, and
# 40 sat in that empty band. A later session swiped faster and landed in it:
# real swipes at 35.7 and 38.9 ms were read as three-finger taps, one of them
# missing by 1.1 ms. Re-measured over every 3-pad single-pass contact, the gap
# in the distribution is between 16.6 and 35.7, not 23 and 53 — so the floor
# belongs below 35.7, and 35 keeps clear air on both sides.
#
# The band narrows as the user swipes faster; it is a property of THEM, not just
# the hardware. Raise it if firm taps are classified as swipes, lower it if
# real swipes are missed. HAL_TOUCH_DEBUG records the gaps it is measured
# against, and a TAP trace now names this number when it is what declined.
SWIPE_MIN_GAP_MS = float(os.environ.get("HAL_TOUCH_SWIPE_MIN_GAP_MS", "35"))

# The other end of the travel band. A gap this long is not a journey across the
# surface, it is the hand lifting and coming back — the rhythm of a second tap.
#
# Without a ceiling, two deliberate taps landing on DIFFERENT pads produce
# exactly the shape of a swipe (each pad once, no revisit, a gap above the
# floor) and resolve as one. Device trace 120736 (2026-08-28) was a double tap
# read as a swipe on a single 196.7 ms gap.
#
# Measured over labelled gestures the same day, the three bands are distinct:
#     landing   6.4  7.6  27.2 ms      two pads lit together
#     travel    69.7  80.6 ms          finger crossing the surface
#     lift     196.7  273.7  291.6  291.8 ms
# 150 sits in the empty space between travel and lift. It rests on 6 labelled
# gestures, so treat it as provisional: a very slow deliberate drag would exceed
# it and read as a tap. Raise it if real swipes are missed.
SWIPE_MAX_GAP_MS = float(os.environ.get("HAL_TOUCH_SWIPE_MAX_GAP_MS", "150"))

# How long the surface must stay empty before a new touch counts as the hand
# ARRIVING again rather than sliding between pads.
#
# Without it, the handoff mid-swipe counts as a second press: the finger leaves
# the near pad microseconds after reaching the far one, the surface is briefly
# empty, and the gesture is read as two taps. Device trace 135217
# (2026-08-28) was a swipe with a textbook 105.8 ms travel gap that lost on the
# press count alone — L100 released at 105.8 ms, L96 touched at 106.5 ms, a
# 0.7 ms hole.
#
# Measured across every captured trace, real lifts leave the surface empty for
# 23.8 ms at minimum and usually 90-180 ms; that 0.7 ms is the only value below
# 20. 15 sits in wide clearance on both sides.
PRESS_MIN_EMPTY_MS = float(os.environ.get("HAL_TOUCH_PRESS_MIN_EMPTY_MS", "15"))


class TTP223Handler:
    def __init__(self, config: TouchConfig | None):
        self._config = config
        self._lgpio = None
        self._handle = None
        self._callbacks = []
        self._chip = 0
        self._lines = []
        # Physical pad order along the swipe axis; None until measured (2.2).
        self._axis = None
        # Per-contact first-touch order for the current gesture cycle:
        # [[(line, ts), ...], ...], one inner list per contact. NOT flattened —
        # device-measured 2026-08-27: several fingers landing in one place spread
        # across pads exactly like one finger moving, so a flat cross-contact
        # sequence cannot tell a 3-finger double tap from a stroke. The two
        # questions need different layers: whether a single contact TRAVERSED
        # (swipe), and whether successive contacts stayed in the same PLACE
        # (double tap).
        self._contacts = []
        self._contact = []
        # Pads released since their last touch. A re-touch of a released pad is
        # a real second contact; without this the repeat-collapse eats it.
        self._released = set()
        # Pads currently held, and how many times the hand has arrived on the
        # surface this cycle. See the PRESS note in _on_edge.
        self._held = set()
        self._presses = 0
        # When the surface last went empty, so a re-touch can be told from a
        # slide between pads. None = it has not been empty yet this cycle.
        self._emptied_at_ms = None
        self._lock = threading.Lock()
        # Session-end timer: fires SESSION_GAP_S after the last edge.
        self._session_end_timer = None
        # Decision timer: fires DECISION_WINDOW_S after the last session
        # ended, resolving how many sessions accumulated → tap vs pet.
        self._decision_timer = None
        self._session_count = 0
        # monotonic deadline before which incoming sessions are silently
        # eaten (cooldown after pet to avoid stuttering single_clicks
        # during a continuous stroke).
        self._pet_cooldown_until = 0.0
        # monotonic deadline before which edges are ignored (startup transient
        # from claiming the lines — see SETTLE_S).
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

        # Arm the settle window now, before claiming: the callback registration
        # below emits an initial edge per line (the current resting level).
        self._ignore_edges_until = time.monotonic() + SETTLE_S

        try:
            self._handle = lgpio.gpiochip_open(self._chip)
        except Exception as e:
            logger.warning("TTP223 gpiochip_open(%d) failed: %s", self._chip, e)
            return

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

    def _on_edge(self, chip, gpio, level, tick):
        # Drop the startup transient: lgpio reports each line's initial level
        # as an edge when the callback is registered. The resting-HIGH pads
        # would otherwise fire a phantom gesture on every HAL start.
        if time.monotonic() < self._ignore_edges_until:
            # Traced anyway, flagged: a suppressed edge and a pad that never
            # fired look identical in a trace that omits them.
            touch_debug.start_cycle(self._chip, self._lines)
            touch_debug.note_edge(gpio, level, suppressed=True)
            return
        touch_debug.start_cycle(self._chip, self._lines, self._axis)
        touch_debug.note_edge(gpio, level)
        # Pads rest HIGH (pull-up), so level 0 is the TOUCH edge; level 1 is
        # FastMode's auto-release and says nothing about where the finger is.
        # Recorded unconditionally, not just when SWIPE_ENABLED: it costs one
        # list append and it means HAL_TOUCH_DEBUG measures the same sequence
        # the classifier would see, which is the point of the instrument.
        # A repeat of the last pad is collapsed ONLY if the pad was not released
        # in between. A line cannot produce two falling edges without a rising
        # edge between them, so a touch following a release is a genuine second
        # contact with that pad — collapsing it threw away the evidence that the
        # hand came back. Device trace 120736: tap L100, release, tap L100
        # again, cross-talk lights L96 14 ms later; the second L100 was dropped
        # so the trace read as one long journey. What the collapse still catches
        # is a duplicate falling edge with no release, which is a driver
        # artefact rather than a touch.
        with self._lock:
            if level == 0:
                # A PRESS is the surface going from nothing-held to held: the
                # hand arriving. During a swipe the finger reaches the far pad
                # before the near one auto-releases, so the surface never
                # empties and the whole gesture is ONE press. Two taps always
                # empty it in between. Device-measured 2026-08-28 over every
                # labelled gesture: swipes 1 press, double taps 2, no overlap.
                #
                # ...but only if it was empty long enough to be a lift. A slide
                # between pads leaves a hole of well under a millisecond, and
                # counting that turns a swipe into two taps.
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
        # Any edge keeps the current session alive — cross-talk and
        # FastMode auto-LOW produce flurries of edges per physical
        # touch; coalesce them by resetting the session-end timer.
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
        """Read the cycle as (is_swipe, moved, gaps, n_contacts, revisited, landed, presses).

        The discriminator is WHEN pads fire, not which. Device-measured on
        orange-lamp 2026-08-27, per-contact inter-pad deltas:

            fingers landing together   1, 2, 6, 17, 18, 18, 23 ms
            a finger moving           53 .. 322 ms

        Nothing between 23 and 53 — the two are cleanly separable, and
        SWIPE_MIN_GAP_MS sits in the gap. So a contact whose pads fired far
        apart in time is a hand that MOVED across the surface; a contact whose
        pads fired together is several fingers landing at once, however many
        pads that lights up.

        Two rules that both fall out of it:

        * **swipe** — one contact that moved, monotonically, across every pad.
        * **moved** — any contact that moved internally, or contacts that landed
          in disjoint places. Its negation is "the hand stayed put", which is
          what makes repeated contact a double tap rather than a stroke.

        An earlier version asked only whether a pad was common to every contact.
        That failed on this hardware because cross-talk makes almost every
        contact touch all three pads, so it could essentially never report
        movement — a full left-right-left stroke came back as a double tap.
        """
        with self._lock:
            contacts = [list(c) for c in self._contacts]
            if self._contact:
                contacts.append(list(self._contact))
        axis = self._axis or self._lines
        pos_of = {line: i for i, line in enumerate(axis)}

        def gaps_of(c):
            return [(b - a) * 1000.0 for (_, a), (_, b) in zip(c, c[1:])]

        # Reported on every trace, not only when a swipe was found — the whole
        # point of recording it is to see how near the floor a gesture landed.
        # Both ends: reporting only the minimum hid the asymmetric-landing bug
        # for a full round of testing, because the gap that mattered was the max.
        all_gaps = [x for c in contacts for x in gaps_of(c)]

        # Every pad-to-pad gap is one of two things, and that is the whole
        # classifier:
        #
        #   below the floor  the two pads lit at the same moment -- the hand
        #                    LANDED, covering both at once
        #   above the floor  the finger TRAVELLED from one pad to the other
        #
        # A stroke is one finger moving the whole time, so it contains no
        # landing. A double tap is land-lift-land, so it must contain at least
        # one. That is what separates them, and it does not care how many pads
        # each tap happened to light.
        #
        # This replaced a burst-clustering rule that grouped the steps and
        # required EVERY cluster to be multi-pad. It broke when the lamp went to
        # two pads: with the pads further apart one tap often lights only one of
        # them, so a cluster of size 1 appeared and the rule fell through to pet.
        # Device-measured 2026-08-28, three double taps in a row read as PET:
        #   L100@1 | L96@293 L100@300   -- first tap lit one pad, second lit two
        # Counting landings instead of clustering pads is geometry-independent.
        landed = any(
            (b - a) * 1000.0 < SWIPE_MIN_GAP_MS
            for c in contacts
            for (_, a), (_, b) in zip(c, c[1:])
        )

        sets_nonempty = [c for c in contacts if c]
        is_swipe = False
        moved_within = False
        # A stroke goes back over ground it already covered: more steps than
        # distinct pads means the finger returned to a pad it had left. A tap
        # and a swipe each touch a pad at most once. Device-measured on
        # orange-lamp 2026-08-27 — pets ran 5-8 steps over 3 pads, taps and
        # swipes exactly 3 over 3.
        revisited = any(len(c) > len({l for l, _ in c}) for c in contacts)
        for c in contacts:
            g = gaps_of(c)
            # Any pad-to-pad step slower than the floor means the hand travelled
            # between them rather than several fingers arriving at once.
            if any(x >= SWIPE_MIN_GAP_MS for x in g):
                moved_within = True
            positions = [pos_of[l] for l, _ in c if l in pos_of]
            pads = {l for l, _ in c}
            # EVERY wired pad, not a fixed count. A fixed floor of 2 would let
            # two of three pads pass on a 3-pad board, which is a partial move,
            # not a crossing — reaching both ends is the whole evidence that the
            # hand went end to end. Compared against the board's own line list
            # so the rule follows the hardware instead of a constant.
            if len(pads) < SWIPE_MIN_PADS or pads != set(self._lines):
                continue
            if len(positions) < SWIPE_MIN_PADS:
                continue
            deltas = [b - a for a, b in zip(positions, positions[1:]) if b != a]
            if not deltas or any((a > 0) != (b > 0) for a, b in zip(deltas, deltas[1:])):
                continue  # turned around — a stroke, not a single pass
            # A swipe is the WHOLE gesture, not a leg of one. A left-right-left
            # stroke contains a clean one-direction pass in nearly every
            # contact — device trace 160546 — so allowing any contact to carry
            # the verdict turns every pet into a swipe. One contact, one pass.
            #
            # ANY gap above the floor, not every gap. The surface is not
            # symmetric: starting a swipe from the right lands on L100 and L98
            # together (they are adjacent and cross-talk bridges them, 5-28ms)
            # and only then travels to L96 over 65-104ms. Requiring every gap to
            # clear the floor made right-to-left swipes impossible while
            # left-to-right worked — device traces 164757/164804/164807/164810.
            # "Did the hand travel at all" is also exactly what `moved` means
            # everywhere else in this classifier.
            # A gap inside the travel band, not merely above the floor. Below
            # the floor the pads lit together (a landing); above the ceiling the
            # hand lifted and came back (a second tap). Only the middle band is
            # a finger crossing the surface.
            if len(sets_nonempty) == 1 and self._presses <= 1 and any(
                SWIPE_MIN_GAP_MS <= x <= SWIPE_MAX_GAP_MS for x in g
            ):
                is_swipe = True

        sets = [{l for l, _ in c} for c in contacts if c]
        # Disjoint contacts are movement too, even when each one was a single
        # instantaneous touch — that is a hand hopping from pad to pad.
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
        # One physical touch ended.
        #
        # 1) If we're still inside the pet cooldown (user is mid-stroke,
        #    a head_pat fired recently), extend the cooldown and bail —
        #    don't count, don't fire. This prevents single_clicks from
        #    interleaving between pets during one continuous stroke.
        # 2) Otherwise increment the count. If it hits PET threshold,
        #    fire head_pat immediately and arm the cooldown.
        # 3) Else schedule the decision timer to classify accumulated
        #    sessions as a single tap when the user stops touching.
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
                # Still petting — swallow this session, extend cooldown.
                self._pet_cooldown_until = now + PET_COOLDOWN_S
                # Also cancel any pending decision_timer left over from
                # the pre-pet count: that count was already consumed
                # when pet fired, so no single_click should fire.
                if self._decision_timer is not None:
                    self._decision_timer.cancel()
                    self._decision_timer = None
                logger.debug("TTP223 session ignored (pet cooldown)")
                swallowed = True
                count = self._session_count
            else:
                self._session_count += 1
                count = self._session_count
                # Pet's fast path needs the hand to have MOVED between contacts,
                # and at least two of them. Gated on the SESSION count rather
                # than the number of contacts that recorded pads: a contact whose
                # touch edge fell the other side of a session boundary records
                # only a release and contributes nothing (device trace 154507).
                # Gated on >=2 at all because a single press's cross-talk
                # ordering can double back — device-measured, a one-contact trace
                # fired PET off nothing but noise.
                # A continuous stroke never lets the session lapse, so the whole
                # ~1s pet arrives as ONE contact — device-measured, five pets in
                # a row came back count=1 and fell through to TAP. A revisit is
                # therefore enough on its own; the contact count is not a gate.
                pet_now = SWIPE_ENABLED and not _landed and (
                    _revisited or (_moved and count >= PET_SESSION_THRESHOLD)
                )
                # First session of a burst: play the acknowledgement chime.
                # Checked outside the lock because it does I/O.
                grab_floor = count == 1
                logger.debug("TTP223 session ended (count=%d)", count)
                if SWIPE_ENABLED:
                    # Reversal is decidable the moment it happens and cannot
                    # later become a swipe, so pet keeps its fast path. Every
                    # other outcome (swipe, double tap, tap, count-fallback pet)
                    # needs to see the whole gesture, so it waits for the
                    # decision window. Checked outside the lock below.
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
            # Closed as its own cycle rather than folded into the pet that
            # armed the cooldown: one file per resolved outcome, and a stroke
            # that keeps extending the window would otherwise grow one file
            # without bound.
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
            # Same finish-before-dispatch rule as _dispatch: the trace closes
            # before the action can block or raise.
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
            # The decision timer outlived its count (pet consumed it inline).
            touch_debug.note_decision("NONE", "decision timer fired at count=0", count)
            touch_debug.finish("IGNORED-no_sessions")
            self._reset_cycle()
            return

        if SWIPE_ENABLED:
            # 1) SWIPE — one contact that ran cleanly across every pad in order.
            #    Checked first so a resolved swipe never also fires a tap.
            if is_swipe:
                self._dispatch(
                    "SWIPE", f"one contact traversed all pads, gaps {gaps[0]:.0f}-{gaps[1]:.0f}ms",
                    count, "head_pat_action", head_pat_action,
                )
                return

            # 2) DOUBLE TAP — the hand was on the same ground twice AND at some
            #    point two pads lit together, which only a landing produces. A
            #    stroke is travel throughout and can never satisfy this, so the
            #    check is safe to run before pet even though both revisit.
            # Two ways the hand tapped twice. Either it came back to a pad it
            # had left AND something landed (both taps on overlapping pads), or
            # it simply arrived on the surface more than once — which is what
            # two taps on DIFFERENT pads look like, with no revisit and no
            # landing to show for it. Device traces 131615 / 131625: tap L96,
            # lift, tap L100. Nothing but the press count separates that from a
            # slow swipe.
            if (revisited and landed) or (presses >= 2 and not revisited):
                self._dispatch(
                    "DOUBLE_TAP",
                    ("revisited a pad, and two pads lit together" if revisited
                     else f"the hand arrived on the surface {presses} times"),
                    count, "head_pat_action", head_pat_action,
                )
                return

            # 3) PET — went back over a pad it had left with NO landing on the
            #    way: every step was travel, which is what a stroke is. No
            #    contact-count gate: a continuous stroke is a single contact.
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

            # 4) DOUBLE TAP, slow — separate contacts that never revisited a pad
            #    and never moved: the hand stayed put. Deliberately NOT "only one pad
            #    touched": several fingers land on several pads at once, and
            #    requiring a single pad made this reachable only with a fingertip.
            if count >= PET_SESSION_THRESHOLD:
                self._dispatch(
                    "DOUBLE_TAP", f"{count} contacts, no revisit and no movement",
                    count, "head_pat_action", head_pat_action,
                )
                return

        # 5) TAP. Also the whole classifier when SWIPE_ENABLED is off, in which
        #    case count is 1 or 2 — 2 is tolerated because cross-talk
        #    occasionally splits one physical touch into two close sessions and
        #    treating both as one tap is friendlier than ignoring.
        #
        # The acknowledgement chime already sounded at the first contact.
        self._dispatch(
            "TAP", self._tap_reason(count),
            count, "head_pat_action", head_pat_action,
        )

    def _tap_reason(self, count):
        """Why this resolved to TAP — naming the rule that declined, not the
        branch that caught it.

        TAP is the fall-through after swipe, double tap and pet all decline, so
        a reason describing how it got here ("decision window expired at
        count=1") is true and useless: it is the one verdict where the reader
        most needs to know what was *nearly* matched. A swipe missing the
        movement floor by 1.1 ms read exactly the same as a deliberate tap
        (device traces 102922 / 102935, 2026-08-28), and the only way to tell
        was to open the file and know what `moved: False` implied.

        Names the nearest miss and the number to tune, so the answer is in the
        trace rather than in someone's head.
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
            wired = set(self._lines)
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
        """Record the verdict, CLOSE THE TRACE, then run the action.

        The classifier label remains in the trace; every action is a head pat.
        Close before dispatch so a slow or failed action cannot leave the
        trace unresolved.
        """
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
