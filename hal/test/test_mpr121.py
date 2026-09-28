"""MPR121 register setup, touch grouping, and worker lifecycle without hardware."""

import threading
import errno
import time
import unittest
from pathlib import Path
from unittest import mock

from hal.board.mpr121 import MPR121Config, load_mpr121_config
from hal.drivers.mpr121 import I2CBus, MPR121Handler, _GestureRecognizer, _GestureEvent


class TestGestures(unittest.TestCase):
    def events(self, samples, debounce_ms=0):
        detector = _GestureRecognizer(debounce_ms)
        return [event for sample in samples for event in detector.update(*sample)]

    def actions(self, events):
        return [event for event in events if event.kind in ('single', 'cue', 'triple', 'hold')]

    def test_one_two_three_four_taps(self):
        for count in range(1, 5):
            with self.subTest(count=count):
                samples = [(False, 0)]
                for index in range(count):
                    samples += [(True, 1 + index * .2), (False, 1.1 + index * .2)]
                samples += [(False, samples[-1][1] + .41)]
                actions = self.actions(self.events(samples))
                self.assertEqual([event.kind for event in actions], ['single', 'triple' if count == 3 else 'cue'])
                self.assertEqual(actions[-1].count, count)

    def test_exact_click_window_boundary(self):
        detector = _GestureRecognizer(0)
        for sample in [(False, 0), (True, .5), (False, 1)]:
            detector.update(*sample)
        self.assertEqual(detector.update(False, 1.399), [])
        self.assertEqual([event.kind for event in detector.update(False, 1.4)], ['cue'])
        events = detector.update(True, 1.5) + detector.update(False, 1.6) + detector.update(False, 2)
        self.assertEqual([event.kind for event in self.actions(events)], ['single', 'cue'])

    def test_hold_boundaries_and_no_action_while_held(self):
        for duration in (1.999, 2, 4.999, 5, 9.999, 10, 15):
            with self.subTest(duration=duration):
                detector = _GestureRecognizer(0)
                detector.update(False, 0)
                detector.update(True, 1)
                self.assertEqual(self.actions(detector.update(True, 1 + duration)), [])
                actions = self.actions(detector.update(False, 1 + duration))
                self.assertEqual([event.kind for event in actions], ['single' if duration < 2 else 'hold'])
                self.assertAlmostEqual(actions[0].held_s, duration)

    def test_hold_clears_pending_triple_and_cue(self):
        samples = [(False, 0)]
        for index in range(3):
            samples += [(True, 1 + index * .2), (False, 1.1 + index * .2)]
        samples += [(True, 1.7), (True, 4), (False, 4.7), (False, 6)]
        self.assertEqual([event.kind for event in self.actions(self.events(samples))], ['single', 'hold'])

    def test_chatter_does_not_count_or_change_duration(self):
        samples = [(False, 0), (True, 1), (False, 1.01), (False, 1.1),
                   (True, 2), (True, 2.04), (False, 3), (True, 3.01),
                   (True, 3.1), (False, 4), (False, 4.04), (False, 5)]
        actions = self.actions(self.events(samples, 30))
        self.assertEqual([event.kind for event in actions], ['hold'])
        self.assertEqual(actions[0].held_s, 2)

    def test_debounce_does_not_promote_short_hold(self):
        samples = [(False, 0), (True, 1), (True, 1.04), (False, 2.999), (False, 3.04)]
        actions = self.actions(self.events(samples, 30))
        self.assertEqual([event.kind for event in actions], ['single'])
        self.assertAlmostEqual(actions[0].held_s, 1.999)

    def test_boot_hold_suppressed_until_stable_release(self):
        samples = [(True, 0), (True, 10), (False, 11), (True, 11.01),
                   (False, 12), (False, 12.04), (True, 13), (True, 13.04),
                   (False, 13.2), (False, 13.24), (False, 14)]
        self.assertEqual([event.kind for event in self.actions(self.events(samples, 30))], ['single', 'cue'])

    def test_cancel_clears_pending_burst_and_hold(self):
        detector = _GestureRecognizer(0)
        for sample in [(False, 0), (True, 1), (False, 1.1)]:
            detector.update(*sample)
        detector.cancel()
        self.assertEqual(self.actions(detector.update(False, 10)), [])
        detector = _GestureRecognizer(0)
        detector.update(False, 0)
        detector.update(True, 1)
        detector.cancel()
        self.assertEqual(self.actions(detector.update(False, 20)), [])

    def test_hold_tiers_are_logged_once_per_threshold(self):
        events = self.events([(False, 0), (True, 1), (True, 3), (True, 4), (True, 6), (True, 11)])
        self.assertEqual([event.count for event in events if event.kind == 'hold_tier'], [1, 2])


