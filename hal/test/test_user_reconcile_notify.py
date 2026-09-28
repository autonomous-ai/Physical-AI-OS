"""Removing an enrollment must not be a half-delete."""

from unittest.mock import patch

import pytest
import requests

from hal.routes import sensing


def test_notify_posts_to_os_server():
    with patch.object(sensing.requests, "post") as post:
        sensing._notify_user_reconcile("face/remove:leo")
    post.assert_called_once()
    assert post.call_args.args[0] == sensing.config.OS_USER_RECONCILE_URL
    assert post.call_args.kwargs["json"] == {"reason": "face/remove:leo"}
    assert post.call_args.kwargs["timeout"] <= 5


def test_notify_never_raises_when_os_server_is_down():
    """notify never raises when os-server is down."""
    with patch.object(
        sensing.requests, "post", side_effect=requests.RequestException("refused")
    ):
        sensing._notify_user_reconcile("face/reset")


@pytest.mark.parametrize(
    "reason_prefix, fn_name",
    [
        ("face/remove", "face_remove"),
        ("face/reset", "face_reset"),
        ("users/rename", "user_rename"),
    ],
)
def test_directory_changing_routes_notify(reason_prefix, fn_name):
    """Only routes that make an enrollment DIRECTORY appear or disappear notify."""
    src = open(sensing.__file__, encoding="utf-8").read()
    fn = src.split(f"def {fn_name}(")[1].split("\n@router")[0]
    assert "_notify_user_reconcile(" in fn, f"{fn_name} does not notify os-server"
    assert reason_prefix in fn


def test_speaker_remove_does_not_notify():
    from hal.routes import speaker

    src = open(speaker.__file__, encoding="utf-8").read()
    assert "_notify_user_reconcile" not in src, (
        "/speaker/remove keeps the user directory (only voice/ is dropped), so "
        "reconciling here would retire someone still enrolled by face"
    )
