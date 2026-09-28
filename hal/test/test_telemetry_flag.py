"""The telemetry on/off switch and the hot-swap-safe playback hooks."""

import json
import re
from pathlib import Path

from hal.telemetry import client, tts_hooks

HAL_ROOT = Path(__file__).resolve().parents[1]


def test_sending_is_off_without_an_endpoint(monkeypatch, caplog):
    """No endpoint configured: nothing leaves the device — but the local log
    still has it, which is the whole point of logging before sending."""
    monkeypatch.delenv(client.ENV_ANALYTICS_URL, raising=False)
    sent = []
    monkeypatch.setattr(client, "_ensure_worker", lambda: sent.append("worker"))

    with caplog.at_level("INFO", logger="hal.telemetry"):
        client.report("voice_metrics_interaction", {"outcome": "acknowledged"})

    assert sent == [], "no worker may start while sending is disabled"
    assert "[telemetry] voice_metrics_interaction" in caplog.text
    assert "acknowledged" in caplog.text


def test_the_endpoint_is_the_switch(monkeypatch):
    monkeypatch.setenv(client.ENV_ANALYTICS_URL, "https://example.test/api")
    assert client.enabled() is True
    for value in ("", "   "):
        monkeypatch.setenv(client.ENV_ANALYTICS_URL, value)
        assert client.enabled() is False, repr(value)


def test_offline_log_preserves_ids_for_amendment_correlation(monkeypatch, caplog):
    monkeypatch.delenv(client.ENV_ANALYTICS_URL, raising=False)
    with caplog.at_level("INFO", logger="hal.telemetry"):
        client.report("voice_metrics_interaction", {
            "interaction_id": "vi-original", "amends_event_id": "",
        }, event_id="int-vi-original")
        client.report("voice_metrics_interaction", {
            "interaction_id": "vi-original", "amends_event_id": "int-vi-original",
        }, event_id="amend-correction")
    marker = "[telemetry] voice_metrics_interaction "
    rows = [json.loads(r.getMessage().split(marker, 1)[1])
            for r in caplog.records if marker in r.getMessage()]
    assert rows[1]["amends_event_id"] == rows[0]["event_id"]
    assert rows[1]["event_id"] == "amend-correction"


def test_a_configured_endpoint_lets_the_event_through(monkeypatch):
    monkeypatch.setenv(client.ENV_ANALYTICS_URL, "https://example.test/api")
    started = []
    monkeypatch.setattr(client, "_ensure_worker", lambda: started.append(1))
    monkeypatch.setattr(client._queue, "put_nowait", lambda payload: None)

    client.report("voice_metrics_interaction", {"outcome": "acknowledged"})
    assert started == [1]


# --- hot swap ---------------------------------------------------------------

def test_every_tts_construction_site_wires_the_playback_hooks():
    """/voice/start hot-swaps TTSService when the provider or voice changes.
    A site that forgets these keeps speaking while the metrics go blind, so every
    construction site must pass the shared hooks.

    Read as text rather than imported: importing hal.server initialises the
    production logging directory."""
    sites = 0
    for path in (HAL_ROOT / "runtime.py", HAL_ROOT / "routes" / "voice.py"):
        src = path.read_text()
        # `= TTSService(` only: VirtualTTSService is the simulator and plays
        # no real audio, so it has nothing to report.
        for match in re.finditer(r"=\s*TTSService\((.*?)\n\s*\)", src, re.S):
            body = match.group(1)
            sites += 1
            assert "on_playback_audio=tts_hooks.on_playback_audio" in body, path
            assert "on_playback_done=tts_hooks.on_playback_done" in body, path
            assert "on_playback_muted=tts_hooks.on_playback_muted" in body, path
    assert sites == 2, f"expected both construction sites, found {sites}"


def test_hooks_never_raise_into_the_audio_path(monkeypatch):
    import hal.telemetry.voice_metrics as voice_metrics

    def boom(*_a, **_k):
        raise RuntimeError("tracker exploded")

    monkeypatch.setattr(voice_metrics, "playback_audio", boom)
    monkeypatch.setattr(voice_metrics, "playback_end", boom)

    tts_hooks.on_playback_audio("run:x")   # must not raise
    tts_hooks.on_playback_done()
