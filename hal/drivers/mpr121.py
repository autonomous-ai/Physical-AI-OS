"""Optional MPR121 capacitive input using shared GPIO button gestures.

I2C transport and register setup adapted from the user-supplied
mpr121_opi_test.py. Only the device-declared bus/address is accessed.
"""

import ctypes
import errno
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass

from hal.board.mpr121 import MPR121Config
from hal.drivers.button_gestures import (
    DOUBLE_CLICK_WINDOW, LONG_PRESS_DURATION, SLEEP_HOLD_DURATION,
)

logger = logging.getLogger(__name__)


class _I2CMsg(ctypes.Structure):
    _fields_ = [
        ("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16),
        ("len", ctypes.c_uint16), ("buf", ctypes.POINTER(ctypes.c_uint8)),
    ]


class _I2CData(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(_I2CMsg)), ("nmsgs", ctypes.c_uint32)]


class I2CBus:
    """Linux repeated-start I2C transport; importing this module needs no hardware."""

    def __init__(self, bus):
        self._libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self._libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
        self._libc.ioctl.restype = ctypes.c_int
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR | os.O_CLOEXEC)

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)

    def _transfer(self, messages):
        array = (_I2CMsg * len(messages))(*messages)
        data = _I2CData(msgs=array, nmsgs=len(messages))
        result = self._libc.ioctl(self.fd, 0x0707, ctypes.byref(data))
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        if result != len(messages):
            raise OSError("Incomplete MPR121 I2C transfer")

    def read_regs(self, address, register, length):
        pointer = (ctypes.c_uint8 * 1)(register)
        buffer = (ctypes.c_uint8 * length)()
        self._transfer([
            _I2CMsg(address, 0, 1, pointer),
            _I2CMsg(address, 1, length, buffer),
        ])
        return bytes(buffer)

    def write_reg(self, address, register, value):
        buffer = (ctypes.c_uint8 * 2)(register, value)
        self._transfer([_I2CMsg(address, 0, 2, buffer)])


@dataclass(frozen=True)
class _GestureEvent:
    kind: str
    gesture_id: int
    count: int = 0
    held_s: float = 0.0
    direction: int = 0


