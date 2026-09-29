"""Face file routes reject escaped paths, including matching-prefix siblings."""

import pytest
from fastapi import HTTPException

from hal.drivers.sensing.perceptions.processors import facerecognizer_v2
from hal.drivers.voice import music_service
from hal.routes import sensing


@pytest.fixture
def users_root(monkeypatch, tmp_path):
    root = tmp_path / "users"
    (root / "alice").mkdir(parents=True)
    monkeypatch.setattr(facerecognizer_v2, "USERS_DIR", root)
    monkeypatch.setattr(music_service, "_USERS_DIR", root)
    return root


@pytest.mark.parametrize("route", [sensing.face_photo, sensing.face_file])
def test_face_file_routes_allow_contained_file(users_root, route):
    (users_root / "alice" / "photo.jpg").write_bytes(b"photo")
    assert route("alice", "photo.jpg").body == b"photo"


@pytest.mark.parametrize("route", [sensing.face_photo, sensing.face_file])
@pytest.mark.parametrize("via_symlink", [False, True])
def test_face_file_routes_reject_matching_prefix_escape(users_root, route, via_symlink):
    sibling = users_root.with_name("users-private")
    sibling.mkdir()
    (sibling / "secret.jpg").write_bytes(b"private")
    if via_symlink:
        (users_root / "alice" / "escape").symlink_to(sibling, target_is_directory=True)
        path = "escape/secret.jpg"
    else:
        path = "../../users-private/secret.jpg"
    with pytest.raises(HTTPException) as error:
        route("alice", path)
    assert error.value.status_code == 400
