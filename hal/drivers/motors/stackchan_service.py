"""Stack-chan motion driver over the authenticated local Wi-Fi protocol.

The ESP32 initiates one WebSocket connection to HAL.  HAL owns the controller
lease and sends acknowledged, timed pan/tilt commands; the firmware owns the
physical interpolation, measured-position readback, torque and the independent
disconnect/lease-expiry halt path.

Joint model exposed to HAL (degrees):

    base_yaw.pos       -30 .. 30
    base_pitch.pos     -15 .. 15 (relative to Stack-chan's 45 degree midpoint)

Configuration is environment-driven because SDK-backed motion services are
constructed by hal.server with only ``safety_policy``:

    STACKCHAN_BODY_HOST       bind address (default 0.0.0.0)
    STACKCHAN_BODY_PORT       bind port (default 8765)
    STACKCHAN_DEVICE_ID       expected firmware device id
    STACKCHAN_BODY_TOKEN      shared bearer token, at least 32 characters
    STACKCHAN_BODY_TLS_CERT   optional PEM certificate path
    STACKCHAN_BODY_TLS_KEY    optional PEM private-key path
    STACKCHAN_BODY_ALLOW_INSECURE_WS
                              set to 1 only for trusted-network development
    STACKCHAN_BODY_COMMAND_TIMEOUT
                              command timeout in seconds (default 3.0)
    STACKCHAN_BODY_LEASE_TTL_MS
                              controller lease TTL (default 1500)

TLS certificate and key must be supplied together. Plain WebSocket is refused
unless the explicit development-only opt-in is enabled. Production firmware
connects by WSS and validates the configured certificate.
"""
from __future__ import annotations

import hmac
import json
import logging
import math
import os
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import Server, ServerConnection, serve

from hal import presets as P
from hal.safety.policy import min_move_duration

logger = logging.getLogger("hal.motion.stackchan")

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 16 * 1024
JOINT_KEYS: Set[str] = {"base_yaw.pos", "base_pitch.pos"}
_JOINT_LIMITS = {"base_yaw.pos": 30.0, "base_pitch.pos": 15.0}
_PITCH_MIDPOINT_DEG = 45.0
HOME_CAPABILITY = "motion.home_degrees.v1"
HOME_FRAME = "calibrated_home_deg_v1"
# A commissioning band with margin above the firmware's 5-degree hold floor.
# This is not a calibration or a general replacement for legacy movement.
HOME_PITCH_LIMITS = (7.0, 10.0)
_HOME_YAW_DRIFT_DEG = 1.0
_HOME_HOLD_MARGIN_DEG = 6.0
_HOME_FEEDBACK_INTERVAL_S = 0.25
_FIRMWARE_SPEED = 1000  # Timed firmware uses duration_ms; speed is legacy shape compatibility.
_MIN_MOVE_DURATION_S = 0.05
_MAX_MOVE_DURATION_S = 60.0
_FIRMWARE_SETTLE_GRACE_S = 0.05
_RELEASE_DURATION_S = 2.0
_TARGET_SETTLE_TIMEOUT_S = 2.0
_POSITION_TOLERANCE_DEG = 1.0
_HOME_MIN_PROGRESS_DEG = 1.0       # measured progress required before any arrival or repeat decision
_HOME_RECOMMAND_LIMIT = 1          # at most one repeat of the same target after a stable, short settle
_HOME_RECOMMAND_MAX_SHORT_DEG = 3.0  # do not repeat if the head is further than this from target
_HOME_STABLE_SPAN_DEG = 0.2        # last three samples within this span count as stable
_GRAVITY_REST = {"base_yaw.pos": 0.0, "base_pitch.pos": -15.0}
_REQUIRED_CAPABILITIES = {
    "motion.pan_tilt",
    "motion.measured_position",
    "motion.halt_hold",
    "motion.torque_release",
    "motion.timed_move",
}
_TERMINAL_STATES = {"completed", "error"}
_AIM_TARGETS: Dict[str, Dict[str, float]] = {
    P.AIM_CENTER: {"base_yaw.pos": 0.0, "base_pitch.pos": 0.0},
    P.AIM_DESK: {"base_yaw.pos": 0.0, "base_pitch.pos": -10.0},
    P.AIM_WALL: {"base_yaw.pos": 0.0, "base_pitch.pos": 8.0},
    P.AIM_LEFT: {"base_yaw.pos": -20.0},
    P.AIM_RIGHT: {"base_yaw.pos": 20.0},
    P.AIM_UP: {"base_pitch.pos": 10.0},
    P.AIM_DOWN: {"base_pitch.pos": -10.0},
    P.AIM_USER: {"base_yaw.pos": 0.0, "base_pitch.pos": 5.0},
}


class StackChanTransportError(RuntimeError):
    """The command failed or its delivery result is unknown."""


class StackChanMotionCancelled(StackChanTransportError):
    """A recovery command interrupted commissioning before it completed."""


class StackChanOffline(StackChanTransportError):
    """No authenticated Stack-chan firmware is connected."""


class StackChanCommandRejected(StackChanTransportError):
    """The firmware answered a command with an error code.

    The firmware has already handled the physical state (held, released, or
    faulted); HAL reports the code and keeps the transport open.
    """

    def __init__(self, op: str, code: str):
        super().__init__(f"Stack-chan rejected {op}: {code}")
        self.op = op
        self.code = code


class StackChanTargetNotReached(StackChanTransportError):
    """A timed move settled short of its measured target; HAL halted and the body is held."""