class _GestureRecognizer:
    """Poll-clock recognition independent of action execution and electrode count."""

    def __init__(self, debounce_ms, *, hold_thresholds=None, multi_click=True, hold_on_threshold=False):
        # Touch pads never factory-reset: that tier stays on the mechanical button.
        self._hold_thresholds = hold_thresholds or (SLEEP_HOLD_DURATION, LONG_PRESS_DURATION)
        self._multi_click = multi_click
        self._hold_on_threshold = hold_on_threshold
        self._delay = debounce_ms / 1000
        self._stable = None
        self._candidate = None
        self._since = 0.0
        self._armed = False
        self._press_start = None
        self._click_count = 0
        self._deadline = None
        self._gesture_id = 0
        self._hold_tier = 0

    def cancel(self):
        self._press_start = None
        self._click_count = 0
        self._deadline = None
        self._armed = False

    def update(self, touched, now):
        events = []
        if self._stable is None:
            self._stable = self._candidate = touched
            self._since = now
            self._armed = not touched
            logger.info("MPR121 event=detector_seed touched=%s armed=%s startup_hold_suppressed=%s", touched, self._armed, touched)
            return events
        if touched != self._candidate:
            self._candidate = touched
            self._since = now
            logger.debug("MPR121 event=debounce_candidate touched=%s", touched)
            if touched:
                # Cancel stale queued outcomes as soon as a new touch appears,
                # but never use raw chatter to increment the gesture count.
                events.append(_GestureEvent("invalidate", self._gesture_id))
        if touched != self._stable and now - self._since >= self._delay:
            self._stable = touched
            if touched and self._armed:
                if self._deadline is not None and self._since >= self._deadline:
                    logger.info("MPR121 event=burst_cancelled gesture_id=%d count=%d reason=new_touch_after_deadline", self._gesture_id, self._click_count)
                    self._click_count = 0
                    self._deadline = None
                if not self._click_count:
                    self._gesture_id += 1
                self._press_start = self._since
                self._hold_tier = 0
                events.append(_GestureEvent("press", self._gesture_id, self._click_count))
            elif not touched:
                if self._press_start is None:
                    logger.info("MPR121 event=stable_release startup_hold_suppressed=true")
                else:
                    # Measure between the first samples of the accepted edges;
                    # debounce must not push a just-short hold over a threshold.
                    held = self._since - self._press_start
                    events.append(_GestureEvent("release", self._gesture_id, self._click_count, held))
                    if held >= self._hold_thresholds[0]:
                        self._click_count = 0
                        self._deadline = None
                        events.append(_GestureEvent("invalidate", self._gesture_id))
                        events.append(_GestureEvent("hold", self._gesture_id, held_s=held))
                    else:
                        if not self._multi_click:
                            self._click_count = 0
                        self._click_count += 1
                        if self._click_count == 1:
                            events.append(_GestureEvent("single", self._gesture_id, self._click_count, held))
                        self._deadline = now + DOUBLE_CLICK_WINDOW if self._multi_click else None
                        if not self._multi_click:
                            self._click_count = 0
                    self._press_start = None
                self._armed = True
        if self._press_start is not None:
            held = (now if touched else self._since) - self._press_start
            tier = sum(held >= threshold for threshold in self._hold_thresholds)
            if tier > self._hold_tier:
                self._hold_tier = tier
                events.append(_GestureEvent("hold_tier", self._gesture_id, tier, held))
                if self._hold_on_threshold:
                    events.append(_GestureEvent("invalidate", self._gesture_id))
                    events.append(_GestureEvent("hold", self._gesture_id, held_s=held))
                    self.cancel()
        # A pending click window never commits a destructive outcome while
        # another electrode is held; a completed hold clears the whole burst.
        if not touched and not self._stable and self._deadline is not None and now >= self._deadline:
            kind = "triple" if self._click_count == 3 else "cue"
            events.append(_GestureEvent(kind, self._gesture_id, self._click_count))
            self._click_count = 0
            self._deadline = None
        return events


# The release grace joins a finger handoff without turning it into two taps.
SWIPE_RELEASE_S = 0.120
SWIPE_MAX_GAP_S = 0.150
# Travel time is measured from cycle start (after the contact debounce) to the
# last new pad, so a fast full-strip swipe (~90 ms raw on Lamp) measures ~50 ms.
# Displacement >= 3 pads already proves motion; this only rejects a whole hand
# landing within a poll or two.
SWIPE_MIN_TRAVEL_S = 0.030


