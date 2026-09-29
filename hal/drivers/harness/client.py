"""Loopback-only OS adapter for an idempotent physical voice-mode gesture."""

import requests

OS_HARNESS_GESTURE_URL = "http://127.0.0.1:5000/api/harness/voice-mode/gesture"


class HarnessGestureError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def request_voice_toggle(gesture_id: str) -> dict:
    return _request_voice_gesture({"gestureId": gesture_id})


def request_voice_disable(gesture_id: str) -> dict:
    return _request_voice_gesture({"gestureId": gesture_id, "action": "disable"})


def request_focus_step(gesture_id: str, direction: str, generation: int) -> dict:
    return _request_voice_gesture(
        {"gestureId": gesture_id, "direction": direction, "generation": generation},
        url="http://127.0.0.1:5000/api/harness/voice-mode/focus", require_focus=False,
    )


def _request_voice_gesture(payload: dict, *, url=OS_HARNESS_GESTURE_URL, require_focus=True) -> dict:
    """Send once; a timeout cannot tell whether the OS already committed."""
    try:
        with requests.Session() as session:
            # Local hardware control must never inherit an HTTP proxy.
            session.trust_env = False
            response = session.post(url,
                                    json=payload, timeout=15,
                                    allow_redirects=False)
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if response.status_code != 200 or payload.get("status") != 1:
            raise HarnessGestureError(data.get("code", "unknown") if isinstance(data, dict) else "unknown")
        if not isinstance(data, dict) or type(data.get("enabled")) is not bool:
            raise HarnessGestureError("unknown")
        if require_focus and data["enabled"] and (not data.get("focusAvailable") or not data.get("agentId")):
            raise HarnessGestureError("focus_unavailable")
        return data
    except (requests.RequestException, ValueError, AttributeError) as exc:
        raise HarnessGestureError("unknown") from exc
