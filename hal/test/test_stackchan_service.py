"""Focused contract tests for the Stack-chan Wi-Fi motion driver."""
from __future__ import annotations

import threading
import time
import unittest
import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from websockets.sync.client import connect

from hal.drivers.motors.base import MotionService
from hal.drivers.motors.stackchan_service import (
    JOINT_KEYS,
    StackChanMotionService,
    StackChanTransportError,
    _BodyTransport,
    _BodyGateway,
    StackChanCommandRejected,
    StackChanTargetNotReached,
    _DeviceConnection,
    StackChanOffline,
)


TOKEN = "stackchan-test-token-that-is-long-enough"


class _FakeServiceTransport:
    def __init__(self):
        self.positions = {"base_yaw.pos": 0.0, "base_pitch.pos": 0.0}
        self.moves = []
        self.halts = 0
        self.releases = 0
        self.release_error = None

    def move(self, native, duration):
        self.moves.append((native, duration))
        if "yawServo" in native:
            self.positions["base_yaw.pos"] = native["yawServo"]["angle"] / 10.0
        if "pitchServo" in native:
            self.positions["base_pitch.pos"] = native["pitchServo"]["angle"] / 10.0 - 45.0

    def get_positions(self):
        return dict(self.positions)

    def halt(self):
        self.halts += 1

    def release(self, native_rest, duration):
        self.releases += 1
        self.release_rest = (native_rest, duration)
        if self.release_error:
            raise self.release_error