class _SpatialGestureRecognizer:
    """Debounce electrode footprints before resolving travel versus a button."""

    def __init__(self, config, button_factory=None):
        self._button_factory = button_factory or _GestureRecognizer
        self._axis = config.swipe_axis
        self._selected = config.electrodes
        # A 30 ms per-pad filter erases measured 10-20 ms travel steps.
        # Require consecutive samples for footprints, while retaining the
        # configured union-contact debounce for button presses and boot holds.
        self._delay = min(config.debounce_ms / 1000, 0.005)
        self._contact_delay = config.debounce_ms / 1000
        self._raw_active = False
        self._contact_since = 0.0
        self._button = self._button_factory(0)
        self._raw = {}
        self._stable = set()
        self._seeded = False
        self._armed = False
        self._cycle = False
        self._release_at = None
        self._gesture_id = 0
        self._clear_cycle()

    def _clear_cycle(self):
        self._cycle = False
        self._seen = set()
        self._outside = set()
        self._previous_positions = set()
        self._origin = None
        self._last_center = None
        self._last_move = None
        self._started = None
        self._direction = 0
        self._peak = 0
        self._moving = False
        self._invalid = False
        self._release_at = None

    def cancel(self):
        self._button.cancel()
        self._clear_cycle()
        self._armed = False

    def _finish(self, now, active=False):
        events = []
        if self._moving:
            span = min(3, len(self._axis) - 1)
            valid = (not self._invalid and self._peak >= span
                     and self._last_move - self._started >= SWIPE_MIN_TRAVEL_S)
            logger.info("MPR121 event=swipe_resolved gesture_id=%d direction=%s displacement=%.2f valid=%s",
                        self._gesture_id, self._direction, self._peak, valid)
            if valid:
                events.append(_GestureEvent("swipe", self._gesture_id, direction=self._direction))
            # Restart the button detector only after the spatial cycle ends.
            self._button = self._button_factory(0)
            self._button.update(False, now)
        else:
            events.extend(self._button.update(False, self._release_at))
            if not active:
                events.extend(self._button.update(False, now))
        self._clear_cycle()
        return events

    def update(self, mask, now):
        active = {i for i in self._selected if mask & (1 << i)}
        if not self._seeded:
            self._seeded = True
            self._raw_active = bool(active)
            self._contact_since = now
            self._stable = active
            self._raw = {i: (i in active, now) for i in self._selected}
            self._armed = not active
            self._button.update(bool(active), now)
            return []
        events = []
        if bool(active) != self._raw_active:
            self._raw_active = bool(active)
            self._contact_since = now
            if active:
                events.append(_GestureEvent("invalidate", self._gesture_id))
        for i in self._selected:
            value = i in active
            previous, since = self._raw[i]
            if value != previous:
                self._raw[i] = (value, now)
                since = now
            if now - since >= self._delay:
                if value:
                    self._stable.add(i)
                else:
                    self._stable.discard(i)
        stable = self._stable
        if not self._armed:
            if not stable and not active and now - self._contact_since >= self._contact_delay:
                self._armed = True
                self._button = self._button_factory(0)
                self._button.update(False, now)
            return events
        # Resolve an old contact before looking at the next one. Raw contact
        # blocks release only inside the handoff grace, never indefinitely.
        if self._cycle and self._release_at is not None:
            if now - self._release_at >= SWIPE_RELEASE_S:
                events.extend(self._finish(now, bool(active)))
            elif stable:
                positions = {self._axis.index(i) for i in stable if i in self._axis}
                if self._contact_since - self._release_at < self._contact_delay:
                    self._release_at = None
                elif not self._moving and positions <= self._seen:
                    events.extend(self._finish(now, bool(active)))
                else:
                    self._release_at = None
        if stable:
            positions = {self._axis.index(i) for i in stable if i in self._axis}
            if not self._cycle:
                if not active or now - self._contact_since < self._contact_delay:
                    return events
                self._cycle = True
                self._gesture_id += 1
                self._started = self._last_move = now
                self._outside = stable - set(self._axis)
                self._seen = positions.copy()
                self._previous_positions = positions.copy()
                self._origin = sum(positions) / len(positions) if positions else None
                self._last_center = self._origin
            elif ((positions and self._origin is None)
                  or (self._origin is not None and stable - set(self._axis) - self._outside)):
                # Mixing an undeclared pad into a spatial contact cannot prove
                # a swipe, and must never extend it into a destructive hold.
                self._moving = self._invalid = True
                self._button.cancel()
                events += [_GestureEvent("invalidate", self._gesture_id),
                           _GestureEvent("release", self._gesture_id)]
            elif positions and self._origin is not None:
                center = sum(positions) / len(positions)
                arrivals = positions - self._previous_positions
                self._previous_positions = positions.copy()
                new = positions - self._seen
                displacement = center - self._origin
                if new:
                    # A far-off arrival is a second finger, not travel -- unless
                    # the contact is already moving and the jump continues in
                    # its direction: a fast swipe (~1 pad per poll) skips pads
                    # whose dwell is shorter than the footprint filter, so the
                    # centroid legitimately leaps 3-4 pads (measured on Lamp).
                    jump = center - self._last_center
                    if abs(jump) > 3 and not (self._moving and jump * self._direction > 0):
                        self._invalid = True
                    if now - self._last_move > SWIPE_MAX_GAP_S:
                        self._invalid = True
                    self._last_move = now
                    self._seen.update(positions)
                if new and abs(displacement) >= 1 and not self._moving:
                    self._moving = True
                    self._direction = 1 if displacement > 0 else -1
                    self._button.cancel()
                    events += [_GestureEvent("invalidate", self._gesture_id),
                               _GestureEvent("release", self._gesture_id)]
                if self._moving:
                    progress = displacement * self._direction
                    if arrivals and progress < self._peak - 1:
                        self._invalid = True
                    # Releases alone shift the centroid but prove no travel.
                    if new:
                        self._peak = max(self._peak, progress)
                self._last_center = center
            if not self._moving:
                edge_time = self._contact_since if self._button._press_start is None else now
                if not active:
                    edge_time = max(since for value, since in self._raw.values() if not value)
                button_events = self._button.update(True, edge_time)
                events.extend(button_events)
                if any(event.kind == "hold" for event in button_events):
                    # Threshold-fired actions consume this entire contact,
                    # including later travel, until a stable physical release.
                    self.cancel()
        elif self._cycle:
            if self._release_at is None:
                # Use raw edge time, so grace/debounce cannot promote a hold.
                self._release_at = max(since for value, since in self._raw.values() if not value)
        elif not active:
            events.extend(self._button.update(False, now))
        return events


