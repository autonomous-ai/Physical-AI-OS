"""Endpoint delivery delays must not replace actual speech/playback timestamps."""

import pytest

from hal.telemetry import voice_metrics as metrics
from hal.test.test_voice_metrics import FakeTTS, kpi  # noqa: F401


def test_delayed_endpoint_recovers_owned_filler_and_answer(kpi):
    endpoint = kpi.clock()
    iid = metrics.speech_end("provider_transcript", endpoint_known=False, mode="live")
    kpi.clock.advance(400)
    metrics.playback_audio("interaction:" + iid, FakeTTS(interruptible=True))
    kpi.clock.advance(700)
    metrics.playback_audio("interaction:" + iid, FakeTTS(realtime_reply=True))
    kpi.clock.advance(900)
    metrics.set_endpoint(iid, "provider_audio_timestamp", endpoint)
    kpi.close_all()
    row = kpi.one(metrics.EVENT_INTERACTION)
    assert row["eligible"] and row["outcome"] == "acknowledged"
    assert row["ack_latency_ms"] == 400
    assert row["answer_latency_ms"] == 1100
    assert row["ack_kind"] == "waiting_audio"


def test_delayed_endpoint_amends_reported_missing_endpoint(kpi):
    endpoint = kpi.clock()
    iid = metrics.speech_end("provider_transcript", endpoint_known=False, mode="live")
    kpi.clock.advance(650)
    metrics.playback_audio("interaction:" + iid, FakeTTS(native_mode=True))
    kpi.close_all()
    assert kpi.one(metrics.EVENT_INTERACTION)["ack_latency_ms"] is None
    kpi.clock.advance(1000)
    metrics.set_endpoint(iid, "provider_audio_timestamp", endpoint)
    amended = kpi.of(metrics.EVENT_INTERACTION)[-1]["params"]
    assert amended["amends_event_id"] == "int-" + iid
    assert amended["amendment_reason"] == "late_endpoint"
    assert amended["ack_latency_ms"] == amended["answer_latency_ms"] == 650


@pytest.mark.parametrize("at", [float("nan"), float("inf"), -1, 0, 1001, True, None])
def test_invalid_live_endpoint_never_creates_latency(kpi, at):
    iid = metrics.speech_end("server_vad", at=at, mode="live")
    metrics.playback_audio("interaction:" + iid, FakeTTS(realtime_reply=True))
    metrics.set_endpoint(iid, "server_vad", at)
    kpi.close_all()
    row = kpi.one(metrics.EVENT_INTERACTION)
    assert not row["eligible"]
    assert row["exclusion_reason"] == "speech_endpoint_unavailable"
    assert row["ack_latency_ms"] is None


def test_endpoint_after_playback_is_not_clamped_to_zero(kpi):
    iid = metrics.speech_end("provider_transcript", endpoint_known=False, mode="live")
    metrics.playback_audio("interaction:" + iid, FakeTTS(realtime_reply=True))
    kpi.clock.advance(100)
    metrics.set_endpoint(iid, "server_vad", kpi.clock())
    kpi.close_all()
    row = kpi.one(metrics.EVENT_INTERACTION)
    assert not row["speech_endpoint_known"]
    assert row["ack_latency_ms"] is None


def test_endpoint_without_owned_playback_keeps_no_ack_in_denominator(kpi):
    endpoint = kpi.clock()
    old = metrics.speech_end("provider_transcript", endpoint_known=False, mode="live")
    new = metrics.speech_end("provider_transcript", endpoint_known=False, mode="live")
    kpi.clock.advance(200)
    metrics.playback_audio("interaction:" + old, FakeTTS(interruptible=True))
    metrics.set_endpoint(new, "provider_audio_timestamp", endpoint)
    kpi.close_all()
    row = next(e["params"] for e in kpi.of(metrics.EVENT_INTERACTION)
               if e["params"]["interaction_id"] == new)
    assert row["eligible"] and row["outcome"] == "no_ack"
    assert row["ack_latency_ms"] is None


def test_equal_endpoint_and_playback_is_a_real_zero(kpi):
    iid = metrics.speech_end("provider_transcript", endpoint_known=False, mode="live")
    played_at = kpi.clock()
    metrics.playback_audio("interaction:" + iid, FakeTTS(realtime_reply=True))
    kpi.clock.advance(500)
    metrics.set_endpoint(iid, "provider_audio_timestamp", played_at)
    kpi.close_all()
    assert kpi.one(metrics.EVENT_INTERACTION)["ack_latency_ms"] == 0


def test_provider_ownership_never_falls_back_to_latest_interaction(kpi):
    old = metrics.speech_end("provider_transcript", endpoint_known=False, mode="live")
    metrics.bind_provider_turn(old, "old-provider-key")
    new = metrics.speech_end("provider_transcript", endpoint_known=False, mode="live")
    metrics.bind_provider_turn(new, "new-provider-key")
    assert metrics.provider_interaction("old-provider-key") == old
    assert metrics.provider_interaction("new-provider-key") == new
    assert metrics.provider_interaction("missing") == ""
    assert metrics.provider_interaction("") == ""
    metrics.bind_provider_turn("missing", "orphan-provider-key")
    assert metrics.provider_interaction("orphan-provider-key") == ""


def test_provider_ownership_expires_with_eviction_and_reset(kpi, monkeypatch):
    monkeypatch.setattr(metrics, "_MAX_TRACKED", 2)
    old = metrics.speech_end("provider_transcript", endpoint_known=False, mode="live")
    metrics.bind_provider_turn(old, "old-provider-key")
    metrics.speech_end("provider_transcript", endpoint_known=False, mode="live")
    new = metrics.speech_end("provider_transcript", endpoint_known=False, mode="live")
    metrics.bind_provider_turn(new, "new-provider-key")
    assert metrics.provider_interaction("old-provider-key") == ""
    assert metrics.provider_interaction("new-provider-key") == new
    metrics.reset_for_test()
    assert metrics.provider_interaction("new-provider-key") == ""


def test_reused_provider_key_is_ambiguous_and_binding_is_immutable(kpi):
    old = metrics.speech_end("provider_transcript", endpoint_known=False, mode="live")
    metrics.bind_provider_turn(old, "reused")
    metrics.bind_provider_turn(old, "replacement")
    assert metrics.provider_interaction("replacement") == ""
    new = metrics.speech_end("provider_transcript", endpoint_known=False, mode="live")
    metrics.bind_provider_turn(new, "reused")
    assert metrics.provider_interaction("reused") == ""