class TestStackChanMotionService(unittest.TestCase):
    def setUp(self):
        self.service = StackChanMotionService(
            device_id="stackchan-test",
            token=TOKEN,
            port=8765,
            allow_insecure_ws=True,
        )
        self.transport = _FakeServiceTransport()
        self.service._transport = self.transport

    def test_joint_mapping_and_timed_move(self):
        self.service.move_to(
            {"base_yaw.pos": -12.3, "base_pitch.pos": 4.5},
            duration=1.25,
        )

        native, duration = self.transport.moves[-1]
        self.assertEqual(duration, 1.25)
        self.assertEqual(native, {
            "yawServo": {"angle": -123, "speed": 1000},
            "pitchServo": {"angle": 495, "speed": 1000},
        })
        self.assertEqual(self.service.get_positions(), {
            "base_yaw.pos": -12.3,
            "base_pitch.pos": 4.5,
        })

    def test_rejects_unknown_non_finite_and_out_of_range_joints(self):
        bad = (
            {"elbow.pos": 1.0},
            {"base_yaw.pos": float("nan")},
            {"base_yaw.pos": 30.1},
            {"base_pitch.pos": -15.1},
        )
        for positions in bad:
            with self.subTest(positions=positions), self.assertRaises(ValueError):
                self.service.move_to(positions)
        self.assertEqual(self.transport.moves, [])

    def test_nudge_stretches_duration_to_safety_speed(self):
        policy = SimpleNamespace(motion=SimpleNamespace(max_speed=10))
        result = self.service.nudge(
            yaw=20,
            pitch=0,
            duration=0.1,
            current_positions={"base_yaw.pos": 0.0, "base_pitch.pos": 0.0},
            safety_policy=policy,
        )
        self.assertEqual(result["base_yaw.pos"], 20.0)
        self.assertEqual(self.transport.moves[-1][1], 2.0)

    def test_modes_halt_release_and_resume(self):
        self.service.hold(explicit=True)
        self.assertEqual(self.service.motion_mode, "hold")
        self.assertEqual(self.transport.halts, 1)

        self.assertEqual(self.service.release(), {})
        self.assertEqual(self.service.motion_mode, "released")
        self.assertEqual(self.transport.release_rest, ({
            "yawServo": {"angle": 0, "speed": 1000},
            "pitchServo": {"angle": 300, "speed": 1000},
        }, 2.0))
        self.service.resume()
        self.assertIsNone(self.service.motion_mode)
        self.assertEqual(len(self.transport.moves), 1)

    def test_release_reports_transport_error(self):
        self.transport.release_error = StackChanTransportError("release failed")
        self.assertEqual(self.service.release(), {"stackchan": "release failed"})
        self.assertIsNone(self.service.motion_mode)

    def test_halt_during_release_does_not_report_torque_released(self):
        gateway = _FakeGateway()
        self.service._transport = _BodyTransport(
            gateway,
            timeout=1.0,
            lease_ttl_ms=1000,
        )
        results = []

        thread = threading.Thread(target=lambda: results.append(self.service.release()))
        thread.start()
        self.assertTrue(gateway.connection_value.motion_started.wait(0.5))
        self.service.halt()
        thread.join(0.5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [{"stackchan": "release was halted before torque-off"}])
        self.assertIsNone(self.service.motion_mode)
        operations = [message["op"] for message in gateway.connection_value.messages]
        self.assertNotIn("motion.release", operations)
        self.assertEqual(operations[-1], "motion.halt")

    def test_protocol_surface_is_complete(self):
        required = {
            "start", "stop", "is_connected", "dispatch", "get_available_recordings",
            "add_recording", "ensure_running", "is_suppressed", "motion_mode",
            "freeze", "unfreeze", "is_frozen", "acquire_body", "release_body",
            "move_to", "move_and_hold",
            "get_joint_names", "get_positions", "send_positions", "zero_pose",
            "release", "halt", "resume", "hold", "joint_status", "aim", "nudge",
        }
        self.assertEqual(self.service.get_joint_names(), JOINT_KEYS)
        self.assertTrue(all(hasattr(self.service, member) for member in required))
        self.assertIsInstance(self.service, MotionService)
        self.assertIsNone(self.service._current_recording)

    def test_tracking_flag_and_capture_ownership_are_independent(self):
        self.service.acquire_body()
        self.service.acquire_body()
        self.service._tracking_active = True
        self.service._tracking_active = False
        self.service.release_body()
        self.assertTrue(self.service._tracking_active)
        self.service.release_body()
        self.assertFalse(self.service._tracking_active)

        self.service._tracking_active = True
        self.service.acquire_body()
        self.service.release_body()
        self.assertTrue(self.service._tracking_active)
        self.service._tracking_active = False
        self.assertFalse(self.service._tracking_active)

        # An unmatched release must not consume the next owner's claim.
        self.service.release_body()
        self.service.acquire_body()
        self.assertTrue(self.service._tracking_active)
        self.service.release_body()
        self.assertFalse(self.service._tracking_active)

    def test_overlapping_look_scopes_keep_body_until_last_owner_exits(self):
        import hal.app_state as state
        from hal.drivers.tracking.aim import servo_ownership

        with patch.object(state, "animation_service", self.service):
            with ExitStack() as first_look, ExitStack() as second_look:
                first_look.enter_context(servo_ownership())
                second_look.enter_context(servo_ownership())
                self.assertTrue(self.service._tracking_active)
                first_look.close()
                self.assertTrue(self.service._tracking_active)
            self.assertFalse(self.service._tracking_active)

    def test_failed_look_releases_body_ownership(self):
        import hal.app_state as state
        from hal.drivers.tracking.aim import servo_ownership

        with patch.object(state, "animation_service", self.service):
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                with servo_ownership():
                    self.assertTrue(self.service._tracking_active)
                    raise RuntimeError("capture failed")
        self.assertFalse(self.service._tracking_active)

    def test_body_owner_can_move_and_halt_without_losing_its_claim(self):
        self.service.acquire_body()
        try:
            self.service.move_to({"base_yaw.pos": 5.0}, duration=0.5)
            self.service.halt()
            self.assertEqual(len(self.transport.moves), 1)
            self.assertEqual(self.transport.halts, 1)
            self.assertTrue(self.service._tracking_active)
        finally:
            self.service.release_body()
        self.assertFalse(self.service._tracking_active)

    def test_generic_servo_state_route_accepts_stackchan_service(self):
        import hal.app_state as state
        from hal.routes.servo import get_servo_state

        previous = state.animation_service
        state.animation_service = self.service
        try:
            self.assertEqual(get_servo_state(), {
                "available_recordings": [],
                "current": None,
                "motion_mode": None,
            })
        finally:
            state.animation_service = previous

    def test_real_websocket_handshake_controls_connection_state(self):
        service = StackChanMotionService(
            device_id="stackchan-test",
            token=TOKEN,
            host="127.0.0.1",
            port=0,
            allow_insecure_ws=True,
        )
        service.start()
        try:
            port = service._server.socket.getsockname()[1]
            with connect(
                f"ws://127.0.0.1:{port}/stackchan/body/v1",
                additional_headers={"Authorization": f"Bearer {TOKEN}"},
            ) as socket:
                socket.send(json.dumps({
                    "v": 1,
                    "type": "hello",
                    "device_id": "stackchan-test",
                    "capabilities": [
                        "motion.pan_tilt",
                        "motion.measured_position",
                        "motion.halt_hold",
                        "motion.torque_release",
                        "motion.timed_move",
                    ],
                }))
                accepted = json.loads(socket.recv(timeout=1))
                self.assertEqual(accepted["type"], "hello.accepted")
                self.assertTrue(service.is_connected)
            deadline = time.monotonic() + 1
            while service.is_connected and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(service.is_connected)
        finally:
            service.stop()