class StackChanCommissioningFailed(StackChanTransportError):
    """Home commissioning halted and held without reaching its target.

    The body stays connected; ``details`` carries the measured evidence.
    """

    def __init__(self, reason: str, details: dict[str, Any]):
        super().__init__(reason)
        self.details = {"reason": reason, **details}


def _json_object(raw: str | bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise StackChanTransportError("invalid Stack-chan protocol message") from exc
    if not isinstance(value, dict) or value.get("v") != PROTOCOL_VERSION:
        raise StackChanTransportError("unsupported Stack-chan protocol message")
    return value


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean flag")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass
class _Pending:
    condition: threading.Condition = field(default_factory=threading.Condition)
    terminal: Optional[dict[str, Any]] = None


class _DeviceConnection:
    def __init__(self, socket: ServerConnection, capabilities: Optional[set[str]] = None):
        self.socket = socket
        self.capabilities = frozenset(capabilities or ())
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self.closed = False

    def request(self, message: dict[str, Any], timeout: float) -> dict[str, Any]:
        command_id = message["id"]
        pending = _Pending()
        with self._pending_lock:
            if self.closed:
                raise StackChanOffline("Stack-chan is offline")
            self._pending[command_id] = pending
        try:
            encoded = json.dumps(message, separators=(",", ":"), allow_nan=False)
            with self._send_lock:
                self.socket.send(encoded)
            deadline = time.monotonic() + timeout
            with pending.condition:
                while pending.terminal is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        # Closing the socket invokes the firmware's independent
                        # measured-position halt.  Unknown delivery is never retried.
                        self.force_close("command timeout")
                        raise StackChanTransportError(
                            "Stack-chan command timed out; delivery is unknown"
                        )
                    pending.condition.wait(remaining)
                result = pending.terminal
            if result.get("status") == "error":
                code = str(result.get("code", "device_error"))
                if code == "device_disconnected":
                    # Synthesized by close(): the socket dropped while waiting.
                    # No firmware reply exists; delivery is unknown.
                    raise StackChanOffline("Stack-chan went offline while waiting; delivery is unknown")
                # The firmware answered, so it has already applied its own
                # fail-closed handling. Record the code; the caller decides.
                logger.warning("[stackchan] firmware rejected %s: %s", message.get("op"), code)
                raise StackChanCommandRejected(str(message.get("op")), code)
            return result
        except StackChanTransportError:
            raise
        except Exception as exc:
            raise StackChanTransportError(
                "Stack-chan connection failed; delivery is unknown"
            ) from exc
        finally:
            with self._pending_lock:
                self._pending.pop(command_id, None)

    def deliver(self, message: dict[str, Any]) -> None:
        command_id = message.get("id")
        status = message.get("status")
        if not isinstance(command_id, str) or status not in _TERMINAL_STATES | {"accepted"}:
            return
        with self._pending_lock:
            pending = self._pending.get(command_id)
        if pending is None or status == "accepted":
            return
        with pending.condition:
            pending.terminal = message
            pending.condition.notify_all()

    def close(self) -> None:
        with self._pending_lock:
            self.closed = True
            pending = list(self._pending.values())
        for item in pending:
            with item.condition:
                item.terminal = {
                    "v": PROTOCOL_VERSION,
                    "id": "",
                    "status": "error",
                    "code": "device_disconnected",
                }
                item.condition.notify_all()

    def force_close(self, reason: str) -> None:
        try:
            self.socket.close(code=1011, reason=reason)
        except Exception:
            pass


class _BodyGateway:
    """Own the device-facing WebSocket and publish one authenticated connection."""

    def __init__(self, device_id: str, token: str):
        if not device_id or len(device_id) > 64:
            raise ValueError("STACKCHAN_DEVICE_ID must contain 1 to 64 characters")
        if len(token) < 32:
            raise ValueError("STACKCHAN_BODY_TOKEN must contain at least 32 characters")
        self.device_id = device_id
        self._token = token
        self._lock = threading.Lock()
        self._connection: Optional[_DeviceConnection] = None

    def connection(self) -> _DeviceConnection:
        with self._lock:
            connection = self._connection
        if connection is None or connection.closed:
            raise StackChanOffline("Stack-chan is offline")
        return connection

    @property
    def connected(self) -> bool:
        try:
            self.connection()
            return True
        except StackChanOffline:
            return False

    def handle(self, socket: ServerConnection) -> None:
        request = socket.request
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {self._token}"
        if request.path != "/stackchan/body/v1" or not hmac.compare_digest(supplied, expected):
            socket.close(code=1008, reason="unauthorized")
            return

        connection: Optional[_DeviceConnection] = None
        try:
            hello = _json_object(socket.recv(timeout=5))
            capabilities = hello.get("capabilities")
            if (
                hello.get("type") != "hello"
                or hello.get("device_id") != self.device_id
                or not isinstance(capabilities, list)
                or not all(isinstance(capability, str) for capability in capabilities)
                or not _REQUIRED_CAPABILITIES.issubset(set(capabilities))
            ):
                socket.close(code=1008, reason="incompatible hello")
                return

            connection = _DeviceConnection(socket, set(capabilities))
            with self._lock:
                if self._connection is not None and not self._connection.closed:
                    socket.close(code=1008, reason="device already connected")
                    return
                # Publish only after acceptance is on the wire. HAL callers
                # cannot send commands while the firmware is still handshaking.
                socket.send(json.dumps({
                    "v": PROTOCOL_VERSION,
                    "type": "hello.accepted",
                    "device_id": self.device_id,
                }, separators=(",", ":")))
                self._connection = connection
            logger.info("[stackchan] authenticated firmware connected (%s)", self.device_id)
            for raw in socket:
                connection.deliver(_json_object(raw))
        except (ConnectionClosed, TimeoutError, StackChanTransportError):
            pass
        finally:
            if connection is not None:
                connection.close()
                with self._lock:
                    if self._connection is connection:
                        self._connection = None
                logger.info("[stackchan] firmware disconnected; device watchdog owns halt")

    def close_connection(self, reason: str) -> None:
        try:
            self.connection().force_close(reason)
        except StackChanOffline:
            pass


class _BodyTransport:
    """Serialize HAL operations through the firmware controller lease."""

    def __init__(self, gateway: _BodyGateway, timeout: float, lease_ttl_ms: int, safety_policy: Any = None):
        if not 0 < timeout <= 10:
            raise ValueError("command timeout must be between 0 and 10 seconds")
        if not 250 <= lease_ttl_ms <= 5000:
            raise ValueError("lease TTL must be between 250 and 5000 milliseconds")
        self._gateway = gateway
        self._safety_policy = safety_policy
        self._timeout = timeout
        self._ttl = lease_ttl_ms
        self._session = uuid.uuid4().hex
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active_cancel: Optional[threading.Event] = None
        self._active_connection: Optional[_DeviceConnection] = None
        self._target_settle_timeout = _TARGET_SETTLE_TIMEOUT_S

    def _request(
        self,
        op: str,
        args: Optional[dict[str, Any]] = None,
        *,
        sequenced: bool = True,
        connection: Optional[_DeviceConnection] = None,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "id": uuid.uuid4().hex,
            "op": op,
            "session": self._session,
        }
        if args is not None:
            message["args"] = args
        if sequenced:
            with self._sequence_lock:
                self._sequence += 1
                message["seq"] = self._sequence
        peer = connection if connection is not None else self._gateway.connection()
        return peer.request(message, self._timeout)

    def _acquire(self, connection: Optional[_DeviceConnection] = None) -> None:
        self._request("lease.acquire", {"ttl_ms": self._ttl}, sequenced=False, connection=connection)

    def home_capability(self) -> dict[str, bool]:
        try:
            peer = self._gateway.connection()
        except StackChanOffline:
            return {"connected": False, "supported": False}
        return {"connected": True, "supported": HOME_CAPABILITY in peer.capabilities}

    def _home_connection(self) -> _DeviceConnection:
        peer = self._gateway.connection()
        if HOME_CAPABILITY not in peer.capabilities:
            raise NotImplementedError("Firmware does not advertise " + HOME_CAPABILITY)
        return peer

    def _read_home_positions(self, peer: _DeviceConnection) -> dict[str, float]:
        result = self._request("motion.get_home", sequenced=False, connection=peer).get("result")
        if not isinstance(result, dict) or result.get("coordinate_frame") != HOME_FRAME:
            raise StackChanTransportError("Stack-chan returned an invalid saved-home coordinate frame")
        positions = result.get("positions")
        if (not isinstance(positions, dict) or set(positions) != {"pan", "tilt"}
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or not math.isfinite(v) or abs(v) > 312.5 for v in positions.values())):
            raise StackChanTransportError("Stack-chan returned invalid saved-home measured positions")
        return {key: float(value) for key, value in positions.items()}

    def get_home_positions(self) -> dict[str, float]:
        if not self._operation_lock.acquire(blocking=False):
            raise StackChanTransportError("controller busy; position was not read")
        try:
            return self._read_home_positions(self._home_connection())
        finally:
            self._operation_lock.release()

    @staticmethod
    def _recommand_worthwhile(phase_samples: list[dict[str, float]], initial: dict[str, float], target: float) -> bool:
        """Stable, moved in the right direction, and short by more than tolerance but not far."""
        if len(phase_samples) < 2:
            return False
        recent = [sample["tilt"] for sample in phase_samples[-3:]]
        if max(recent) - min(recent) > _HOME_STABLE_SPAN_DEG:
            return False
        tilt = recent[-1]
        short_by = target - tilt
        return (tilt - initial["tilt"] >= _HOME_MIN_PROGRESS_DEG
                and tilt >= _HOME_HOLD_MARGIN_DEG
                and _POSITION_TOLERANCE_DEG < short_by <= _HOME_RECOMMAND_MAX_SHORT_DEG)

    @staticmethod
    def _check_home_yaw(measured: dict[str, float], initial: dict[str, float]) -> None:
        if abs(measured["pan"] - initial["pan"]) > _HOME_YAW_DRIFT_DEG:
            raise StackChanTransportError("Uncommanded yaw drift during home commissioning")

    @staticmethod
    def _home_arrived(measured: dict[str, float], initial: dict[str, float], target: float) -> bool:
        _BodyTransport._check_home_yaw(measured, initial)
        return (abs(measured["tilt"] - target) <= _POSITION_TOLERANCE_DEG
                and measured["tilt"] >= _HOME_HOLD_MARGIN_DEG
                and measured["tilt"] - initial["tilt"] >= _HOME_MIN_PROGRESS_DEG)

    def commission_home(self, target: float, duration: float) -> dict[str, Any]:
        cancel = threading.Event()
        if not self._active_lock.acquire(blocking=False):
            raise StackChanTransportError("controller busy; commissioning was not queued")
        try:
            if not self._operation_lock.acquire(blocking=False):
                raise StackChanTransportError("controller busy; commissioning was not queued")
            try:
                peer = self._home_connection()
            except Exception:
                self._operation_lock.release()
                raise
            self._active_cancel = cancel
            self._active_connection = peer
        finally:
            self._active_lock.release()

        def check_cancel():
            if cancel.is_set():
                raise StackChanMotionCancelled("Home commissioning was halted before completion")

        def request(op, args=None, *, sequenced=True):
            check_cancel()
            return self._request(op, args, sequenced=sequenced, connection=peer)

        try:
            initial = self._read_home_positions(peer)
            # Reject an unsuitable starting pose before acquiring any lease.
            if not (abs(initial["pan"]) <= 30 and 0 <= initial["tilt"] < 5):
                raise ValueError("Home commissioning requires measured pan within +/-30 and pitch in [0, 5) degrees")
            target = round(target * 10) / 10.0
            duration = min_move_duration(self._safety_policy,
                                         {"base_pitch.pos": target},
                                         {"base_pitch.pos": initial["tilt"]}, duration)
            duration = math.ceil(duration * 1000) / 1000.0
            if not math.isfinite(duration) or not 2 <= duration <= _MAX_MOVE_DURATION_S:
                raise ValueError("Safe home commissioning duration must be between 2 and 60 seconds")
            check_cancel()
            request("lease.acquire", {"ttl_ms": self._ttl}, sequenced=False)
            move_args = {"duration_ms": round(duration * 1000),
                         "motion": {"pitchServo": {"angle": round(target * 10)}}}
            renew_s = max(0.1, self._ttl / 2000.0)
            samples: list[dict[str, float]] = []
            recommands = 0
            measured = dict(initial)
            move_started = time.monotonic()
            while True:
                if recommands:
                    try:
                        # Refresh the lease immediately before the repeat, then send it.
                        # If either is refused (for example the lease lapsed and the
                        # firmware already holds on its own), keep the evidence.
                        request("lease.renew", {"ttl_ms": self._ttl}, sequenced=False)
                        request("motion.move_home", move_args)
                    except StackChanMotionCancelled:
                        raise
                    except StackChanTransportError as exc:
                        reason = f"Home commissioning re-command was not accepted: {exc}"
                        logger.warning("[stackchan] commissioning failed: %s (%d samples)", reason, len(samples))
                        raise StackChanCommissioningFailed(reason, {
                            "initial": initial, "target": {"tilt": target}, "final": measured,
                            "samples": samples, "recommands": recommands,
                            "terminal_command": "none; firmware holds on lease expiry"}) from exc
                else:
                    request("motion.move_home", move_args)
                phase_started = time.monotonic()
                deadline = phase_started + duration + _FIRMWARE_SETTLE_GRACE_S
                while True:
                    check_cancel()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    cancel.wait(min(_HOME_FEEDBACK_INTERVAL_S, renew_s, remaining))
                    check_cancel()
                    # Yaw has no torque during this pitch-only trajectory. Check
                    # it while moving, including repeats and stretched durations,
                    # instead of leaving the guard blind until settling begins.
                    try:
                        measured = self._read_home_positions(peer)
                        self._check_home_yaw(measured, initial)
                    except StackChanTransportError:
                        request("motion.halt")
                        raise
                    if time.monotonic() < deadline:
                        request("lease.renew", {"ttl_ms": self._ttl}, sequenced=False)

                phase_samples: list[dict[str, float]] = []
                deadline = time.monotonic() + self._target_settle_timeout
                arrived = False
                while True:
                    check_cancel()
                    try:
                        measured = self._read_home_positions(peer)
                        sample = {"elapsed_s": round(time.monotonic() - move_started, 3),
                                  "pan": measured["pan"], "tilt": measured["tilt"], "move": recommands + 1}
                        samples.append(sample)
                        phase_samples.append(sample)
                        logger.info("[stackchan] commissioning sample elapsed=%.3f pan=%s tilt=%s target=%s move=%d",
                                    sample["elapsed_s"], sample["pan"], sample["tilt"], target, recommands + 1)
                        arrived = self._home_arrived(measured, initial, target)
                    except StackChanTransportError:
                        # Invalid feedback, a replaced peer, or yaw drift: halt the
                        # scheduled move ourselves rather than waiting for lease expiry.
                        request("motion.halt")
                        raise
                    if arrived:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    request("lease.renew", {"ttl_ms": self._ttl}, sequenced=False)
                    cancel.wait(min(0.05, remaining))
                if arrived:
                    break
                if recommands < _HOME_RECOMMAND_LIMIT and self._recommand_worthwhile(phase_samples, initial, target):
                    # The servo stopped short but is holding still. Repeat the same
                    # bounded target once so it rebuilds its error term from rest.
                    recommands += 1
                    logger.info("[stackchan] commissioning re-command %d: measured tilt=%s target=%s",
                                recommands, measured["tilt"], target)
                    continue
                # Halt-and-hold is the fail-closed action. The body stays
                # connected so the measured evidence survives the failure.
                request("motion.halt")
                reason = "Home commissioning did not reach a measured holdable target"
                logger.warning("[stackchan] commissioning failed: %s (final pan=%s tilt=%s, %d samples, %d re-commands)",
                               reason, measured["pan"], measured["tilt"], len(samples), recommands)
                raise StackChanCommissioningFailed(reason, {
                    "initial": initial, "target": {"tilt": target}, "final": measured,
                    "samples": samples, "recommands": recommands, "terminal_command": "motion.halt"})

            # This performs a measured hold of BOTH axes; it is not torque-off.
            request("lease.release", sequenced=False)
            check_cancel()
            final = self._read_home_positions(peer)
            check_cancel()
            if not self._home_arrived(final, initial, target):
                raise StackChanTransportError("Home commissioning position changed after terminal hold")
            return {"initial_positions": initial, "positions": final,
                    "commanded_positions": {"tilt": target}, "duration": duration,
                    "target_reached": True, "terminal_state": "held", "recommands": recommands}
        except StackChanMotionCancelled:
            # The concurrent halt owns the terminal command after this operation
            # relinquishes its lock. Never report this move as successful.
            raise
        except Exception as exc:
            # Do not close the transport here. Firmware halts and holds on its
            # own when the lease lapses, and closing only forces a reboot that
            # destroys the evidence of what happened. Command timeouts still
            # close inside request(), where delivery is genuinely unknown.
            logger.warning("[stackchan] commissioning ended without success: %s", exc)
            raise
        finally:
            self._finish_operation(cancel)

    def _read_positions(self, connection: Optional[_DeviceConnection] = None) -> dict[str, float]:
        result = self._request("motion.get", sequenced=False, connection=connection).get("result", {})
        positions = result.get("positions") if isinstance(result, dict) else None
        if not isinstance(positions, dict):
            raise StackChanTransportError("Stack-chan returned invalid measured positions")
        pan = positions.get("pan")
        tilt = positions.get("tilt")
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value))
            for value in (pan, tilt)
        ):
            raise StackChanTransportError("Stack-chan returned invalid measured positions")
        return {"base_yaw.pos": float(pan), "base_pitch.pos": float(tilt)}

    def get_positions(self) -> dict[str, float]:
        if not self._operation_lock.acquire(blocking=False):
            raise StackChanTransportError("controller busy; position was not read")
        try:
            return self._read_positions()
        finally:
            self._operation_lock.release()

    @staticmethod
    def _expected_positions(native: dict[str, Any]) -> dict[str, float]:
        expected: dict[str, float] = {}
        if "yawServo" in native:
            expected["base_yaw.pos"] = native["yawServo"]["angle"] / 10.0
        if "pitchServo" in native:
            expected["base_pitch.pos"] = native["pitchServo"]["angle"] / 10.0 - 45.0
        return expected

    def _wait_for_measured_target(
        self,
        native: dict[str, Any],
        cancel: threading.Event,
        connection: Optional[_DeviceConnection] = None,
    ) -> None:
        expected = self._expected_positions(native)
        deadline = time.monotonic() + self._target_settle_timeout
        while not cancel.is_set():
            measured = self._read_positions(connection)
            if all(
                abs(measured[joint] - target) <= _POSITION_TOLERANCE_DEG
                for joint, target in expected.items()
            ):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Keep torque enabled and end motion before reporting failure.
                # The halt succeeded, so the body is held; the caller must not
                # close the transport, which would only reboot a held body.
                self._request("motion.halt", connection=connection)
                raise StackChanTargetNotReached(
                    "Stack-chan did not reach its measured target"
                )
            self._request("lease.renew", {"ttl_ms": self._ttl}, sequenced=False, connection=connection)
            cancel.wait(min(0.05, remaining))

    def _run_timed_motion(
        self,
        native: dict[str, Any],
        duration: float,
        cancel: threading.Event,
        terminal_op: str,
        terminal_sequenced: bool,
        halt_before_measurement: bool = False,
        connection: Optional[_DeviceConnection] = None,
    ) -> bool:
        self._acquire(connection)
        if self._safety_policy is not None:
            if halt_before_measurement:
                # A release may supersede interpolation still running on the
                # firmware. Pin it before measuring the start of the rest move.
                self._request("motion.halt", connection=connection)
                self._acquire(connection)
            duration = min_move_duration(
                self._safety_policy,
                self._expected_positions(native),
                self._read_positions(connection),
                duration,
            )
            if not math.isfinite(duration) or duration > _MAX_MOVE_DURATION_S:
                raise StackChanTransportError("safe move duration exceeds the 60 second limit")
        # Round up so wire quantization cannot shorten a safety-stretched move.
        duration = math.ceil(duration * 1000) / 1000
        self._request("motion.move", {
            "motion": native,
            "duration_ms": round(duration * 1000),
        }, connection=connection)

        # A firmware lease is deliberately shorter than the API's move limit.
        # Renew while interpolation runs so loss of this process, socket or Wi-Fi
        # stops the servos within one TTL. One short grace covers frame rounding.
        deadline = time.monotonic() + duration + _FIRMWARE_SETTLE_GRACE_S
        renew_s = max(0.1, self._ttl / 2000.0)
        while not cancel.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if cancel.wait(min(renew_s, remaining)):
                break
            if time.monotonic() < deadline:
                self._request("lease.renew", {"ttl_ms": self._ttl}, sequenced=False, connection=connection)

        # Firmware schedules fixed steps; scheduler stalls extend the actual
        # trajectory. Elapsed host time is not evidence of physical arrival.
        if not cancel.is_set():
            self._wait_for_measured_target(native, cancel, connection)
        if cancel.is_set():
            return False
        self._request(terminal_op, sequenced=terminal_sequenced, connection=connection)
        return True

    def _finish_operation(self, cancel: threading.Event) -> None:
        # Release the operation before taking _active_lock. A superseding
        # release holds _active_lock while it waits to claim this operation.
        self._operation_lock.release()
        with self._active_lock:
            if self._active_cancel is cancel:
                self._active_cancel = None
                self._active_connection = None

    def move(self, native: dict[str, Any], duration: float) -> None:
        cancel = threading.Event()
        if not self._active_lock.acquire(blocking=False):
            raise StackChanTransportError("controller busy; motion was not queued")
        try:
            # Publish move ownership atomically with acquiring the operation
            # lock. A recovery command holding _active_lock also rejects this
            # move instead of leaving it queued to start after recovery returns.
            if not self._operation_lock.acquire(blocking=False):
                raise StackChanTransportError("controller busy; motion was not queued")
            try:
                connection = self._gateway.connection()
            except Exception:
                self._operation_lock.release()
                raise
            self._active_cancel = cancel
            self._active_connection = connection
        finally:
            self._active_lock.release()
        try:
            # lease.release performs measured halt-and-hold after interpolation.
            self._run_timed_motion(
                native,
                duration,
                cancel,
                "lease.release",
                terminal_sequenced=False,
                connection=connection,
            )
        except (StackChanCommandRejected, StackChanTargetNotReached):
            # Firmware refused the command (and has held, released, or faulted,
            # or its lease lapses within one TTL), or HAL already halted a short
            # move. Either way the body is stopped; closing would only reboot it.
            raise
        except StackChanTransportError:
            connection.force_close("motion failed")
            raise
        finally:
            self._finish_operation(cancel)

    def halt(self) -> None:
        with self._active_lock:
            connection = self._active_connection or self._gateway.connection()
            cancel = self._active_cancel
            if cancel is not None:
                cancel.set()
            # Wait for the cancelled operation to stop issuing commands, then
            # send halt last on the wire. Holding _active_lock makes concurrent
            # moves fail instead of queuing behind this recovery action.
            if not self._operation_lock.acquire(timeout=self._timeout + 0.5):
                connection.force_close("halt could not claim controller")
                raise StackChanTransportError("controller did not stop for halt")
            try:
                # Reacquire or renew after the prior operation is fully done.
                # This covers the small window after lease.release succeeds but
                # before the old cancellation marker is cleared.
                self._acquire(connection)
                self._request("motion.halt", connection=connection)
            except StackChanCommandRejected:
                # halt_failed means the firmware already released torque and
                # faulted the session; the body is stopped and still online.
                raise
            except StackChanTransportError:
                connection.force_close("halt failed")
                raise
            finally:
                self._operation_lock.release()

    def release(self, native_rest: dict[str, Any], duration: float) -> None:
        cancel = threading.Event()
        with self._active_lock:
            # Recovery belongs to the operation being interrupted, even if the
            # gateway already has a replacement connection. Never move that peer.
            connection = self._active_connection or self._gateway.connection()
            previous = self._active_cancel
            if previous is not None:
                previous.set()
            # Keep _active_lock while waiting: this blocks a new move from
            # slipping between the cancelled operation and recovery ownership.
            if not self._operation_lock.acquire(timeout=self._timeout + 0.5):
                connection.force_close("release could not claim controller")
                raise StackChanTransportError("controller did not stop for release")
            self._active_cancel = cancel
            self._active_connection = connection
        try:
            # Move to the head-down soft-limit rest before disabling torque.
            # motion.release then halt-holds measured position before torque-off.
            completed = self._run_timed_motion(
                native_rest,
                duration,
                cancel,
                "motion.release",
                terminal_sequenced=True,
                halt_before_measurement=True,
                connection=connection,
            )
        except (StackChanCommandRejected, StackChanTargetNotReached):
            # release_failed: firmware attempted both torque-offs and faulted.
            # A short rest move was halted by HAL and is held.
            raise
        except StackChanTransportError:
            connection.force_close("release failed")
            raise
        finally:
            self._finish_operation(cancel)
        if not completed:
            raise StackChanTransportError("release was halted before torque-off")


