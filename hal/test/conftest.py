"""Test-session setup that must run before any HAL module is imported."""

import os
import shutil
import tempfile

_TEST_ROOT = os.path.join(tempfile.gettempdir(), "autonomous-hal-test")

for _var, _leaf in (
    ("HAL_USERS_DIR", "users"),
    ("HAL_STRANGERS_DIR", "strangers"),
    # SpeakerRecognizer.__init__ mkdirs this; the device default is not writable here.
    ("HAL_VOICE_STRANGERS_DIR", "voice_strangers"),
    # Switch sidecars outlive the process; a shared path leaks sleep state across runs.
    ("HAL_STATE_DIR", "state"),
):
    os.environ.setdefault(_var, os.path.join(_TEST_ROOT, _leaf))

# Start every session from empty; only the directory this file owns is removed.
if os.environ["HAL_STATE_DIR"].startswith(_TEST_ROOT):
    shutil.rmtree(os.environ["HAL_STATE_DIR"], ignore_errors=True)
os.makedirs(os.environ["HAL_STATE_DIR"], exist_ok=True)


import json
from pathlib import Path

import pytest


@pytest.fixture
def lamp_presets():
    """Read fresh device declarations so tests follow tuning without sharing mutations."""
    path = Path(__file__).resolve().parents[2] / "robots" / "lamp" / "presets.json"
    return json.loads(path.read_text(encoding="utf-8"))
