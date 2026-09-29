"""The enrollment gate around starting the voice pipeline."""

import hal.app_state as state


class _FakeVoiceService:
    def __init__(self):
        self.starts = 0

    def start(self):
        self.starts += 1


def test_start_is_allowed_when_no_enrollment_is_running(monkeypatch):
    voice = _FakeVoiceService()
    monkeypatch.setattr(state, "voice_service", voice)
    monkeypatch.setattr(state, "_enrolling", False)

    assert state.start_voice_service("unit-test") is True
    assert voice.starts == 1


def test_start_is_refused_while_an_enrollment_owns_the_mic(monkeypatch):
    voice = _FakeVoiceService()
    monkeypatch.setattr(state, "voice_service", voice)
    monkeypatch.setattr(state, "_enrolling", True)

    assert state.start_voice_service("unit-test") is False
    assert voice.starts == 0


def test_start_is_a_no_op_when_the_pipeline_does_not_exist(monkeypatch):
    monkeypatch.setattr(state, "voice_service", None)
    monkeypatch.setattr(state, "_enrolling", False)

    assert state.start_voice_service("unit-test") is False


def test_every_caller_outside_record_enroll_goes_through_the_gate():
    """Guard against a seventh call site reintroducing the race."""
    import pathlib
    import re

    hal_root = pathlib.Path(state.__file__).resolve().parent
    direct = []
    for path in hal_root.rglob("*.py"):
        if ".venv" in path.parts or "test" in path.parts:
            continue
        for line in path.read_text().splitlines():
            if re.search(r"voice_service\.start\(\)", line):
                direct.append(str(path.relative_to(hal_root)))

    assert sorted(direct) == ["app_state.py", "routes/speaker.py"], direct