class TestMPR121(unittest.TestCase):
    def setUp(self):
        mode = mock.patch("hal.drivers.harness.gestures.read_mode", return_value={"enabled": False, "generation": 1})
        mode.start()
        self.addCleanup(mode.stop)

    def make_handler(self, **kwargs):
        return MPR121Handler(MPR121Config(bus=5, **kwargs))

    def test_hold_feedback_follows_debounced_tiers_until_release(self):
        handler = self.make_handler()
        handler._hold_led = feedback = mock.Mock()
        for sample in [(False, 0), (True, 1), (True, 1.04),
                       (True, 2.999), (True, 3), (True, 6), (True, 11)]:
            handler._process_touch(*sample)
        self.assertEqual(feedback.set_tier.call_args_list,
                         [mock.call(1), mock.call(2)])
        feedback.commit.assert_not_called()
        feedback.release.reset_mock()
        handler._process_touch(False, 11.1)
        feedback.release.assert_not_called()
        handler._process_touch(False, 11.14)
        feedback.release.assert_called_once_with()
        feedback.commit.assert_not_called()

    def test_boot_held_and_chatter_do_not_arm_feedback(self):
        handler = self.make_handler()
        handler._hold_led = feedback = mock.Mock()
        for sample in [(True, 0), (True, 12), (False, 13), (False, 13.04),
                       (True, 14), (False, 14.01), (False, 20)]:
            handler._process_touch(*sample)
        feedback.set_tier.assert_not_called()
        feedback.commit.assert_not_called()

    def test_feedback_commits_before_shared_hold_action(self):
        handler = self.make_handler()
        handler._hold_led = feedback = mock.Mock()
        calls = mock.Mock()
        calls.attach_mock(feedback.commit, 'led')
        with mock.patch('hal.drivers.mpr121.hold_release_action') as hold:
            calls.attach_mock(hold, 'action')
            handler._execute(_GestureEvent('hold', 1, held_s=5))
        self.assertEqual(calls.mock_calls,
                         [mock.call.led(5, factory_reset=False), mock.call.action(5, source='MPR121')])

    def test_cancelled_feedback_commit_does_not_start_hold_action(self):
        handler = self.make_handler()
        handler._hold_led = mock.Mock()
        handler._hold_led.commit.return_value = False
        with mock.patch('hal.drivers.mpr121.hold_release_action') as hold:
            handler._execute(_GestureEvent('hold', 1, held_s=10))
        hold.assert_not_called()

    def test_stop_and_poll_fault_stop_feedback(self):
        for fault in (False, True):
            with self.subTest(fault=fault):
                handler = self.make_handler(poll_ms=1)
                handler._hold_led = feedback = mock.Mock()
                if fault:
                    with mock.patch.object(handler, '_read_touched', side_effect=OSError('disconnected')), \
                            self.assertLogs('hal.drivers.mpr121', level='ERROR'):
                        handler._poll()
                else:
                    handler.stop()
                feedback.stop.assert_called()

    def test_register_initialization(self):
        handler = self.make_handler()
        handler._bus = bus = mock.Mock()
        bus.read_regs.return_value = b'\x24'
        handler._initialize()
        calls = bus.write_reg.call_args_list
        self.assertEqual(calls[:2], [mock.call(0x5A, 0x80, 0x63), mock.call(0x5A, 0x5E, 0)])
        for electrode in range(12):
            self.assertIn(mock.call(0x5A, 0x41 + electrode * 2, 2), calls)
            self.assertIn(mock.call(0x5A, 0x42 + electrode * 2, 1), calls)
        for register, value in ((0x2F, 1), (0x30, 1), (0x31, 0xFF), (0x32, 0x02),
                                (0x33, 0), (0x34, 0), (0x35, 0),
                                (0x5B, 0), (0x5C, 0x10), (0x5D, 0x30),
                                (0x7D, 200), (0x7F, 180), (0x7E, 130), (0x7B, 0x0B)):
            self.assertIn(mock.call(0x5A, register, value), calls)
        self.assertEqual(calls[-1], mock.call(0x5A, 0x5E, 0x8F))

    def test_lamp_thresholds_are_loaded_and_written_to_chip(self):
        device_dir = Path(__file__).resolve().parents[2] / "robots" / "lamp"
        config = load_mpr121_config(device_dir, "orangepi_sun60")
        handler = MPR121Handler(config)
        handler._bus = bus = mock.Mock()
        bus.read_regs.return_value = b'\x24'
        handler._initialize()
        for electrode in range(12):
            self.assertIn(mock.call(0x5A, 0x41 + electrode * 2, 6), bus.write_reg.call_args_list)
            self.assertIn(mock.call(0x5A, 0x42 + electrode * 2, 3), bus.write_reg.call_args_list)

    def test_autoconfig_disabled(self):
        handler = self.make_handler(autoconfig=False)
        handler._bus = mock.Mock()
        handler._bus.read_regs.return_value = b'\x24'
        handler._initialize()
        self.assertFalse(any(call.args[1] == 0x7B for call in handler._bus.write_reg.call_args_list))

    def test_selected_electrodes_overlap_form_one_touch(self):
        handler = self.make_handler(electrodes=(0, 1))
        handler._bus = mock.Mock()
        # Electrode 2 is excluded. Overlapping 0 and 1 must not split the tap.
        masks = [4, 1, 1, 3, 2, 2, 4, 4]
        handler._bus.read_regs.side_effect = [mask.to_bytes(2, 'little') for mask in masks]
        times = [0, 1, 1.04, 2, 3, 3.04, 4, 4.04]
        events = [event for now in times for event in handler._detector.update(handler._read_touched(), now)]
        holds = [event for event in events if event.kind == 'hold']
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0].held_s, 3)

    def test_electrode_logs_include_changes_without_repeating_samples(self):
        handler = self.make_handler(electrodes=(0,))
        handler._bus = mock.Mock()
        handler._bus.read_regs.side_effect = [mask.to_bytes(2, 'little') for mask in (0, 1, 1, 3, 2)]
        with self.assertLogs('hal.drivers.mpr121', level='INFO') as logs:
            results = [handler._read_touched() for _ in range(5)]
        self.assertEqual(results, [False, True, True, True, False])
        self.assertEqual(len(logs.output), 4)
        self.assertIn('raw_mask=0x003 selected_mask=0x001 selected_active=0x001 touched=[1] released=[]', logs.output[2])
        self.assertIn('raw_mask=0x002 selected_mask=0x001 selected_active=0x000 touched=[] released=[0]', logs.output[3])

    def test_overcurrent_is_fault_not_release(self):
        handler = self.make_handler()
        handler._bus = mock.Mock()
        handler._bus.read_regs.return_value = b"\x00\x80"
        with self.assertRaisesRegex(OSError, "over-current"):
            handler._read_touched()

    def test_init_failure_closes_bus(self):
        handler = self.make_handler()
        bus = mock.Mock()
        bus.read_regs.return_value = b'\x00'
        with mock.patch('hal.drivers.mpr121.I2CBus', return_value=bus) as factory:
            with self.assertRaises(RuntimeError):
                handler.start()
        factory.assert_called_once_with(5)
        bus.close.assert_called_once()
        handler.stop()
        bus.close.assert_called_once()

    def test_missing_bus_warns_without_traceback_or_workers(self):
        handler = self.make_handler()
        error = FileNotFoundError(errno.ENOENT, 'No such file or directory', '/dev/i2c-5')
        with mock.patch('hal.drivers.mpr121.I2CBus', side_effect=error), \
                mock.patch('hal.drivers.mpr121.threading.Thread') as thread, \
                self.assertLogs('hal.drivers.mpr121', level='WARNING') as logs:
            with self.assertRaises(FileNotFoundError):
                handler.start()
        self.assertIn('event=unavailable bus=5 address=0x5a', logs.output[0])
        self.assertIsNone(logs.records[0].exc_info)
        self.assertEqual(logs.records[0].levelname, 'WARNING')
        self.assertTrue(handler._stop.is_set())
        self.assertIsNone(handler._bus)
        thread.assert_not_called()

    def test_absent_sensor_warns_and_closes_bus(self):
        for code in (errno.ENODEV, errno.ENXIO, 121):
            with self.subTest(errno=code):
                handler = self.make_handler()
                bus = mock.Mock()
                bus.write_reg.side_effect = OSError(code, 'Sensor unavailable')
                with mock.patch('hal.drivers.mpr121.I2CBus', return_value=bus), \
                        self.assertLogs('hal.drivers.mpr121', level='WARNING') as logs:
                    with self.assertRaises(OSError):
                        handler.start()
                self.assertIsNone(logs.records[0].exc_info)
                self.assertIn('event=unavailable', logs.output[0])
                self.assertTrue(handler._stop.is_set())
                handler.stop()
                bus.close.assert_called_once()

    def test_unexpected_startup_failure_retains_traceback(self):
        for error in (PermissionError(errno.EACCES, 'Permission denied'), RuntimeError('broken driver')):
            with self.subTest(error=error):
                handler = self.make_handler()
                with mock.patch('hal.drivers.mpr121.I2CBus', side_effect=error), \
                        self.assertLogs('hal.drivers.mpr121', level='ERROR') as logs:
                    with self.assertRaises(type(error)):
                        handler.start()
                self.assertIn('event=start_failed', logs.output[0])
                self.assertIsNotNone(logs.records[0].exc_info)

    def test_initial_touch_read_failure_closes_bus(self):
        handler = self.make_handler()
        bus = mock.Mock()
        bus.read_regs.side_effect = [b'\x24', OSError('touch read failed')]
        with mock.patch('hal.drivers.mpr121.I2CBus', return_value=bus), mock.patch('hal.drivers.mpr121.time.sleep'):
            with self.assertRaises(OSError):
                handler.start()
        bus.close.assert_called_once()

    def test_semantic_actions_call_shared_functions(self):
        handler = self.make_handler()
        handler._hold_led = mock.Mock()
        with mock.patch('hal.drivers.mpr121.single_click_action') as single, \
                mock.patch('hal.drivers.mpr121.announce_listening_cue') as cue, \
                mock.patch('hal.drivers.mpr121.triple_click_action') as triple, \
                mock.patch('hal.drivers.mpr121.hold_release_action') as hold:
            for event in [_GestureEvent('single', 1), _GestureEvent('cue', 1),
                          _GestureEvent('triple', 2, count=3), _GestureEvent('hold', 3, held_s=10)]:
                handler._execute(event)
        single.assert_called_once_with(source='MPR121', announce=False)
        cue.assert_called_once_with(source='MPR121')
        triple.assert_called_once_with(source='MPR121')
        hold.assert_called_once_with(10, source='MPR121')

    def test_worker_delivers_action_with_correlation_logs(self):
        handler = self.make_handler()
        called = threading.Event()
        with mock.patch('hal.drivers.mpr121.single_click_action', side_effect=lambda **kwargs: called.set()) as action, \
                self.assertLogs('hal.drivers.mpr121', level='INFO') as logs:
            handler._action_thread = threading.Thread(target=handler._dispatch)
            handler._action_thread.start()
            try:
                handler._process_touch(False, 0)
                handler._process_touch(True, 1)
                handler._process_touch(True, 1.04)
                handler._process_touch(False, 1.2)
                handler._process_touch(False, 1.24)
                self.assertTrue(called.wait(1))
            finally:
                handler.stop()
            action.assert_called_once_with(source='MPR121', announce=False)
        output = '\n'.join(logs.output)
        for event in ('action_queued', 'action_begin', 'action_complete'):
            self.assertIn(f'event={event} gesture_id=1 action=single', output)

    def test_poll_failure_closes_bus_and_stops_dispatch(self):
        handler = self.make_handler(poll_ms=1)
        bus = mock.Mock()
        bus.read_regs.side_effect = [b'\x24', b'\x00\x00', OSError('disconnected')]
        with mock.patch('hal.drivers.mpr121.I2CBus', return_value=bus):
            handler.start()
            self.assertTrue(handler._stop.wait(1))
            handler.stop()
        bus.close.assert_called_once()
        self.assertFalse(handler._poll_thread.is_alive())
        self.assertFalse(handler._action_thread.is_alive())

    def test_poll_fault_cancels_pending_gesture_and_actions(self):
        handler = self.make_handler(poll_ms=1, debounce_ms=0)
        handler._bus = bus = mock.Mock()
        handler._process_touch(False, 0)
        handler._process_touch(True, 1)
        handler._process_touch(False, 1.1)
        self.assertFalse(handler._pending.empty())
        with mock.patch.object(handler, '_read_touched', side_effect=OSError('disconnected')), \
                self.assertLogs('hal.drivers.mpr121', level='ERROR'):
            handler._poll()
        self.assertTrue(handler._stop.is_set())
        self.assertTrue(handler._pending.empty())
        self.assertEqual(handler._detector.update(False, 10), [])
        bus.close.assert_called_once()

    def test_stop_closes_bus_once(self):
        handler = self.make_handler()
        bus = mock.Mock()
        bus.read_regs.side_effect = lambda address, register, length: b'\x24' if register == 0x5D else b'\x00\x00'
        with mock.patch('hal.drivers.mpr121.I2CBus', return_value=bus):
            handler.start()
            handler.stop()
            handler.stop()
        bus.close.assert_called_once()

    def test_busy_worker_rejects_destructive_outcomes(self):
        for kind in ('hold', 'triple', 'swipe'):
            with self.subTest(kind=kind):
                handler = self.make_handler()
                handler._hold_led = feedback = mock.Mock()
                handler._action_busy = True
                handler._detector = mock.Mock()
                handler._detector.update.return_value = [_GestureEvent(kind, 1, held_s=10)]
                with self.assertLogs('hal.drivers.mpr121', level='WARNING') as logs:
                    handler._process_touch(False, 10)
                self.assertTrue(handler._pending.empty())
                self.assertIn('reason=action_worker_busy', logs.output[0])
                feedback.commit.assert_not_called()

    def test_new_touch_invalidates_pending_destructive_outcome(self):
        handler = self.make_handler()
        handler._process_touch(False, 0)
        handler._pending.put_nowait((handler._generation, _GestureEvent('hold', 1, held_s=10), time.monotonic()))
        handler._process_touch(True, 1)
        self.assertTrue(handler._pending.empty())

    def test_expired_destructive_outcome_never_executes(self):
        for kind in ("triple", "hold", "swipe"):
            handler = self.make_handler()
            handler._pending.put_nowait((handler._generation, _GestureEvent(kind, 1, count=3), time.monotonic() - 1))
            with mock.patch.object(handler, '_execute') as execute, \
                    self.assertLogs('hal.drivers.mpr121', level='INFO') as logs:
                handler._action_thread = threading.Thread(target=handler._dispatch)
                handler._action_thread.start()
                # Observing an empty queue means the worker has consumed the request;
                # stop then joins it before assertions about execution/logging.
                deadline = time.monotonic() + 1
                while not handler._pending.empty() and time.monotonic() < deadline:
                    threading.Event().wait(.001)
                handler.stop()
            execute.assert_not_called()
            self.assertTrue(any('reason=stale_expired_or_stopping' in item for item in logs.output))

    def test_stop_discards_pending_actions(self):
        for kind in ("triple", "hold", "swipe"):
            handler = self.make_handler()
            handler._pending.put_nowait((handler._generation, _GestureEvent(kind, 1, count=3), time.monotonic()))
            with mock.patch.object(handler, '_execute') as execute:
                handler.stop()
                handler._dispatch()
            self.assertTrue(handler._pending.empty())
            execute.assert_not_called()

    def test_queue_capacity_is_bounded_without_affecting_gesture_count(self):
        handler = self.make_handler()
        handler._detector = mock.Mock()
        handler._detector.update.return_value = [_GestureEvent('cue', index) for index in range(10)]
        with self.assertLogs('hal.drivers.mpr121', level='WARNING'):
            handler._process_touch(False, 1)
        self.assertEqual(handler._pending.qsize(), 2)

    def test_transport_close_is_idempotent(self):
        bus = I2CBus.__new__(I2CBus)
        bus.fd = 42
        with mock.patch('hal.drivers.mpr121.os.close') as close:
            bus.close()
            bus.close()
        close.assert_called_once_with(42)