class _FakeConnection:
    def __init__(self):
        self.messages = []
        self.close_reasons = []
        self.motion_started = threading.Event()
        self.positions = {"pan": 2.0, "tilt": -3.0}
        self.apply_moves = True
        self.lease_active = False

    def request(self, message, timeout):
        del timeout
        self.messages.append(message)
        operation = message["op"]
        if operation.startswith("motion.") and operation != "motion.get":
            if "seq" not in message:
                raise AssertionError(f"{operation} requires seq")
        if operation == "lease.acquire":
            self.lease_active = True
        elif operation == "lease.release":
            if not self.lease_active:
                raise AssertionError("lease.release requires an active lease")
            self.lease_active = False
        elif operation in {"motion.halt", "motion.release"}:
            if not self.lease_active:
                raise AssertionError(f"{operation} requires an active lease")
            self.lease_active = False
        if operation == "motion.move":
            self.motion_started.set()
            if self.apply_moves:
                motion = message["args"]["motion"]
                if "yawServo" in motion:
                    self.positions["pan"] = motion["yawServo"]["angle"] / 10.0
                if "pitchServo" in motion:
                    self.positions["tilt"] = motion["pitchServo"]["angle"] / 10.0 - 45.0
        if operation == "motion.get":
            return {
                "status": "completed",
                "result": {"positions": dict(self.positions)},
            }
        return {"status": "completed", "result": {}}

    def force_close(self, reason):
        self.close_reasons.append(reason)


class _FakeGateway:
    def __init__(self):
        self.connection_value = _FakeConnection()
        self.closed = []

    def connection(self):
        return self.connection_value

    def close_connection(self, reason):
        self.closed.append(reason)


