"""[WS] log lines must not carry base64 camera frames (#530)."""

from lbserver.app import _loggable_ws_text

B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAIBAQEBAQIBAQECAg" * 1000


def test_frame_replaced_by_its_length():
    msg = '{"type": "frame", "task": "action", "frame_b64": "' + B64 + '"}'
    assert _loggable_ws_text(msg) == (
        f'{{"type": "frame", "task": "action", "frame_b64": "<{len(B64)} chars>"}}'
    )


def test_fields_after_the_frame_are_kept():
    msg = '{"type":"frame","frame_b64":"' + B64 + '","ts":123}'
    out = _loggable_ws_text(msg)
    assert B64[:20] not in out
    assert out.endswith('"ts":123}')


def test_message_without_frame_is_unchanged():
    msg = '{"type": "config", "task": "pose", "threshold": 0.5}'
    assert _loggable_ws_text(msg) == msg


def test_non_json_text_is_unchanged():
    assert _loggable_ws_text("ping") == "ping"


def test_output_capped_at_100_chars():
    msg = '{"type": "config", "note": "' + "x" * 500 + '"}'
    assert len(_loggable_ws_text(msg)) == 100