class StackChanMotionService:
    """MotionService implementation backed by Stack-chan autonomous firmware."""

    def __init__(
        self,
        safety_policy: Any = None,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        device_id: Optional[str] = None,
        token: Optional[str] = None,
        tls_cert: Optional[str] = None,
        tls_key: Optional[str] = None,
        command_timeout: Optional[float] = None,
        lease_ttl_ms: Optional[int] = None,
        allow_insecure_ws: Optional[bool] = None,
        home_commissioning_enabled: Optional[bool] = None,
    ):
        self._safety_policy = safety_policy
        self._home_commissioning_enabled = (
            home_commissioning_enabled if home_commissioning_enabled is not None
            else _env_flag("STACKCHAN_HOME_COMMISSIONING_ENABLED")
        )
        self._host = host or os.getenv("STACKCHAN_BODY_HOST", "0.0.0.0")
        self._port = port if port is not None else _env_int("STACKCHAN_BODY_PORT", 8765, 1, 65535)
        self._device_id = device_id or os.getenv("STACKCHAN_DEVICE_ID", "")
        self._token = token or os.getenv("STACKCHAN_BODY_TOKEN", "")
        self._tls_cert = tls_cert or os.getenv("STACKCHAN_BODY_TLS_CERT")
        self._tls_key = tls_key or os.getenv("STACKCHAN_BODY_TLS_KEY")
        if bool(self._tls_cert) != bool(self._tls_key):
            raise ValueError("STACKCHAN_BODY_TLS_CERT and STACKCHAN_BODY_TLS_KEY must be set together")
        self._allow_insecure_ws = (
            allow_insecure_ws
            if allow_insecure_ws is not None
            else _env_flag("STACKCHAN_BODY_ALLOW_INSECURE_WS")
        )
        if not self._tls_cert and not self._allow_insecure_ws:
            raise ValueError(
                "Stack-chan requires TLS; set STACKCHAN_BODY_ALLOW_INSECURE_WS=1 "
                "only for trusted-network development"
            )
        timeout = command_timeout if command_timeout is not None else float(
            os.getenv("STACKCHAN_BODY_COMMAND_TIMEOUT", "3.0")
        )
        ttl = lease_ttl_ms if lease_ttl_ms is not None else _env_int(
            "STACKCHAN_BODY_LEASE_TTL_MS", 1500, 250, 5000
        )
        self._gateway = _BodyGateway(self._device_id, self._token)
        self._transport = _BodyTransport(self._gateway, timeout, ttl, safety_policy)
        self._server: Optional[Server] = None
        self._server_thread: Optional[threading.Thread] = None
        self._state_lock = threading.RLock()
        self._tracking_flag = False
        self._body_owners = 0
        self._zero_mode = False
        self._hold_mode = False
        self._released = False
        self._frozen = False
        self._current_recording: Optional[str] = None
        self._target: Dict[str, float] = {joint: 0.0 for joint in JOINT_KEYS}

    # --- Lifecycle ---

    def start(self, skip_wake: bool = False) -> None:
        del skip_wake  # Connecting doesn't perform a wake animation.
        if self._server is not None:
            return
        ssl_context = None
        if self._tls_cert and self._tls_key:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(self._tls_cert, self._tls_key)
        # serve() binds synchronously, so a port/configuration failure reaches
        # hal.server instead of being lost in the background thread.
        self._server = serve(
            self._gateway.handle,
            self._host,
            self._port,
            max_size=MAX_MESSAGE_BYTES,
            compression=None,
            server_header=None,
            ssl=ssl_context,
        )
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="stackchan-body-gateway",
        )
        self._server_thread.start()
        logger.info(
            "[stackchan] listening on %s:%d (%s), waiting for %s",
            self._host,
            self._port,
            "wss" if ssl_context else "ws",
            self._device_id,
        )

    def stop(self, timeout: float = 5.0) -> None:
        if self.is_connected:
            try:
                self.halt()
            except Exception as exc:
                logger.warning("[stackchan] halt during shutdown failed: %s", exc)
        server = self._server
        self._server = None
        if server is not None:
            # websockets 12 exposes shutdown() without the connection-closing
            # arguments added later. Close our one device explicitly first.
            self._gateway.close_connection("HAL stopping")
            server.shutdown()
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=timeout)
        self._server_thread = None

    @property
    def is_connected(self) -> bool:
        return self._gateway.connected

    # --- Animation / event dispatch ---

    def dispatch(self, event_type: str, payload: Any) -> None:
        # Stack-chan has no HAL recording library yet. Direct aim/nudge/move
        # remain available; accepting an invented animation would be misleading.
        logger.debug("[stackchan] dispatch %s (%r) ignored", event_type, payload)

    def get_available_recordings(self) -> List[str]:
        return []

    def add_recording(self, name: str, actions: List[Dict[str, float]]) -> None:
        logger.warning("[stackchan] add_recording(%r) is unsupported", name)

    def ensure_running(self) -> None:
        pass

    @property
    def is_suppressed(self) -> bool:
        with self._state_lock:
            return self._zero_mode or self._hold_mode or self._released

    @property
    def motion_mode(self) -> Optional[str]:
        with self._state_lock:
            if self._released:
                return "released"
            if self._zero_mode:
                return "zero"
            if self._hold_mode:
                return "hold"
        return None

    # --- Freeze ---

    def freeze(self) -> None:
        with self._state_lock:
            self._frozen = True

    def unfreeze(self) -> None:
        with self._state_lock:
            self._frozen = False

    @property
    def is_frozen(self) -> bool:
        with self._state_lock:
            return self._frozen

    # --- Body ownership ---

    @property
    def _tracking_active(self) -> bool:
        with self._state_lock:
            return self._tracking_flag or self._body_owners > 0

    @_tracking_active.setter
    def _tracking_active(self, value: bool) -> None:
        # A tracking session cannot release an overlapping aim/capture owner.
        with self._state_lock:
            self._tracking_flag = bool(value)

    def acquire_body(self) -> None:
        with self._state_lock:
            self._body_owners += 1

    def release_body(self) -> None:
        with self._state_lock:
            self._body_owners = max(0, self._body_owners - 1)

    # --- Motion primitives ---

    @staticmethod
    def _validated(positions: Dict[str, float]) -> Dict[str, float]:
        if not positions:
            raise ValueError("provide at least one Stack-chan joint")
        unknown = set(positions) - JOINT_KEYS
        if unknown:
            raise ValueError(f"unsupported Stack-chan joints: {sorted(unknown)}")
        result: Dict[str, float] = {}
        for joint, value in positions.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{joint} must be a finite angle")
            angle = float(value)
            limit = _JOINT_LIMITS[joint]
            if abs(angle) > limit:
                raise ValueError(f"{joint} must be within +/-{limit:g} degrees")
            result[joint] = angle
        return result

    @staticmethod
    def _native(positions: Dict[str, float]) -> Dict[str, dict[str, int]]:
        native: Dict[str, dict[str, int]] = {}
        if "base_yaw.pos" in positions:
            native["yawServo"] = {
                "angle": round(positions["base_yaw.pos"] * 10),
                "speed": _FIRMWARE_SPEED,
            }
        if "base_pitch.pos" in positions:
            native["pitchServo"] = {
                "angle": round((_PITCH_MIDPOINT_DEG + positions["base_pitch.pos"]) * 10),
                "speed": _FIRMWARE_SPEED,
            }
        return native

    def move_to(self, target_positions: Dict[str, float], duration: float = 2.0) -> None:
        positions = self._validated(target_positions)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration):
            raise ValueError("duration must be a finite number")
        if duration < 0:
            raise ValueError("duration must be non-negative")
        if duration > _MAX_MOVE_DURATION_S:
            raise ValueError(f"duration must be at most {_MAX_MOVE_DURATION_S:g} seconds")
        effective = max(float(duration), _MIN_MOVE_DURATION_S)
        self._transport.move(self._native(positions), effective)
        with self._state_lock:
            self._target.update(positions)
            self._released = False

    def move_and_hold(self, target_positions: Dict[str, float], duration: float = 2.0) -> None:
        self.move_to(target_positions, duration)

    def home_state(self) -> dict[str, Any]:
        capability = self._transport.home_capability()
        return {**capability, "coordinate_frame": HOME_FRAME,
                "commissioning_configured": self._home_commissioning_enabled,
                "motion_enabled": self._home_commissioning_enabled and capability["supported"],
                "target_limits_deg": {"tilt": list(HOME_PITCH_LIMITS)},
                "motion_scope": "pitch_only_commissioning",
                "start_pitch_limits_deg": {"minimum": 0, "exclusive_maximum": 5},
                "terminal_state": "both_axes_held", "calibration_verified": False}

    def get_home_positions(self) -> dict[str, Any]:
        return {"coordinate_frame": HOME_FRAME, "position_source": "measured",
                "positions": self._transport.get_home_positions()}

    def move_home(self, positions: dict[str, float], duration: float = 2.0) -> dict[str, Any]:
        if not self._home_commissioning_enabled:
            raise PermissionError("Home commissioning disabled; explicitly enable STACKCHAN_HOME_COMMISSIONING_ENABLED for supervised use")
        if not isinstance(positions, dict) or set(positions) != {"tilt"}:
            raise ValueError("Home commissioning accepts only a numeric tilt target")
        target = positions["tilt"]
        if (isinstance(target, bool) or not isinstance(target, (int, float))
                or not math.isfinite(target) or not HOME_PITCH_LIMITS[0] <= target <= HOME_PITCH_LIMITS[1]):
            raise ValueError("Home commissioning pitch must be between 7 and 10 degrees")
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or not 2 <= duration <= 10):
            raise ValueError("Home commissioning duration must be between 2 and 10 seconds")
        with self._state_lock:
            if self._body_owners or self._tracking_flag or self._frozen:
                raise PermissionError("Home commissioning requires an idle, unfrozen body")
        result = self._transport.commission_home(float(target), float(duration))
        with self._state_lock:
            self._target.update({"base_yaw.pos": result["positions"]["pan"],
                                 "base_pitch.pos": result["positions"]["tilt"] - _PITCH_MIDPOINT_DEG})
            self._released = False
        return {"status": "ok", "coordinate_frame": HOME_FRAME, "position_source": "measured",
                "requested": positions, **result}

    def get_joint_names(self) -> Set[str]:
        return set(JOINT_KEYS)

    def get_positions(self) -> Dict[str, float]:
        positions = self._transport.get_positions()
        with self._state_lock:
            self._target.update(positions)
        return positions

    def send_positions(self, positions: Dict[str, float]) -> None:
        current = self.get_positions()
        duration = min_move_duration(self._safety_policy, positions, current, _MIN_MOVE_DURATION_S)
        self.move_to(positions, duration)

    # --- Postures & modes ---

    def zero_pose(self) -> None:
        with self._state_lock:
            self._zero_mode = True
        self.move_to({joint: 0.0 for joint in JOINT_KEYS}, 2.0)

    def release(self) -> Dict[str, str]:
        try:
            self.release_checked()
        except Exception as exc:
            return {"stackchan": str(exc)}
        return {}

    def release_checked(self) -> None:
        """Bench release preserving diagnostics; the OS keeps its error-dict contract."""
        self._transport.release(self._native(_GRAVITY_REST), _RELEASE_DURATION_S)
        with self._state_lock:
            self._released = True

    def halt(self) -> None:
        try:
            self._transport.halt()
        except StackChanOffline:
            # Offline is already the firmware's independent stop trigger.
            logger.info("[stackchan] halt requested while offline; watchdog owns hold")

    def resume(self) -> None:
        with self._state_lock:
            was_released = self._released
            self._zero_mode = False
            self._hold_mode = False
        if was_released:
            # A same-pose timed command re-enables torque without a jump.
            self.move_to(self.get_positions(), _MIN_MOVE_DURATION_S)
        with self._state_lock:
            self._released = False

    def hold(self, explicit: bool = False) -> None:
        del explicit  # Stack-chan has no scene-change animation exception.
        with self._state_lock:
            self._hold_mode = True
        self.halt()

    def joint_status(self) -> Dict[str, dict]:
        positions = self.get_positions()
        return {
            "base_yaw": {"online": True, "id": 1, "angle": round(positions["base_yaw.pos"], 1)},
            "base_pitch": {"online": True, "id": 2, "angle": round(positions["base_pitch.pos"], 1)},
        }

    # --- Aim & nudge ---

    def aim(
        self,
        direction: str,
        duration: float,
        current_positions: Dict[str, float],
        safety_policy: Any,
    ) -> Dict[str, float]:
        target = _AIM_TARGETS.get(direction)
        if target is None:
            logger.warning("[stackchan] unknown aim direction %r; using center", direction)
            target = _AIM_TARGETS[P.AIM_CENTER]
        positions = {**current_positions, **target}
        effective = min_move_duration(safety_policy, positions, current_positions, duration)
        self.move_to(positions, effective)
        return positions

    def nudge(
        self,
        yaw: float,
        pitch: float,
        duration: float,
        current_positions: Dict[str, float],
        safety_policy: Any,
    ) -> Dict[str, float]:
        positions = dict(current_positions)
        positions["base_yaw.pos"] = positions.get("base_yaw.pos", 0.0) + yaw
        positions["base_pitch.pos"] = positions.get("base_pitch.pos", 0.0) + pitch
        positions = self._validated(positions)
        effective = min_move_duration(safety_policy, positions, current_positions, duration)
        self.move_to(positions, effective)
        return positions