class TestBodyTransport(unittest.TestCase):
    def test_timed_move_renews_then_releases_lease(self):
        gateway = _FakeGateway()
        transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=250)
        transport.move({"yawServo": {"angle": 10, "speed": 1000}}, duration=0.16)

        operations = [message["op"] for message in gateway.connection_value.messages]
        self.assertEqual(operations, [
            "lease.acquire", "motion.move", "lease.renew", "motion.get", "lease.release",
        ])
        move = gateway.connection_value.messages[1]
        self.assertEqual(move["args"]["duration_ms"], 160)
        self.assertEqual(move["seq"], 1)

    def test_move_fails_when_measured_target_is_not_reached(self):
        gateway = _FakeGateway()
        gateway.connection_value.apply_moves = False
        transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=1000)
        transport._target_settle_timeout = 0.01
        with self.assertRaisesRegex(StackChanTransportError, "measured target"):
            transport.move({"yawServo": {"angle": 100, "speed": 1000}}, 0.05)
        operations = [m["op"] for m in gateway.connection_value.messages]
        self.assertIn("motion.get", operations)
        self.assertIn("motion.halt", operations)
        self.assertNotIn("lease.release", operations)
        # The halt succeeded, so the body is held; closing would only reboot it.
        self.assertEqual(gateway.connection_value.close_reasons, [])

    def test_move_renews_lease_until_delayed_firmware_reaches_target(self):
        gateway = _FakeGateway()
        gateway.connection_value.apply_moves = False
        original = gateway.connection_value.request
        reads = []

        def request(message, timeout):
            if message["op"] == "motion.get":
                reads.append(message)
                if len(reads) == 2:
                    gateway.connection_value.positions["pan"] = 10.0
            return original(message, timeout)

        transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=1000)
        with patch.object(gateway.connection_value, "request", side_effect=request):
            transport.move({"yawServo": {"angle": 100, "speed": 1000}}, 0.05)
        self.assertEqual([m["op"] for m in gateway.connection_value.messages], [
            "lease.acquire", "motion.move", "motion.get", "lease.renew",
            "motion.get", "lease.release",
        ])
        self.assertEqual(gateway.closed, [])

    def test_halt_interrupts_move_without_waiting_for_duration(self):
        gateway = _FakeGateway()
        transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=1000)
        errors = []

        def run_move():
            try:
                transport.move({"yawServo": {"angle": 10, "speed": 1000}}, duration=2.0)
            except Exception as exc:  # pragma: no cover - failure is asserted below
                errors.append(exc)

        thread = threading.Thread(target=run_move)
        started = time.monotonic()
        thread.start()
        self.assertTrue(gateway.connection_value.motion_started.wait(0.5))
        transport.halt()
        thread.join(0.5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertLess(time.monotonic() - started, 1.0)
        operations = [message["op"] for message in gateway.connection_value.messages]
        self.assertIn("motion.halt", operations)
        self.assertNotIn("lease.release", operations)

    def test_release_supersedes_move_and_reaches_rest_before_torque_off(self):
        gateway = _FakeGateway()
        transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=1000)
        move_errors = []

        def run_move():
            try:
                transport.move(
                    {"yawServo": {"angle": 10, "speed": 1000}},
                    duration=2.0,
                )
            except Exception as exc:  # pragma: no cover - asserted below
                move_errors.append(exc)

        thread = threading.Thread(target=run_move)
        thread.start()
        self.assertTrue(gateway.connection_value.motion_started.wait(0.5))
        rest = {
            "yawServo": {"angle": 0, "speed": 1000},
            "pitchServo": {"angle": 300, "speed": 1000},
        }
        transport.release(rest, duration=0.05)
        thread.join(0.5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(move_errors, [])
        operations = [message["op"] for message in gateway.connection_value.messages]
        self.assertEqual(operations.count("motion.move"), 2)
        self.assertNotIn("lease.release", operations)
        self.assertEqual(operations[-1], "motion.release")
        self.assertIn("seq", gateway.connection_value.messages[-1])
        rest_move = [
            message for message in gateway.connection_value.messages
            if message["op"] == "motion.move"
        ][-1]
        self.assertEqual(rest_move["args"]["motion"], rest)

    def test_release_holds_torque_when_measured_rest_is_not_reached(self):
        gateway = _FakeGateway()
        gateway.connection_value.apply_moves = False
        transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=1000)
        transport._target_settle_timeout = 0.01
        rest = {
            "yawServo": {"angle": 0, "speed": 1000},
            "pitchServo": {"angle": 300, "speed": 1000},
        }

        closed = []
        with patch.object(gateway.connection_value, "force_close", side_effect=closed.append):
            with self.assertRaisesRegex(StackChanTransportError, "measured target"):
                transport.release(rest, duration=0.05)

        # HAL halted the short rest move and torque is held; no close.
        self.assertEqual(closed, [])
        self.assertEqual(gateway.closed, [])
        operations = [message["op"] for message in gateway.connection_value.messages]
        self.assertNotIn("motion.release", operations)
        self.assertIn("motion.halt", operations)

    def test_halt_cannot_return_before_a_concurrent_move_publishes_ownership(self):
        gateway = _FakeGateway()
        transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=1000)
        transport._active_lock.acquire()
        move_errors = []
        halt_errors = []

        def run_move():
            try:
                transport.move(
                    {"yawServo": {"angle": 10, "speed": 1000}},
                    duration=2.0,
                )
            except Exception as exc:  # pragma: no cover - asserted below
                move_errors.append(exc)

        def run_halt():
            try:
                transport.halt()
            except Exception as exc:  # pragma: no cover - asserted below
                halt_errors.append(exc)

        move_thread = threading.Thread(target=run_move)
        halt_thread = threading.Thread(target=run_halt)
        move_thread.start()
        # Both operations must wait at the ownership boundary. Releasing it
        # allows exactly one to publish or claim the controller first.
        halt_thread.start()
        time.sleep(0.02)
        transport._active_lock.release()

        halt_thread.join(0.5)
        self.assertFalse(halt_thread.is_alive())
        if gateway.connection_value.motion_started.is_set():
            move_thread.join(0.5)
        else:
            move_thread.join(0.5)

        self.assertFalse(move_thread.is_alive())
        self.assertEqual(halt_errors, [])
        operations = [message["op"] for message in gateway.connection_value.messages]
        if move_errors:
            self.assertIsInstance(move_errors[0], StackChanTransportError)
            self.assertEqual(operations, ["lease.acquire", "motion.halt"])
        else:
            self.assertIn("motion.move", operations)
            self.assertIn("motion.halt", operations)
            self.assertNotIn("lease.release", operations)

    def test_position_read_is_passive_and_converts_wire_keys(self):
        gateway = _FakeGateway()
        transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=1000)
        self.assertEqual(transport.get_positions(), {
            "base_yaw.pos": 2.0,
            "base_pitch.pos": -3.0,
        })
        message = gateway.connection_value.messages[-1]
        self.assertEqual(message["op"], "motion.get")
        self.assertNotIn("seq", message)

    def test_halt_acquires_lease_when_only_a_passive_read_is_busy(self):
        gateway = _FakeGateway()
        transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=1000)
        acquired = threading.Event()

        def passive_read():
            transport._operation_lock.acquire()
            acquired.set()
            time.sleep(0.02)
            transport._operation_lock.release()

        thread = threading.Thread(target=passive_read)
        thread.start()
        self.assertTrue(acquired.wait(0.5))
        transport.halt()
        thread.join(0.5)
        self.assertFalse(thread.is_alive())
        operations = [message["op"] for message in gateway.connection_value.messages]
        self.assertEqual(operations, ["lease.acquire", "motion.halt"])

    def test_halt_reacquires_after_a_completed_move_left_a_stale_marker(self):
        gateway = _FakeGateway()
        transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=1000)
        transport._active_cancel = threading.Event()

        transport.halt()

        operations = [message["op"] for message in gateway.connection_value.messages]
        self.assertEqual(operations, ["lease.acquire", "motion.halt"])

    def test_zero_and_release_obey_speed_limit_at_transport_boundary(self):
        policy = SimpleNamespace(motion=SimpleNamespace(max_speed=5))
        for operation in ("zero_pose", "release"):
            with self.subTest(operation=operation):
                gateway = _FakeGateway()
                gateway.connection_value.positions = {"pan": 30.0, "tilt": 15.0}
                service = StackChanMotionService(
                    safety_policy=policy,
                    device_id="stackchan-test",
                    token=TOKEN,
                    allow_insecure_ws=True,
                )
                self.assertIs(service._transport._safety_policy, policy)
                service._transport._gateway = gateway
                result = getattr(service, operation)()
                if operation == "release":
                    self.assertEqual(result, {})
                messages = gateway.connection_value.messages
                move = next(m for m in messages if m["op"] == "motion.move")
                self.assertEqual(move["args"]["duration_ms"], 6000)
                ops = [m["op"] for m in messages]
                self.assertLess(ops.index("motion.get"), ops.index("motion.move"))
                if operation == "release":
                    self.assertLess(ops.index("motion.halt"), ops.index("motion.get"))
                    self.assertEqual(ops[-1], "motion.release")

    def test_unsafe_duration_or_invalid_measurement_does_not_send_move(self):
        policy = SimpleNamespace(motion=SimpleNamespace(max_speed=1))
        for pan in (100.0, float("nan")):
            with self.subTest(pan=pan):
                gateway = _FakeGateway()
                gateway.connection_value.positions["pan"] = pan
                transport = _BodyTransport(gateway, 1.0, 1000, policy)
                with self.assertRaises(StackChanTransportError):
                    transport.move({"yawServo": {"angle": 0, "speed": 1000}}, 0.05)
                self.assertNotIn("motion.move", [m["op"] for m in gateway.connection_value.messages])
                self.assertEqual(gateway.connection_value.close_reasons, ["motion failed"])

    def test_malformed_position_result_fails_closed(self):
        policy = SimpleNamespace(motion=SimpleNamespace(max_speed=5))
        gateway = _FakeGateway()
        transport = _BodyTransport(gateway, 1.0, 1000, policy)
        original = gateway.connection_value.request

        def request(message, timeout):
            if message["op"] == "motion.get":
                return {"status": "completed", "result": None}
            return original(message, timeout)

        with patch.object(gateway.connection_value, "request", side_effect=request):
            with self.assertRaisesRegex(StackChanTransportError, "invalid measured positions"):
                transport.move({"yawServo": {"angle": 0, "speed": 1000}}, 0.05)
        self.assertEqual(gateway.connection_value.close_reasons, ["motion failed"])
        self.assertNotIn("motion.move", [m["op"] for m in gateway.connection_value.messages])

    def test_handshake_acceptance_precedes_connection_publication(self):
        gateway = _BodyGateway("stackchan-test", TOKEN)
        observed = []
        test = self

        class Socket:
            request = SimpleNamespace(
                path="/stackchan/body/v1",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )

            def recv(self, timeout):
                return json.dumps({
                    "v": 1, "type": "hello", "device_id": "stackchan-test",
                    "capabilities": [
                        "motion.pan_tilt", "motion.measured_position", "motion.halt_hold",
                        "motion.torque_release", "motion.timed_move",
                    ],
                })

            def send(self, raw):
                observed.append(json.loads(raw)["type"])
                # Another HAL thread cannot obtain a published connection
                # until the acceptance send has finished.
                test.assertIsNone(gateway._connection)
                test.assertTrue(gateway._lock.locked())

            def __iter__(self):
                test.assertTrue(gateway.connected)
                return iter(())

        gateway.handle(Socket())
        self.assertEqual(observed, ["hello.accepted"])
        self.assertFalse(gateway.connected)

    def test_tls_is_required_without_explicit_development_opt_in(self):
        with self.assertRaisesRegex(ValueError, "requires TLS"):
            StackChanMotionService(device_id="stackchan-test", token=TOKEN, port=0)


