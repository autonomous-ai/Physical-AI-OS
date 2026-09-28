"""Saved-home commissioning: isolated peers only, never a physical robot."""
from __future__ import annotations

import copy
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

import hal.app_state as state
from hal.drivers.motors.stackchan_service import (
    HOME_CAPABILITY, HOME_FRAME, StackChanCommissioningFailed, StackChanMotionService, StackChanMotionCancelled,
    StackChanOffline, StackChanTransportError, _BodyTransport,
)
from robots._experimental.stackchan.commissioning import router

TOKEN = "home-commissioning-test-only-token-32-characters"
CAPABILITIES = {"motion.pan_tilt", "motion.measured_position", "motion.halt_hold",
                "motion.torque_release", "motion.timed_move", HOME_CAPABILITY}


class HomePeer:
    def __init__(self):
        self.capabilities = frozenset(CAPABILITIES)
        self.closed = False
        self.commands = []
        self.close_reasons = []
        self.positions = {"pan": 0.6, "tilt": 2.5}
        self.arrival = {"pan": 0.6, "tilt": 7.8}
        self.arrivals = None  # optional list consumed per motion.move_home
        self.post_hold_positions = None
        self.motion_started = threading.Event()
        self.result_override = None
        self.after_read = None
        self.lease_active = False
        self.fail_move_number = None  # raise on the Nth motion.move_home
        self.move_count = 0

    def request(self, message, timeout):
        del timeout
        if self.closed:
            raise StackChanOffline("original peer disconnected")
        self.commands.append(copy.deepcopy(message))
        op = message["op"]
        if op in {"motion.move_home", "motion.halt"}:
            assert self.lease_active and isinstance(message.get("seq"), int)
        result = {}
        if op == "motion.get_home":
            assert "seq" not in message
            result = {"coordinate_frame": HOME_FRAME, "positions": dict(self.positions)}
            if self.result_override is not None:
                result = copy.deepcopy(self.result_override)
            if self.after_read:
                self.after_read()
        elif op == "lease.acquire":
            self.lease_active = True
        elif op == "lease.renew":
            assert self.lease_active
        elif op == "motion.move_home":
            self.move_count += 1
            if self.fail_move_number == self.move_count:
                self.lease_active = False
                raise StackChanTransportError("Stack-chan rejected command: lease_expired")
            self.motion_started.set()
            if self.arrivals:
                self.arrival = self.arrivals.pop(0)
            self.positions = dict(self.arrival)
            result = {"state": "scheduled"}
        elif op in {"motion.halt", "lease.release"}:
            assert self.lease_active
            self.lease_active = False
            if self.positions["tilt"] < 5:
                raise StackChanTransportError("Stack-chan rejected command: halt_failed")
            if self.post_hold_positions:
                self.positions = dict(self.post_hold_positions)
            result = {"state": "held"}
        else:
            raise AssertionError("Unexpected hardware operation: " + op)
        return {"v": 1, "id": message["id"], "status": "completed", "result": result}

    def force_close(self, reason):
        self.close_reasons.append(reason)
        self.closed = True


class HomeGateway:
    def __init__(self):
        self.peer = HomePeer()

    @property
    def connected(self):
        return not self.peer.closed

    def connection(self):
        if self.peer.closed:
            raise StackChanOffline("offline")
        return self.peer


def service(*, enabled=False, policy=None):
    svc = StackChanMotionService(device_id="home-test", token=TOKEN,
                                host="127.0.0.1", port=0, allow_insecure_ws=True,
                                home_commissioning_enabled=enabled, safety_policy=policy)
    gateway = HomeGateway()
    svc._gateway = gateway
    svc._transport = _BodyTransport(gateway, 1.0, 500, policy)
    svc._transport._target_settle_timeout = 0.01
    return svc, gateway.peer


