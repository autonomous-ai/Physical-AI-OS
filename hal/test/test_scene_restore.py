"""Scene restoration must not undo sleep after an OTA/service restart."""

import json
from unittest.mock import Mock

import pytest

from hal import app_state as state
from hal.routes import scene


@pytest.fixture
def persisted_scene(tmp_path, monkeypatch):
    path = tmp_path / "scene.json"
    monkeypatch.setattr(scene, "_SCENE_STATE_PATH", path)
    monkeypatch.setattr(scene, "_boot_id", lambda: "current-boot")
    monkeypatch.setattr(state, "_active_scene", None)
    return path


@pytest.mark.parametrize("name", list(scene.SCENE_PRESETS))
def test_sleep_restore_keeps_scene_without_touching_hardware(persisted_scene, monkeypatch, name):
    persisted_scene.write_text(json.dumps({"scene": name, "boot_id": "current-boot"}))
    monkeypatch.setattr(state, "_sleeping", True)
    for flag in ("_mic_muted", "_speaker_muted", "_camera_disabled",
                 "_sleepy_auto_muted_mic", "_sleepy_auto_muted_speaker"):
        monkeypatch.setattr(state, flag, True)
    services = []
    for attr in ("rgb_service", "animation_service", "voice_service", "sensing_service"):
        service = Mock()
        monkeypatch.setattr(state, attr, service)
        services.append(service)
    camera_on = Mock()
    monkeypatch.setattr(state, "_auto_camera_on", camera_on)

    scene.restore_persisted_scene()

    assert scene.list_scenes()["active"] == name
    assert state._sleeping
    assert state._mic_muted and state._speaker_muted and state._camera_disabled
    assert state._sleepy_auto_muted_mic and state._sleepy_auto_muted_speaker
    assert persisted_scene.exists()
    camera_on.assert_not_called()
    for service in services:
        assert service.mock_calls == []


def test_awake_restore_still_activates_scene(persisted_scene, monkeypatch):
    persisted_scene.write_text(json.dumps({"scene": "relax", "boot_id": "current-boot"}))
    monkeypatch.setattr(state, "_sleeping", False)
    activate = Mock()
    monkeypatch.setattr(scene, "activate_scene", activate)

    scene.restore_persisted_scene()

    activate.assert_called_once_with(scene.SceneRequest(scene="relax"))


@pytest.mark.parametrize("name,boot", [("relax", "old-boot"), ("unknown", "current-boot")])
def test_sleep_does_not_restore_invalid_scene(persisted_scene, monkeypatch, name, boot):
    persisted_scene.write_text(json.dumps({"scene": name, "boot_id": boot}))
    monkeypatch.setattr(state, "_sleeping", True)
    scene.restore_persisted_scene()
    assert state._active_scene is None
    assert not persisted_scene.exists()