class TestFirmwareRejections(unittest.TestCase):
    """A firmware error reply means the firmware already handled the state; HAL must not hang up."""

    def _rejecting(self, op, code):
        gateway = _FakeGateway()
        original = gateway.connection_value.request

        def request(message, timeout):
            if message["op"] == op:
                gateway.connection_value.messages.append(message)
                raise StackChanCommandRejected(op, code)
            return original(message, timeout)

        gateway.connection_value.request = request
        return gateway

    def test_halt_rejected_by_firmware_keeps_transport_open(self):
        gateway = self._rejecting("motion.halt", "halt_failed")
        transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=500)
        with self.assertRaises(StackChanCommandRejected) as caught:
            transport.halt()
        self.assertEqual((caught.exception.op, caught.exception.code), ("motion.halt", "halt_failed"))
        self.assertEqual(gateway.connection_value.close_reasons, [])
        # The warning line is emitted by _DeviceConnection.request; see the
        # device-connection test below. This fake raises past that layer.

    def test_move_rejected_by_firmware_keeps_transport_open(self):
        gateway = self._rejecting("motion.move", "motion_enable_failed")
        transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=500)
        with self.assertRaises(StackChanCommandRejected):
            transport.move({"yawServo": {"angle": 10, "speed": 1000}}, duration=0.1)
        self.assertEqual(gateway.connection_value.close_reasons, [])

    def test_release_rejected_by_firmware_keeps_transport_open(self):
        gateway = self._rejecting("motion.release", "release_failed")
        transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=500)
        transport._target_settle_timeout = 0.01
        with self.assertRaises(StackChanCommandRejected):
            transport.release({"yawServo": {"angle": 0, "speed": 1000}, "pitchServo": {"angle": 300, "speed": 1000}}, 0.1)
        self.assertEqual(gateway.connection_value.close_reasons, [])

    def test_other_transport_errors_still_close(self):
        gateway = _FakeGateway()
        original = gateway.connection_value.request

        def request(message, timeout):
            if message["op"] == "motion.halt":
                raise StackChanTransportError("Stack-chan returned invalid measured positions")
            return original(message, timeout)

        gateway.connection_value.request = request
        transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=500)
        with self.assertRaises(StackChanTransportError):
            transport.halt()
        self.assertEqual(gateway.connection_value.close_reasons, ["halt failed"])

    def test_device_connection_turns_error_reply_into_rejection_with_code(self):
        sent = threading.Event()

        class Socket:
            def __init__(self):
                self.sent = []
                self.closed = []

            def send(self, data):
                self.sent.append(json.loads(data))
                sent.set()

            def close(self, code=None, reason=None):
                self.closed.append((code, reason))

        socket = Socket()
        connection = _DeviceConnection(socket)
        message = {"v": 1, "id": "abc", "op": "motion.halt", "session": "s", "seq": 1}
        outcome = {}

        def call():
            try:
                connection.request(message, timeout=2.0)
            except Exception as exc:  # noqa: BLE001 - captured for assertions
                outcome["exc"] = exc

        worker = threading.Thread(target=call)
        with self.assertLogs("hal.motion.stackchan", level="WARNING") as logs:
            worker.start()
            self.assertTrue(sent.wait(2))
            connection.deliver({"v": 1, "id": "abc", "status": "error", "code": "halt_failed"})
            worker.join(2)
        exc = outcome["exc"]
        self.assertIsInstance(exc, StackChanCommandRejected)
        self.assertEqual((exc.op, exc.code), ("motion.halt", "halt_failed"))
        self.assertIn("halt_failed", str(exc))
        self.assertTrue(any("motion.halt" in line and "halt_failed" in line for line in logs.output))
        self.assertEqual(socket.closed, [])

    def test_socket_disconnect_while_waiting_is_offline_not_a_rejection(self):
        sent = threading.Event()

        class Socket:
            def send(self, data):
                sent.set()

            def close(self, code=None, reason=None):
                pass

        connection = _DeviceConnection(Socket())
        outcome = {}

        def call():
            try:
                connection.request({"v": 1, "id": "x", "op": "motion.move", "session": "s", "seq": 1}, timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                outcome["exc"] = exc

        worker = threading.Thread(target=call)
        worker.start()
        self.assertTrue(sent.wait(2))
        connection.close()  # what the server handler does when the socket drops
        worker.join(2)
        self.assertIsInstance(outcome["exc"], StackChanOffline)
        self.assertNotIsInstance(outcome["exc"], StackChanCommandRejected)

    def test_settle_miss_in_timed_move_halts_and_keeps_transport(self):
        gateway = _FakeGateway()
        gateway.connection_value.apply_moves = False
        transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=1000)
        transport._target_settle_timeout = 0.01
        with self.assertRaises(StackChanTargetNotReached):
            transport.move({"yawServo": {"angle": 100, "speed": 1000}}, 0.05)
        operations = [m["op"] for m in gateway.connection_value.messages]
        self.assertIn("motion.halt", operations)
        self.assertNotIn("lease.release", operations)
        self.assertEqual(gateway.connection_value.close_reasons, [])

    def _bench_client(self, svc, stack):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import hal.app_state as state
        from robots._experimental.stackchan.commissioning import router

        app = FastAPI()
        app.include_router(router)
        stack.enter_context(patch.object(StackChanMotionService, "is_connected", True))
        stack.enter_context(patch.object(state, "animation_service", svc))
        stack.enter_context(patch.object(state, "tracker_service", None))
        stack.enter_context(patch.object(state, "policy_service", None))
        return TestClient(app)

    def test_http_stop_reports_firmware_code(self):
        svc = StackChanMotionService(device_id="t-01", token=TOKEN, port=0, allow_insecure_ws=True)
        svc._transport = _FakeServiceTransport()
        with ExitStack() as stack:
            client = self._bench_client(svc, stack)
            stack.enter_context(patch.object(svc._transport, "halt", side_effect=StackChanCommandRejected("motion.halt", "halt_failed")))
            response = client.post("/stackchan/stop")
        self.assertEqual(response.status_code, 502)
        detail = response.json()["detail"]
        self.assertEqual((detail["op"], detail["code"]), ("motion.halt", "halt_failed"))

    def test_http_release_reports_firmware_code(self):
        svc = StackChanMotionService(device_id="t-01", token=TOKEN, port=0, allow_insecure_ws=True)
        svc._transport = _FakeServiceTransport()
        svc._transport.release_error = StackChanCommandRejected("motion.release", "release_failed")
        with ExitStack() as stack:
            response = self._bench_client(svc, stack).post("/stackchan/release")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"]["code"], "release_failed")

    def test_http_release_requires_measured_arrival_and_torque_off(self):
        for reaches_target in (False, True):
            with self.subTest(reaches_target=reaches_target):
                svc = StackChanMotionService(device_id="t-01", token=TOKEN, port=0, allow_insecure_ws=True)
                gateway = _FakeGateway()
                gateway.connection_value.apply_moves = reaches_target
                svc._transport = _BodyTransport(gateway, timeout=1.0, lease_ttl_ms=1000)
                svc._transport._target_settle_timeout = 0.01
                with ExitStack() as stack:
                    client = self._bench_client(svc, stack)
                    stack.enter_context(patch("hal.drivers.motors.stackchan_service._RELEASE_DURATION_S", 0.05))
                    response = client.post("/stackchan/release")
                operations = [message["op"] for message in gateway.connection_value.messages]
                self.assertEqual(gateway.connection_value.close_reasons, [])
                if reaches_target:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"status": "ok"})
                    self.assertEqual(operations[-1], "motion.release")
                    self.assertEqual(svc.motion_mode, "released")
                else:
                    self.assertEqual(response.status_code, 502)
                    self.assertIn("did not reach", response.json()["detail"]["errors"]["stackchan"])
                    self.assertEqual(operations[-1], "motion.halt")
                    self.assertNotIn("motion.release", operations)
                    self.assertIsNone(svc.motion_mode)

    def test_http_release_reports_cancellation_and_transport_failure(self):
        for message in ("release was halted before torque-off", "command timed out; delivery is unknown"):
            with self.subTest(error=message):
                svc = StackChanMotionService(device_id="t-01", token=TOKEN, port=0, allow_insecure_ws=True)
                transport = _FakeServiceTransport()
                transport.release_error = StackChanTransportError(message)
                svc._transport = transport
                with ExitStack() as stack:
                    response = self._bench_client(svc, stack).post("/stackchan/release")
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["detail"]["errors"], {"stackchan": message})
                self.assertIsNone(svc.motion_mode)

    def test_service_release_preserves_os_error_dict_contract(self):
        svc = StackChanMotionService(device_id="t-01", token="x" * 40, host="127.0.0.1", port=0, allow_insecure_ws=True)

        class Transport:
            def release(self, native, duration):
                raise StackChanCommandRejected("motion.release", "release_failed")

        svc._transport = Transport()
        errors = svc.release()
        self.assertEqual(errors, {"stackchan": "Stack-chan rejected motion.release: release_failed"})
        with self.assertRaises(StackChanCommandRejected) as caught:
            svc.release_checked()
        self.assertEqual((caught.exception.op, caught.exception.code), ("motion.release", "release_failed"))


if __name__ == "__main__":
    unittest.main()
