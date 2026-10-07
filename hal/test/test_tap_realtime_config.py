"""Manual device input must own realtime turn boundaries on every restart."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("live", ["true", "false"])
@pytest.mark.parametrize("turn_detection", ["off", "server_vad", "semantic_vad"])
def test_tap_input_disables_provider_endpointing_and_restores_automatic(
    live, turn_detection, tmp_path,
):
    path = tmp_path / "config.json"
    env = dict(
        os.environ,
        OS_CONFIG_PATH=str(path),
        PYTHONDONTWRITEBYTECODE="1",
        HAL_LIVE_MODE=live,
        HAL_REALTIME_TURN_DETECTION=turn_detection,
    )
    script = """
import json
import os
from hal import config
from hal.realtime.config import GeminiConfig, OpenAIConfig
print(json.dumps({
    "live": config.LIVE_MODE,
    "detection": config.REALTIME_TURN_DETECTION,
    "gemini_vad": GeminiConfig().vad_enabled,
    "openai_detection": OpenAIConfig().turn_detection_type,
    "saved_live": os.environ["HAL_LIVE_MODE"],
    "saved_detection": os.environ["HAL_REALTIME_TURN_DETECTION"],
}))
"""
    # Reload through a fresh process, as the OS does when input mode changes.
    for mode in ("tap_to_talk", "automatic"):
        path.write_text(json.dumps({"voice_input_mode": mode}))
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        actual = json.loads(result.stdout.strip().splitlines()[-1])
        expected_live = mode == "automatic" and live == "true"
        expected_detection = (
            "off" if mode == "tap_to_talk"
            else "server_vad" if expected_live and turn_detection == "off"
            else turn_detection
        )
        assert actual == {
            "live": expected_live,
            "detection": expected_detection,
            "gemini_vad": expected_detection != "off",
            "openai_detection": None if expected_detection == "off" else expected_detection,
            "saved_live": live,
            "saved_detection": turn_detection,
        }