class TestHomeCommissioning(unittest.TestCase):
    def test_default_gate_and_passive_explicit_frame(self):
        svc, peer = service()
        status = svc.home_state()
        self.assertTrue(status["supported"])
        self.assertFalse(status["motion_enabled"])
        self.assertFalse(status["calibration_verified"])
        self.assertEqual(peer.commands, [])
        with self.assertRaises(PermissionError):
            svc.move_home({"tilt": 8.0})
        self.assertEqual(peer.commands, [])
        value = svc.get_home_positions()
        self.assertEqual(value, {"coordinate_frame": HOME_FRAME, "position_source": "measured",
                                 "positions": {"pan": 0.6, "tilt": 2.5}})
        self.assertEqual([m["op"] for m in peer.commands], ["motion.get_home"])

    def test_default_environment_does_not_enable_commissioning(self):
        with patch.dict("os.environ", {}, clear=True):
            svc = StackChanMotionService(device_id="home-test", token=TOKEN,
                                        allow_insecure_ws=True, port=0)
            with self.assertRaises(PermissionError):
                svc.move_home({"tilt": 8.0})

    def test_old_or_disconnected_peer_is_not_assumed_to_support_home(self):
        svc, peer = service(enabled=True)
        peer.capabilities = frozenset(CAPABILITIES - {HOME_CAPABILITY})
        self.assertFalse(svc.home_state()["supported"])
        self.assertTrue(svc.home_state()["commissioning_configured"])
        self.assertFalse(svc.home_state()["motion_enabled"])
        with self.assertRaises(NotImplementedError):
            svc.get_home_positions()
        with self.assertRaises(NotImplementedError):
            svc.move_home({"tilt": 8.0})
        self.assertEqual(peer.commands, [])
        peer.closed = True
        self.assertFalse(svc.home_state()["connected"])
        self.assertFalse(svc.home_state()["supported"])

    def test_invalid_feedback_is_not_reinterpreted_or_used_for_a_lease(self):
        cases = [None, {}, {"positions": {"pan": 0, "tilt": -42.5}},
                 {"coordinate_frame": "legacy", "positions": {"pan": 0, "tilt": 2.5}}]
        for positions in ({"pan": True, "tilt": 2.5}, {"pan": 0, "tilt": float("nan")},
                          {"pan": 0, "tilt": 313}, {"pan": 0},
                          {"pan": 0, "tilt": 2.5, "extra": 1}, {"pan": "0", "tilt": 2.5}):
            cases.append({"coordinate_frame": HOME_FRAME, "positions": positions})
        for value in cases:
            with self.subTest(value=value):
                svc, peer = service(enabled=True)
                # None is tested as a non-object result, not the fixture's no-override sentinel.
                peer.result_override = [] if value is None else value
                with self.assertRaises(StackChanTransportError):
                    svc.move_home({"tilt": 8.0})
                self.assertEqual([m["op"] for m in peer.commands], ["motion.get_home"])
                self.assertEqual(peer.close_reasons, [])

    def test_unsuitable_start_is_rejected_before_lease(self):
        for positions in ({"pan": 31, "tilt": 2.5}, {"pan": 0.6, "tilt": -0.1},
                          {"pan": 0.6, "tilt": 5.0}, {"pan": 0.6, "tilt": 8.0}):
            with self.subTest(positions=positions):
                svc, peer = service(enabled=True)
                peer.positions = positions
                with self.assertRaises(ValueError):
                    svc.move_home({"tilt": 8.0})
                self.assertEqual([m["op"] for m in peer.commands], ["motion.get_home"])
                self.assertFalse(peer.closed)

    def test_invalid_targets_and_durations_never_reach_peer(self):
        svc, peer = service(enabled=True)
        for positions in ({}, {"pan": 1}, {"tilt": 8, "pan": 0}, {"base_pitch.pos": 8},
                          {"tilt": 5}, {"tilt": 10.1}, {"tilt": True},
                          {"tilt": "8"}, {"tilt": float("inf")}):
            with self.subTest(positions=positions), self.assertRaises(ValueError):
                svc.move_home(positions)
        for duration in (True, "2", 0, 1.999, 10.1, float("nan")):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                svc.move_home({"tilt": 8}, duration)
        self.assertEqual(peer.commands, [])

    def test_busy_owned_or_frozen_body_does_not_queue_commissioning(self):
        svc, peer = service(enabled=True)
        svc.acquire_body()
        with self.assertRaises(PermissionError):
            svc.move_home({"tilt": 8})
        svc.release_body()
        svc._tracking_flag = True
        with self.assertRaises(PermissionError):
            svc.move_home({"tilt": 8})
        svc._tracking_flag = False
        svc._frozen = True
        with self.assertRaises(PermissionError):
            svc.move_home({"tilt": 8})
        svc._frozen = False
        svc._transport._operation_lock.acquire()
        try:
            with self.assertRaises(StackChanTransportError):
                svc.move_home({"tilt": 8})
            with self.assertRaises(StackChanTransportError):
                svc.get_home_positions()
        finally:
            svc._transport._operation_lock.release()
        self.assertEqual(peer.commands, [])

    def test_measured_arrival_precedes_hold_and_post_hold_read(self):
        svc, peer = service(enabled=True)
        value = svc.move_home({"tilt": 8}, 2)
        self.assertTrue(value["target_reached"])
        self.assertEqual(value["terminal_state"], "held")
        self.assertEqual(value["positions"], {"pan": 0.6, "tilt": 7.8})
        self.assertEqual(svc._target, {"base_yaw.pos": 0.6, "base_pitch.pos": -37.2})
        operations = [m["op"] for m in peer.commands]
        self.assertEqual(operations[:3], ["motion.get_home", "lease.acquire", "motion.move_home"])
        self.assertEqual(operations[-3:], ["motion.get_home", "lease.release", "motion.get_home"])
        self.assertIn("lease.renew", operations)
        self.assertNotIn("motion.move", operations)
        self.assertNotIn("motion.release", operations)
        move = next(m for m in peer.commands if m["op"] == "motion.move_home")
        self.assertEqual(move["args"], {"duration_ms": 2000, "motion": {"pitchServo": {"angle": 80}}})
        self.assertFalse(peer.lease_active)
        self.assertFalse(peer.closed)

    def test_stall_below_hold_floor_never_reports_success(self):
        svc, peer = service(enabled=True)
        peer.arrival = {"pan": 0.6, "tilt": 4.0}
        with self.assertRaisesRegex(StackChanTransportError, "halt_failed"):
            svc.move_home({"tilt": 8})
        operations = [m["op"] for m in peer.commands]
        self.assertIn("motion.halt", operations)
        self.assertNotIn("lease.release", operations)
        self.assertEqual(peer.close_reasons, [])
        self.assertFalse(peer.closed)

    def test_settle_miss_halts_holds_and_reports_samples_without_closing(self):
        svc, peer = service(enabled=True)
        peer.arrival = {"pan": 0.6, "tilt": 6.9}
        with self.assertRaises(StackChanCommissioningFailed) as caught:
            svc.move_home({"tilt": 8}, 2)
        details = caught.exception.details
        self.assertEqual(details["initial"], {"pan": 0.6, "tilt": 2.5})
        self.assertEqual(details["target"], {"tilt": 8.0})
        self.assertIn("did not reach", details["reason"])
        self.assertEqual(details["terminal_command"], "motion.halt")
        self.assertGreaterEqual(len(details["samples"]), 1)
        for sample in details["samples"]:
            self.assertEqual(set(sample), {"elapsed_s", "pan", "tilt", "move"})
            self.assertEqual(sample["tilt"], 6.9)
        self.assertEqual(details["final"], {"pan": 0.6, "tilt": 6.9})
        operations = [m["op"] for m in peer.commands]
        self.assertIn("motion.halt", operations)
        self.assertNotIn("lease.release", operations)
        self.assertFalse(peer.closed)
        self.assertEqual(peer.close_reasons, [])
        self.assertFalse(peer.lease_active)

    def test_settle_samples_are_logged(self):
        svc, peer = service(enabled=True)
        peer.arrival = {"pan": 0.6, "tilt": 6.9}
        with self.assertLogs("hal.motion.stackchan", level="INFO") as logs:
            with self.assertRaises(StackChanCommissioningFailed):
                svc.move_home({"tilt": 8}, 2)
        self.assertTrue(any("commissioning sample" in line and "tilt=6.9" in line for line in logs.output))
        self.assertTrue(any("commissioning failed" in line and "did not reach" in line for line in logs.output))

    def test_http_settle_miss_returns_502_with_samples(self):
        svc, peer = service(enabled=True)
        peer.arrival = {"pan": 0.6, "tilt": 6.9}
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        request = {"coordinate_frame": HOME_FRAME, "positions": {"tilt": 8}, "duration": 2}
        with patch.object(state, "animation_service", svc), patch.object(state, "_sleeping", False):
            response = client.post("/stackchan/home/move", json=request)
        self.assertEqual(response.status_code, 502)
        detail = response.json()["detail"]
        self.assertIn("did not reach", detail["reason"])
        self.assertEqual(detail["final"], {"pan": 0.6, "tilt": 6.9})
        self.assertGreaterEqual(len(detail["samples"]), 1)
        self.assertFalse(peer.closed)

    def test_invalid_feedback_during_settle_halts_and_keeps_transport(self):
        svc, peer = service(enabled=True)
        peer.arrival = {"pan": 0.6, "tilt": 6.9}

        def corrupt_after_move():
            if peer.motion_started.is_set():
                peer.result_override = {"coordinate_frame": "legacy", "positions": {"pan": 0.6, "tilt": 6.9}}

        peer.after_read = corrupt_after_move
        with self.assertRaisesRegex(StackChanTransportError, "invalid saved-home coordinate frame"):
            svc.move_home({"tilt": 8}, 2)
        operations = [m["op"] for m in peer.commands]
        self.assertIn("motion.halt", operations)
        self.assertNotIn("lease.release", operations)
        self.assertFalse(peer.lease_active)
        self.assertFalse(peer.closed)
        self.assertEqual(peer.close_reasons, [])

    def test_one_recommand_closes_a_short_but_stable_target(self):
        svc, peer = service(enabled=True)
        svc._transport._target_settle_timeout = 0.2
        peer.arrivals = [{"pan": 0.6, "tilt": 6.5}, {"pan": 0.6, "tilt": 7.9}]
        value = svc.move_home({"tilt": 8}, 2)
        self.assertTrue(value["target_reached"])
        self.assertEqual(value["terminal_state"], "held")
        self.assertEqual(value["positions"], {"pan": 0.6, "tilt": 7.9})
        self.assertEqual(value["recommands"], 1)
        operations = [m["op"] for m in peer.commands]
        self.assertEqual(operations.count("motion.move_home"), 2)
        self.assertEqual(operations.count("lease.release"), 1)
        self.assertNotIn("motion.halt", operations)
        moves = [m for m in peer.commands if m["op"] == "motion.move_home"]
        self.assertEqual(moves[0]["args"], moves[1]["args"])
        self.assertGreater(moves[1]["seq"], moves[0]["seq"])
        first, second = [i for i, m in enumerate(peer.commands) if m["op"] == "motion.move_home"]
        self.assertIn("lease.renew", [m["op"] for m in peer.commands[first + 1:second]])
        self.assertFalse(peer.closed)

    def test_persistent_short_target_fails_after_exactly_one_recommand(self):
        svc, peer = service(enabled=True)
        svc._transport._target_settle_timeout = 0.2
        peer.arrivals = [{"pan": 0.6, "tilt": 6.5}, {"pan": 0.6, "tilt": 6.6}, {"pan": 0.6, "tilt": 8.0}]
        with self.assertRaises(StackChanCommissioningFailed) as caught:
            svc.move_home({"tilt": 8}, 2)
        details = caught.exception.details
        self.assertEqual(details["recommands"], 1)
        tilts = {sample["tilt"] for sample in details["samples"]}
        self.assertEqual(tilts, {6.5, 6.6})
        self.assertEqual({sample["move"] for sample in details["samples"]}, {1, 2})
        self.assertEqual(details["final"], {"pan": 0.6, "tilt": 6.6})
        operations = [m["op"] for m in peer.commands]
        self.assertEqual(operations.count("motion.move_home"), 2)
        self.assertIn("motion.halt", operations)
        self.assertNotIn("lease.release", operations)
        self.assertFalse(peer.closed)

    def test_no_recommand_when_the_head_did_not_move(self):
        svc, peer = service(enabled=True)
        peer.arrivals = [{"pan": 0.6, "tilt": 2.5}]
        # Below the hold floor the halt itself is rejected; either way, no second move.
        with self.assertRaises(StackChanTransportError):
            svc.move_home({"tilt": 8}, 2)
        self.assertEqual([m["op"] for m in peer.commands].count("motion.move_home"), 1)

    def test_success_without_recommand_reports_zero(self):
        svc, peer = service(enabled=True)
        value = svc.move_home({"tilt": 8}, 2)
        self.assertTrue(value["target_reached"])
        self.assertEqual(value["recommands"], 0)

    def test_no_recommand_while_the_head_is_still_moving(self):
        svc, peer = service(enabled=True)
        svc._transport._target_settle_timeout = 0.2
        peer.arrivals = [{"pan": 0.6, "tilt": 6.0}]

        def wobble():
            # Still oscillating around 6.5: never stable, never within tolerance.
            if peer.motion_started.is_set():
                tilt = 6.8 if peer.positions["tilt"] <= 6.5 else 6.2
                peer.positions = {"pan": 0.6, "tilt": tilt}

        peer.after_read = wobble
        with self.assertRaises(StackChanCommissioningFailed) as caught:
            svc.move_home({"tilt": 8}, 2)
        self.assertEqual(caught.exception.details["recommands"], 0)
        self.assertEqual([m["op"] for m in peer.commands].count("motion.move_home"), 1)

    def test_no_recommand_below_the_hold_margin_or_too_far_short(self):
        for tilt in (5.5, 4.9):
            with self.subTest(tilt=tilt):
                svc, peer = service(enabled=True)
                svc._transport._target_settle_timeout = 0.2
                peer.arrivals = [{"pan": 0.6, "tilt": tilt}]
                with self.assertRaises(StackChanTransportError) as caught:
                    svc.move_home({"tilt": 8}, 2)
                self.assertEqual([m["op"] for m in peer.commands].count("motion.move_home"), 1)
                if isinstance(caught.exception, StackChanCommissioningFailed):
                    self.assertEqual(caught.exception.details["recommands"], 0)

    def test_no_recommand_after_overshoot(self):
        svc, peer = service(enabled=True)
        svc._transport._target_settle_timeout = 0.2
        peer.arrivals = [{"pan": 0.6, "tilt": 9.6}]
        with self.assertRaises(StackChanCommissioningFailed) as caught:
            svc.move_home({"tilt": 8}, 2)
        self.assertEqual(caught.exception.details["recommands"], 0)
        self.assertEqual([m["op"] for m in peer.commands].count("motion.move_home"), 1)

    def test_failed_recommand_still_returns_the_evidence(self):
        svc, peer = service(enabled=True)
        svc._transport._target_settle_timeout = 0.2
        peer.arrivals = [{"pan": 0.6, "tilt": 6.5}]
        peer.fail_move_number = 2
        with self.assertRaises(StackChanCommissioningFailed) as caught:
            svc.move_home({"tilt": 8}, 2)
        details = caught.exception.details
        self.assertEqual(details["recommands"], 1)
        self.assertGreaterEqual(len(details["samples"]), 2)
        self.assertTrue(all(sample["move"] == 1 for sample in details["samples"]))
        self.assertIn("lease_expired", details["reason"])
        self.assertFalse(peer.closed)

    def test_in_flight_feedback_failure_halts_before_motion_duration(self):
        for failure in ("yaw drift", "invalid saved-home coordinate frame"):
            with self.subTest(failure=failure):
                svc, peer = service(enabled=True)
                errors, successes = [], []
                finished = threading.Event()
                original_request = peer.request

                def request(message, timeout):
                    result = original_request(message, timeout)
                    if message["op"] == "motion.move_home":
                        if failure == "yaw drift":
                            peer.positions["pan"] += 10
                        else:
                            peer.result_override = {"coordinate_frame": "legacy"}
                    return result

                def move():
                    try:
                        successes.append(svc.move_home({"tilt": 8}, 10))
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        finished.set()

                peer.request = request
                worker = threading.Thread(target=move)
                worker.start()
                try:
                    self.assertTrue(peer.motion_started.wait(1))
                    self.assertTrue(finished.wait(1), "Feedback failure must halt before the 10-second move ends")
                    self.assertEqual(successes, [])
                    self.assertEqual(len(errors), 1)
                    self.assertIn(failure, str(errors[0]))
                    operations = [m["op"] for m in peer.commands]
                    self.assertEqual(operations.count("motion.halt"), 1)
                    self.assertNotIn("lease.release", operations)
                    self.assertFalse(peer.closed)
                finally:
                    if worker.is_alive():
                        svc.halt()
                    worker.join(2)

    def test_yaw_drift_stops_before_terminal_success(self):
        svc, peer = service(enabled=True)
        peer.arrival = {"pan": 2.0, "tilt": 7.8}
        with self.assertRaisesRegex(StackChanTransportError, "yaw drift"):
            svc.move_home({"tilt": 8})
        self.assertIn("motion.halt", [m["op"] for m in peer.commands])
        self.assertNotIn("lease.release", [m["op"] for m in peer.commands])
        self.assertEqual([m["op"] for m in peer.commands].count("motion.move_home"), 1)
        self.assertEqual(peer.close_reasons, [])
        self.assertFalse(peer.closed)

    def test_post_hold_change_is_not_reported_as_arrival(self):
        svc, peer = service(enabled=True)
        peer.post_hold_positions = {"pan": 0.6, "tilt": 6.0}
        with self.assertRaisesRegex(StackChanTransportError, "changed after terminal hold"):
            svc.move_home({"tilt": 8})
        self.assertIn("lease.release", [m["op"] for m in peer.commands])
        self.assertEqual(peer.close_reasons, [])

    def test_peer_replacement_never_receives_lease_or_move(self):
        svc, original = service(enabled=True)
        replacement = HomePeer()

        def replace_peer():
            original.closed = True
            svc._gateway.peer = replacement

        original.after_read = replace_peer
        with self.assertRaises(StackChanOffline):
            svc.move_home({"tilt": 8})
        self.assertEqual(replacement.commands, [])
        self.assertEqual(replacement.close_reasons, [])
        self.assertEqual(original.close_reasons, [])

    def test_halt_cancels_without_false_success_or_a_second_terminal_command(self):
        svc, peer = service(enabled=True)
        errors, successes = [], []

        def move():
            try:
                successes.append(svc.move_home({"tilt": 8}))
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=move)
        worker.start()
        self.assertTrue(peer.motion_started.wait(1))
        svc.halt()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(successes, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StackChanMotionCancelled)
        ops = [m["op"] for m in peer.commands]
        self.assertEqual(ops[-2:], ["lease.acquire", "motion.halt"])
        self.assertNotIn("lease.release", ops)
        self.assertFalse(peer.closed)

    def test_release_after_reconnect_does_not_move_the_replacement_peer(self):
        svc, original = service(enabled=True)
        replacement = HomePeer()
        errors = []

        def move():
            try:
                svc.move_home({"tilt": 8})
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=move)
        worker.start()
        self.assertTrue(original.motion_started.wait(1))
        original.closed = True
        svc._gateway.peer = replacement
        try:
            with self.assertRaises(StackChanOffline):
                svc._transport.release({"pitchServo": {"angle": 300}}, 2)
        finally:
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StackChanMotionCancelled)
        self.assertEqual(replacement.commands, [])
        self.assertEqual(replacement.close_reasons, [])
        self.assertEqual(original.close_reasons, ["release failed"])
        self.assertIsNone(svc._transport._active_connection)

    def test_release_and_concurrent_halt_keep_one_peer_after_reconnect(self):
        svc, original = service(enabled=True)
        replacement = HomePeer()
        release_started = threading.Event()
        errors = []
        original_request = original.request

        def recovery_request(message, timeout):
            if message["op"] == "motion.move":
                original.commands.append(copy.deepcopy(message))
                release_started.set()
                return {"v": 1, "id": message["id"], "status": "completed"}
            return original_request(message, timeout)

        original.request = recovery_request

        def move():
            try:
                svc.move_home({"tilt": 8})
            except Exception as exc:
                errors.append(exc)

        def release():
            try:
                svc._transport.release({"pitchServo": {"angle": 300}}, 10)
            except Exception as exc:
                errors.append(exc)

        mover = threading.Thread(target=move)
        releaser = threading.Thread(target=release)
        mover.start()
        self.assertTrue(original.motion_started.wait(1))
        releaser.start()
        self.assertTrue(release_started.wait(1))
        original.closed = True
        svc._gateway.peer = replacement
        try:
            with self.assertRaises(StackChanOffline):
                svc._transport.halt()
        finally:
            mover.join(2)
            releaser.join(2)
        self.assertFalse(mover.is_alive())
        self.assertFalse(releaser.is_alive())
        self.assertEqual(len(errors), 2)
        self.assertTrue(any(isinstance(exc, StackChanMotionCancelled) for exc in errors))
        self.assertTrue(any("halted before torque-off" in str(exc) for exc in errors))
        self.assertEqual(replacement.commands, [])
        self.assertEqual(replacement.close_reasons, [])
        self.assertEqual(original.close_reasons, ["halt failed"])
        self.assertNotIn("motion.release", [m["op"] for m in original.commands])
        self.assertIsNone(svc._transport._active_connection)

    def test_new_legacy_move_cannot_inherit_finishing_home_connection(self):
        svc, original = service(enabled=True)
        replacement = HomePeer()
        operation_released = threading.Event()
        allow_home_cleanup = threading.Event()
        legacy_started = threading.Event()
        errors = []
        real_finish = svc._transport._finish_operation
        replacement_request = replacement.request
        home_thread = None

        def delayed_finish(cancel):
            if threading.current_thread() is home_thread:
                # Expose the legitimate unlock-before-owner-cleanup window.
                svc._transport._operation_lock.release()
                operation_released.set()
                allow_home_cleanup.wait(5)
                with svc._transport._active_lock:
                    if svc._transport._active_cancel is cancel:
                        svc._transport._active_cancel = None
                        svc._transport._active_connection = None
            else:
                real_finish(cancel)

        def legacy_request(message, timeout):
            if message["op"] == "motion.move":
                replacement.commands.append(copy.deepcopy(message))
                replacement.positions = {"pan": 1.0, "tilt": 8.0}
                legacy_started.set()
                return {"v": 1, "id": message["id"], "status": "completed"}
            return replacement_request(message, timeout)

        def home():
            try:
                svc.move_home({"tilt": 8})
            except Exception as exc:
                errors.append(exc)

        def legacy():
            try:
                svc._transport.move({"yawServo": {"angle": 10, "speed": 1000}}, 10)
            except Exception as exc:
                errors.append(exc)

        replacement.request = legacy_request
        svc._transport._finish_operation = delayed_finish
        home_thread = threading.Thread(target=home)
        legacy_thread = threading.Thread(target=legacy)
        home_thread.start()
        self.assertTrue(operation_released.wait(4))
        original.closed = True
        svc._gateway.peer = replacement
        legacy_thread.start()
        try:
            self.assertTrue(legacy_started.wait(1))
            svc._transport.halt()
        finally:
            allow_home_cleanup.set()
            home_thread.join(2)
            legacy_thread.join(2)
        self.assertFalse(home_thread.is_alive())
        self.assertFalse(legacy_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([m["op"] for m in replacement.commands][-2:],
                         ["lease.acquire", "motion.halt"])
        self.assertEqual(original.close_reasons, [])
        self.assertIsNone(svc._transport._active_connection)

    def test_safety_policy_stretches_home_duration_in_one_frame(self):
        policy = SimpleNamespace(motion=SimpleNamespace(max_speed=1))
        svc, peer = service(enabled=True, policy=policy)
        errors = []

        def move():
            try:
                svc.move_home({"tilt": 8}, 2)
            except StackChanMotionCancelled:
                pass
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=move)
        worker.start()
        self.assertTrue(peer.motion_started.wait(1))
        command = next(m for m in peer.commands if m["op"] == "motion.move_home")
        self.assertEqual(command["args"]["duration_ms"], 5500)
        svc.halt()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_http_rejects_wrong_frame_booleans_unknown_fields_and_sleep(self):
        svc, peer = service(enabled=True)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        request = {"coordinate_frame": HOME_FRAME, "positions": {"tilt": 8}, "duration": 2}
        invalid = [{**request, "coordinate_frame": "legacy"},
                   {k: v for k, v in request.items() if k != "coordinate_frame"},
                   {**request, "positions": {"tilt": True}}, {**request, "duration": True},
                   {**request, "positions": {"tilt": "8"}}, {**request, "extra": "ignored?"}]
        with patch.object(state, "animation_service", svc), patch.object(state, "_sleeping", False):
            for body in invalid:
                with self.subTest(body=body):
                    self.assertEqual(client.post("/stackchan/home/move", json=body).status_code, 422)
            self.assertEqual(client.get("/stackchan/home").status_code, 200)
            with patch.object(state, "_sleeping", True):
                self.assertEqual(client.post("/stackchan/home/move", json=request).status_code, 409)
            svc._home_commissioning_enabled = False
            self.assertEqual(client.post("/stackchan/home/move", json=request).status_code, 403)
        self.assertEqual(peer.commands, [])

    def test_real_websocket_retains_capability_and_http_runs_home_protocol(self):
        svc = StackChanMotionService(device_id="home-test", token=TOKEN,
                                    host="127.0.0.1", port=0, allow_insecure_ws=True,
                                    home_commissioning_enabled=True)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        peer = HomePeer()
        errors = []
        svc.start()
        try:
            port = svc._server.socket.getsockname()[1]
            with connect(f"ws://127.0.0.1:{port}/stackchan/body/v1",
                         additional_headers={"Authorization": "Bearer " + TOKEN}) as ws:
                ws.send(json.dumps({"v": 1, "type": "hello", "device_id": "home-test",
                                    "capabilities": sorted(CAPABILITIES)}))
                self.assertEqual(json.loads(ws.recv(timeout=2))["type"], "hello.accepted")
                with self.assertRaises(TimeoutError):
                    ws.recv(timeout=0.1)

                def firmware():
                    try:
                        for raw in ws:
                            command = json.loads(raw)
                            ws.send(json.dumps(peer.request(command, 1)))
                    except ConnectionClosed:
                        pass
                    except Exception as exc:
                        errors.append(exc)

                worker = threading.Thread(target=firmware, daemon=True)
                worker.start()
                try:
                    with patch.object(state, "animation_service", svc), patch.object(state, "_sleeping", False):
                        self.assertTrue(client.get("/stackchan/home").json()["supported"])
                        self.assertEqual(client.get("/stackchan/home/position").json()["positions"],
                                         {"pan": 0.6, "tilt": 2.5})
                        reply = client.post("/stackchan/home/move", json={"coordinate_frame": HOME_FRAME,
                                             "positions": {"tilt": 8}, "duration": 2})
                        self.assertEqual(reply.status_code, 200, reply.text)
                        self.assertTrue(reply.json()["target_reached"])
                        self.assertEqual(reply.json()["positions"]["tilt"], 7.8)
                finally:
                    ws.close()
                    worker.join(3)
                    self.assertFalse(worker.is_alive())
            deadline = time.monotonic() + 1
            while svc.is_connected and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(svc.is_connected)
            self.assertEqual(errors, [])
        finally:
            svc.stop()


if __name__ == "__main__":
    unittest.main()