def single_click_action(*, source, announce):
    from hal.drivers.button_actions import single_click_action as action
    action(source=source, announce=announce)


def announce_listening_cue(*, source):
    from hal.drivers.button_actions import announce_listening_cue as action
    action(source=source)


def triple_click_action(*, source):
    # Disabled for MPR121: accidental triple taps must not reboot the device.
    # Keep the shared action wiring here so it can be restored deliberately.
    # from hal.drivers.button_actions import triple_click_action as action
    # action(source=source)
    return


def hold_release_action(held_s, *, source):
    from hal.drivers.button_actions import hold_release_action as action
    action(held_s, source=source, factory_reset=False)


def swipe_action(*, source):
    from hal.drivers.button_actions import swipe_action as action
    action(source=source)


class MPR121Handler:
    def __init__(self, config: MPR121Config):
        self._config = config
        self._mask = sum(1 << electrode for electrode in set(config.electrodes))
        self._bus = None
        self._stop = threading.Event()
        self._pending = queue.Queue(maxsize=2)
        self._poll_thread = None
        self._action_thread = None
        self._harness_gestures = None
        self._mode_key = None
        self._detector = self._new_detector()
        self._last_raw_mask = None
        self._generation = 0
        self._action_busy = False
        self._gesture_lock = threading.Lock()
        self._hold_led = None

    def _new_detector(self):
        factory = _GestureRecognizer
        if self._harness_gestures and self._harness_gestures.snapshot.get("enabled"):
            from hal.drivers.harness.gestures import harness_button_recognizer
            factory = harness_button_recognizer
        if self._config.swipe_axis is not None:
            return _SpatialGestureRecognizer(self._config, factory)
        return factory(self._config.debounce_ms)

    def _sample(self):
        touched = self._read_touched()
        return self._last_raw_mask if self._config.swipe_axis is not None else touched

    def _feedback(self):
        with self._gesture_lock:
            if self._hold_led is None:
                from hal.drivers.button_actions import HoldLEDFeedback

                self._hold_led = HoldLEDFeedback()
            if self._stop.is_set():
                self._hold_led.stop()
            return self._hold_led

    def _initialize(self):
        config = self._config
        bus = self._bus

        def write(register, value):
            bus.write_reg(config.address, register, value)

        write(0x80, 0x63)
        time.sleep(0.001)
        write(0x5E, 0x00)
        if bus.read_regs(config.address, 0x5D, 1) != b"\x24":
            raise RuntimeError("MPR121 CONFIG2 reset value is not 0x24")
        for electrode in range(12):
            write(0x41 + 2 * electrode, config.touch_threshold)
            write(0x42 + 2 * electrode, config.release_threshold)
        # Slow falling baseline tracking using NXP AN3944 quick-start values
        # so the baseline does not quickly follow an approaching finger.
        for register, value in (
            (0x2B, 1), (0x2C, 1), (0x2D, 14), (0x2E, 0),
            (0x2F, 1), (0x30, 1), (0x31, 0xFF), (0x32, 0x02),
            (0x33, 0), (0x34, 0), (0x35, 0),
            # CONFIG2: CDT 0.5 us, SFI 10 samples, ESI 1 ms. The 10-sample
            # second-level filter halves idle noise versus 4 samples and
            # updates every ~10 ms, matching the poll period. Chip debounce
            # stays 0: contact/footprint debounce is done in software.
            (0x5B, 0), (0x5C, 0x10), (0x5D, 0x30),
        ):
            write(register, value)
        if config.autoconfig:
            for register, value in ((0x7D, 200), (0x7F, 180), (0x7E, 130), (0x7B, 0x0B)):
                write(register, value)
        write(0x5E, 0x8F)

    def _read_touched(self):
        data = self._bus.read_regs(self._config.address, 0x00, 2)
        if len(data) != 2:
            raise OSError("Incomplete MPR121 touch status")
        status = int.from_bytes(data, "little")
        if status & 0x8000:
            raise OSError("MPR121 over-current fault")
        raw_mask = status & 0x0FFF
        if raw_mask != self._last_raw_mask:
            previous = self._last_raw_mask or 0
            touched = [i for i in range(12) if raw_mask & ~previous & (1 << i)]
            released = [i for i in range(12) if previous & ~raw_mask & (1 << i)]
            logger.info(
                "MPR121 event=electrodes initial=%s raw_mask=0x%03x selected_mask=0x%03x selected_active=0x%03x touched=%s released=%s",
                self._last_raw_mask is None, raw_mask, self._mask,
                raw_mask & self._mask, touched, released,
            )
            self._last_raw_mask = raw_mask
        return bool(raw_mask & self._mask)

    def start(self):
        if any(thread and thread.is_alive() for thread in (self._poll_thread, self._action_thread)):
            raise RuntimeError("MPR121 handler already running or stopping")
        logger.info(
            "MPR121 event=start bus=%d address=0x%02x electrodes=%s touch_threshold=%d release_threshold=%d autoconfig=%s poll_ms=%d debounce_ms=%d settle_ms=100 pending_capacity=2",
            self._config.bus, self._config.address, self._config.electrodes,
            self._config.touch_threshold, self._config.release_threshold,
            self._config.autoconfig, self._config.poll_ms, self._config.debounce_ms,
        )
        logger.info("MPR121 event=swipe_config axis=%s release_ms=120 max_gap_ms=150 min_travel_ms=30 footprint_debounce_ms=5", self._config.swipe_axis)
        self._last_raw_mask = None
        if self._hold_led is not None:
            self._hold_led.stop()
        self._hold_led = None
        self._stop.clear()
        self._pending = queue.Queue(maxsize=2)
        self._detector = self._new_detector()
        try:
            self._bus = I2CBus(self._config.bus)
            self._initialize()
            # Allow conversions/autoconfiguration to settle before seeding the
            # boot-held suppression from the first reported electrode state.
            time.sleep(0.1)
            from hal.drivers.harness.gestures import HarnessGestures
            self._harness_gestures = HarnessGestures()
            self._harness_gestures.start()
            self._detector.update(self._sample(), time.monotonic())
            self._action_thread = threading.Thread(target=self._dispatch, daemon=True, name="mpr121-actions")
            self._poll_thread = threading.Thread(target=self._poll, daemon=True, name="mpr121-poll")
            self._action_thread.start()
            self._poll_thread.start()
        except Exception as exc:
            # Optional hardware may be declared before it is installed. Keep
            # permission, configuration and unexpected failures diagnosable.
            # Linux reports a non-acknowledging I2C slave as ENXIO or EREMOTEIO.
            if isinstance(exc, OSError) and exc.errno in {
                errno.ENOENT, errno.ENODEV, errno.ENXIO, 121,  # Linux EREMOTEIO
            }:
                logger.warning(
                    "MPR121 event=unavailable bus=%d address=0x%02x errno=%d reason=%s; check I2C bus and wiring, then restart HAL",
                    self._config.bus, self._config.address, exc.errno, exc,
                )
            else:
                logger.exception("MPR121 event=start_failed")
            self._stop.set()
            if self._harness_gestures is not None:
                self._harness_gestures.stop()
            if self._hold_led is not None:
                self._hold_led.stop()
            self._close_bus()
            raise
        logger.info("MPR121 ready on i2c-%d address 0x%02x electrodes %s", self._config.bus, self._config.address, self._config.electrodes)

    def _close_bus(self):
        if self._bus is not None:
            bus, self._bus = self._bus, None
            try:
                bus.close()
                logger.info("MPR121 event=bus_closed bus=%d", self._config.bus)
            except OSError:
                logger.exception("MPR121 bus close failed")

    def _invalidate_pending(self, reason):
        with self._gesture_lock:
            self._generation += 1
            while True:
                try:
                    _, event, _ = self._pending.get_nowait()
                except queue.Empty:
                    break
                logger.info("MPR121 event=action_discarded gesture_id=%d action=%s reason=%s", event.gesture_id, event.kind, reason)

    def _process_touch(self, touched, now):
        if self._harness_gestures:
            key = self._harness_gestures.mode_key()
            if key != self._mode_key:
                self._mode_key = key
                self._invalidate_pending("harness_mode_changed")
                if self._hold_led is not None:
                    self._hold_led.release()
                self._detector = self._new_detector()
                self._detector.update(touched, now)
                return
            if self._harness_gestures.snapshot.get("unavailable"):
                return
        for event in self._detector.update(touched, now):
            if self._stop.is_set():
                return
            logger.info("MPR121 event=gesture kind=%s gesture_id=%d count=%d held_s=%.3f direction=%d", event.kind, event.gesture_id, event.count, event.held_s, event.direction)
            if event.kind == "invalidate":
                self._invalidate_pending("new_touch_or_hold")
            elif event.kind == "hold_tier":
                if not (self._harness_gestures and self._harness_gestures.snapshot.get("enabled")):
                    self._feedback().set_tier(event.count)
            elif event.kind == "release":
                if self._hold_led is not None:
                    self._hold_led.release()
            elif event.kind in ("single", "cue", "triple", "hold", "swipe"):
                with self._gesture_lock:
                    if self._stop.is_set():
                        return
                    if event.kind in ("hold", "triple", "swipe") and (self._action_busy or not self._pending.empty()):
                        logger.warning("MPR121 event=action_discarded gesture_id=%d action=%s reason=action_worker_busy", event.gesture_id, event.kind)
                        continue
                    try:
                        self._pending.put_nowait((self._generation, event, time.monotonic()))
                        logger.info("MPR121 event=action_queued gesture_id=%d action=%s", event.gesture_id, event.kind)
                    except queue.Full:
                        # Counts are resolved from every electrode edge above;
                        # dropping a semantic outcome never invents a triple.
                        logger.warning("MPR121 event=action_discarded gesture_id=%d action=%s reason=pending_queue_full", event.gesture_id, event.kind)

    def _poll(self):
        logger.info("MPR121 event=worker_started worker=poll")
        try:
            while not self._stop.wait(self._config.poll_ms / 1000):
                self._process_touch(self._sample(), time.monotonic())
        except Exception:
            logger.exception("MPR121 polling stopped after hardware error")
            self._stop.set()
        finally:
            self._detector.cancel()
            if self._hold_led is not None:
                self._hold_led.stop()
            self._invalidate_pending("poll_stopped")
            self._close_bus()
            logger.info("MPR121 event=worker_stopped worker=poll")

    def _execute_swipe(self, direction):
        """The declared axis runs left to right; route one resolved swipe."""
        if self._hold_led is not None and self._hold_led.commit(0) is False:
            return
        if direction == 1:
            swipe_action(source="MPR121")
        elif direction == -1:
            from hal.drivers.harness.actions import toggle_harness_voice
            toggle_harness_voice()
        else:
            logger.warning("MPR121 swipe discarded: missing or invalid direction %s", direction)

    def _execute(self, event):
        if self._harness_gestures:
            if not self._harness_gestures.refresh_matches(self._mode_key):
                return
            if self._harness_gestures.snapshot.get("enabled"):
                self._harness_gestures.execute(event)
                return
        if event.kind == "single":
            single_click_action(source="MPR121", announce=False)
        elif event.kind == "cue":
            announce_listening_cue(source="MPR121")
        elif event.kind == "triple":
            triple_click_action(source="MPR121")
        elif event.kind == "swipe":
            self._execute_swipe(event.direction)
        elif event.kind == "hold":
            if self._feedback().commit(event.held_s, factory_reset=False) is False:
                logger.info("MPR121 event=action_discarded gesture_id=%d action=hold reason=feedback_cancelled", event.gesture_id)
                return
            hold_release_action(event.held_s, source="MPR121")

    def _dispatch(self):
        logger.info("MPR121 event=worker_started worker=action")
        try:
            while not self._stop.is_set():
                try:
                    generation, event, queued_at = self._pending.get(timeout=0.1)
                except queue.Empty:
                    continue
                with self._gesture_lock:
                    valid = not self._stop.is_set() and generation == self._generation
                    if event.kind in ("hold", "triple", "swipe") and time.monotonic() - queued_at > DOUBLE_CLICK_WINDOW:
                        valid = False
                    if valid:
                        self._action_busy = True
                if not valid:
                    logger.info("MPR121 event=action_discarded gesture_id=%d action=%s reason=stale_expired_or_stopping", event.gesture_id, event.kind)
                    continue
                try:
                    logger.info("MPR121 event=action_begin gesture_id=%d action=%s count=%d held_s=%.3f", event.gesture_id, event.kind, event.count, event.held_s)
                    # Once a shared action starts its own I/O/OS sequence, it
                    # cannot be interrupted here. Only pending work is canceled.
                    self._execute(event)
                    logger.info("MPR121 event=action_complete gesture_id=%d action=%s", event.gesture_id, event.kind)
                except Exception:
                    logger.exception("MPR121 event=action_failed gesture_id=%d action=%s", event.gesture_id, event.kind)
                finally:
                    with self._gesture_lock:
                        self._action_busy = False
        finally:
            if self._harness_gestures is not None:
                self._harness_gestures.stop()
            self._invalidate_pending("action_worker_stopped")
            logger.info("MPR121 event=worker_stopped worker=action")

    def stop(self):
        logger.info("MPR121 event=stop_requested")
        self._stop.set()
        if self._harness_gestures is not None:
            self._harness_gestures.stop()
        if self._hold_led is not None:
            self._hold_led.stop()
        self._invalidate_pending("stop_requested")
        for thread in (self._poll_thread, self._action_thread):
            if thread is not None and thread.ident is not None:
                thread.join(timeout=2)
                if thread.is_alive():
                    logger.warning("MPR121 worker %s still stopping", thread.name)
        # The polling thread owns the bus after start, so never close its fd
        # underneath an in-flight ioctl. Its finally block closes it on exit.
