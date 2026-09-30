"""A scene's servo hold belongs to the scene: ending the scene frees it, nothing else does."""

from unittest.mock import Mock

import pytest

import hal.app_state as state
from hal.drivers.motors import hold
from hal.models import SceneRequest
from hal.routes import scene


class _Body:
    """Just the hold flags the routes and the tracker write."""

    def __init__(self):
        self._hold_mode = False
        self._hold_explicit = False

    def hold(self, explicit=False):
        self._hold_mode = True
        if explicit:
            self._hold_explicit = True

    def resume(self):
        # What every backend's resume() does: clear the flags directly.
        self._hold_mode = False
        self._hold_explicit = False

    @property
    def is_suppressed(self):
        return self._hold_mode

    @property
    def motion_mode(self):
        return "hold" if self._hold_mode else None


@pytest.fixture
def body(monkeypatch):
    svc = _Body()
    monkeypatch.setattr(state, "animation_service", svc, raising=False)
    monkeypatch.setattr(state, "_active_scene", None)
    return svc


class _InlineThread:
    """Runs the scene's aim-then-hold thread inline so the test sees its result."""

    def __init__(self, target=None, **_kw):
        self._target = target

    def start(self):
        self._target()


@pytest.fixture
def scene_env(body, monkeypatch, tmp_path):
    from hal.routes import led, servo

    monkeypatch.setattr(scene, "_SCENE_STATE_PATH", tmp_path / "scene.json")
    monkeypatch.setattr(scene.threading, "Thread", _InlineThread)
    aims = []
    monkeypatch.setattr(servo, "aim_servo", lambda req: aims.append(req.direction))
    monkeypatch.setattr(led, "restore_led", Mock())
    monkeypatch.setattr(state, "rgb_service", Mock(), raising=False)
    monkeypatch.setattr(state, "sensing_service", None, raising=False)
    monkeypatch.setattr(state, "music_service", None, raising=False)
    for name in ("_stop_current_effect", "_save_user_led_state", "_auto_camera_off",
                 "_auto_camera_on", "_start_scene_speaker_drain",
                 "_cancel_scene_speaker_drain"):
        monkeypatch.setattr(state, name, Mock())
    for flag in ("_mic_muted", "_speaker_muted", "_camera_disabled"):
        monkeypatch.setattr(state, flag, False)
    return aims


# --- the owner set ---------------------------------------------------------

def test_one_owner_claims_and_releases(body):
    hold.claim(body, hold.SCENE)
    assert body._hold_mode and hold.owners(body) == {"scene"}
    assert hold.release(body, hold.SCENE) is True
    assert not body._hold_mode and hold.owners(body) == set()


def test_releasing_one_owner_keeps_the_others_hold(body):
    hold.claim(body, hold.SCENE)
    hold.claim(body, hold.TRACKING)
    hold.release(body, hold.TRACKING)
    assert body._hold_mode
    assert hold.owners(body) == {"scene"}


def test_releasing_an_owner_that_never_claimed_changes_nothing(body):
    hold.claim(body, hold.EXPLICIT)
    assert hold.release(body, hold.SCENE) is False
    assert body._hold_mode and body._hold_explicit


def test_explicit_release_clears_only_the_explicit_flag(body):
    hold.claim(body, hold.EXPLICIT)
    hold.claim(body, hold.SCENE)
    hold.release(body, hold.EXPLICIT)
    assert body._hold_mode and not body._hold_explicit


def test_a_resume_forgets_every_owner(body):
    hold.claim(body, hold.SCENE)
    body.resume()
    hold.claim(body, hold.TRACKING)
    assert hold.owners(body) == {"tracking"}


def test_a_hold_set_outside_the_module_still_reads_as_held(body, monkeypatch):
    monkeypatch.setattr(state, "_active_scene", "reading")
    body._hold_mode = True
    assert hold.holder(body) == "hold"
    assert hold.release(body, hold.SCENE) is False
    assert body._hold_mode


def test_holder_names_the_most_specific_owner(body, monkeypatch):
    monkeypatch.setattr(state, "_active_scene", "reading")
    assert hold.holder(body) is None
    hold.claim(body, hold.TRACKING)
    assert hold.holder(body) == "tracking"
    hold.claim(body, hold.SCENE)
    assert hold.holder(body) == "scene"
    hold.claim(body, hold.EXPLICIT)
    assert hold.holder(body) == "explicit"


def test_a_missing_service_is_not_an_error():
    hold.claim(None, hold.SCENE)
    assert hold.release(None, hold.SCENE) is False
    assert hold.owners(None) == frozenset()
    assert hold.holder(None) is None


# --- scene routes ----------------------------------------------------------

def test_a_reading_scene_holds_after_its_aim(scene_env, body):
    scene.activate_scene(SceneRequest(scene="reading"))
    assert scene_env == ["desk"]
    assert hold.owners(body) == {"scene"}


def test_a_scene_without_hold_releases_the_previous_scene_hold(scene_env, body):
    scene.activate_scene(SceneRequest(scene="reading"))
    scene.activate_scene(SceneRequest(scene="relax"))
    assert not body._hold_mode


def test_scene_off_keeps_an_explicit_hold(scene_env, body):
    hold.claim(body, hold.EXPLICIT)
    scene.activate_scene(SceneRequest(scene="reading"))
    assert hold.owners(body) == {"explicit", "scene"}
    scene.deactivate_scene()
    assert hold.owners(body) == {"explicit"}
    assert body._hold_mode and body._hold_explicit


def test_a_scene_ended_before_its_aim_finished_does_not_hold(scene_env, body, monkeypatch):
    from hal.routes import servo

    # The scene is switched off while the aim is still moving.
    monkeypatch.setattr(servo, "aim_servo", lambda req: scene.deactivate_scene())
    scene.activate_scene(SceneRequest(scene="reading"))
    assert not body._hold_mode


# --- /servo/hold -----------------------------------------------------------

def test_servo_hold_claims_as_explicit(body, monkeypatch):
    from hal.routes import servo

    monkeypatch.setattr(servo, "_svc", lambda: body)
    servo.hold_servos()
    assert hold.owners(body) == {"explicit"}
    assert body._hold_explicit
