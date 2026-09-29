"""Malformed or unreadable poses must not bypass declared motion speed bounds."""

from unittest import mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from hal import app_state as state
from hal.models import ServoMoveRequest
from hal.routes import servo
from hal.safety.policy import MotionBounds, SafetyPolicy


@pytest.fixture
def motor(monkeypatch):
    svc = mock.Mock(is_connected=True)
    svc.get_joint_names.return_value = ['pan.pos']
    svc.get_positions.return_value = {'pan.pos': 0.0}
    monkeypatch.setattr(state, 'animation_service', svc)
    monkeypatch.setattr(state, '_sleeping', False)
    monkeypatch.setattr(state, 'safety_policy', SafetyPolicy(schema='autonomous.safety.v1', motion=MotionBounds(max_speed=120)))
    monkeypatch.setattr(state, 'tracker_service', None)
    monkeypatch.setattr(state, 'policy_service', None)
    return svc


@pytest.mark.parametrize('pose', [{}, {'pan.pos': None}, {'pan.pos': float('nan')}, {'pan.pos': float('inf')}])
def test_unknown_pose_refuses_speed_limited_move(motor, pose):
    motor.get_positions.return_value = pose
    with pytest.raises(HTTPException) as error:
        servo.move_servo(ServoMoveRequest(positions={'pan.pos': 60}, duration=0))
    assert error.value.status_code == 503
    motor.move_and_hold.assert_not_called()


def test_pose_read_failure_refuses_speed_limited_move(motor):
    motor.get_positions.side_effect = RuntimeError('bus unavailable')
    with pytest.raises(HTTPException) as error:
        servo.move_servo(ServoMoveRequest(positions={'pan.pos': 60}, duration=0))
    assert error.value.status_code == 503
    motor.move_and_hold.assert_not_called()


def test_known_pose_still_stretches_duration(motor):
    response = servo.move_servo(ServoMoveRequest(positions={'pan.pos': 60}, duration=0))
    motor.move_and_hold.assert_called_once_with({'pan.pos': 60}, duration=0.5)
    assert response['duration'] == 0.5


def test_absent_policy_preserves_move_behavior(motor, monkeypatch):
    monkeypatch.setattr(state, 'safety_policy', None)
    motor.get_positions.side_effect = RuntimeError('bus unavailable')
    servo.move_servo(ServoMoveRequest(positions={'pan.pos': 60}, duration=0))
    motor.move_and_hold.assert_called_once_with({'pan.pos': 60}, duration=0)


@pytest.mark.parametrize('target', ['NaN', 'Infinity', '-Infinity'])
def test_nonfinite_target_rejected_over_http(motor, target):
    app = FastAPI()
    app.include_router(servo.router)
    with TestClient(app) as client:
        response = client.post('/servo/move', json={'positions': {'pan.pos': target}})
    assert response.status_code == 422
    motor.move_and_hold.assert_not_called()


def test_stop_cancels_tracker_even_when_motor_disconnected(motor, monkeypatch):
    motor.is_connected = False
    tracker = mock.Mock(is_tracking=True)
    monkeypatch.setattr(state, 'tracker_service', tracker)
    with pytest.raises(HTTPException) as error:
        servo.stop_servos()
    assert error.value.status_code == 503
    tracker.stop.assert_called_once()
    motor.halt.assert_not_called()


def test_stop_halts_connected_motor_after_tracker(motor, monkeypatch):
    tracker = mock.Mock(is_tracking=True)
    monkeypatch.setattr(state, 'tracker_service', tracker)
    ordered = mock.Mock()
    ordered.attach_mock(tracker.stop, 'stop_tracker')
    ordered.attach_mock(motor.halt, 'halt_motor')
    assert servo.stop_servos() == {'status': 'ok'}
    assert ordered.mock_calls == [mock.call.stop_tracker(), mock.call.halt_motor()]


@pytest.mark.parametrize('path,body', [
    ('/servo/move', {'positions': {'pan.pos': 10}}),
    ('/servo/aim', {'direction': 'left'}),
    ('/servo/nudge', {'yaw': 20}),
    ('/servo/resume', {}),
    ('/servo/track', {'target': 'person'}),
])
def test_sleep_refusal_is_visible_over_http(motor, monkeypatch, path, body):
    monkeypatch.setattr(state, '_sleeping', True)
    app = FastAPI()
    app.include_router(servo.router)
    with TestClient(app) as client:
        response = client.post(path, json=body)
    assert response.status_code == 409
    assert 'sleeping' in response.json()['detail']
    assert motor.mock_calls == []
    assert state._sleeping


def test_move_does_not_claim_requested_target_was_clamped(motor):
    motor.get_positions.side_effect = [{'pan.pos': 95}, {'pan.pos': 100}]
    app = FastAPI()
    app.include_router(servo.router)
    with TestClient(app) as client:
        response = client.post('/servo/move', json={'positions': {'pan.pos': 115}, 'duration': 0})
    assert response.status_code == 200
    data = response.json()
    assert data['requested'] == {'pan.pos': 115}
    assert data['clamped'] is None
    assert data['actual'] == {'pan.pos': 100}
    motor.move_and_hold.assert_called_once_with({'pan.pos': 115}, duration=pytest.approx(20 / 120))


@pytest.mark.parametrize('path,body,method', [
    ('/servo/nudge', {'yaw': 20}, 'nudge'),
    ('/servo/aim', {'direction': 'left'}, 'aim'),
])
def test_aim_and_nudge_report_observed_pose(motor, path, body, method):
    motor.get_positions.side_effect = [{'pan.pos': 95}, {'pan.pos': 100}]
    getattr(motor, method).return_value = {'pan.pos': 115}
    app = FastAPI()
    app.include_router(servo.router)
    with TestClient(app) as client:
        response = client.post(path, json=body)
    assert response.status_code == 200
    assert response.json()['requested'] == {'pan.pos': 115}
    assert response.json()['positions'] == {'pan.pos': 100}


def test_nudge_readback_failure_does_not_invent_position_or_retry(motor):
    motor.get_positions.side_effect = [{'pan.pos': 95}, RuntimeError('bus unavailable')]
    motor.nudge.return_value = {'pan.pos': 115}
    app = FastAPI()
    app.include_router(servo.router)
    with TestClient(app) as client:
        response = client.post('/servo/nudge', json={'yaw': 20})
    assert response.status_code == 200
    assert response.json()['positions'] == {}
    assert response.json()['requested'] == {'pan.pos': 115}
    assert 'read_position' in response.json()['errors']
    motor.nudge.assert_called_once()


@pytest.mark.parametrize('reported', [{}, {'pan.pos': float('nan')}, {'pan.pos': float('inf')}])
def test_move_invalid_readback_is_reported_without_inventing_position(motor, reported):
    motor.get_positions.side_effect = [{'pan.pos': 0}, reported]
    response = servo.move_servo(ServoMoveRequest(positions={'pan.pos': 10}))
    assert response['actual'] == {}
    assert response['clamped'] is None
    assert 'read_position' in response['errors']
    motor.move_and_hold.assert_called_once()
