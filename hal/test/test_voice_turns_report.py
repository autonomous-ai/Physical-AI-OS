"""The offline turn report reads the lines HAL writes and nothing else."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bench" / "voice_turns.py"
spec = importlib.util.spec_from_file_location("voice_turns", SCRIPT)
voice_turns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice_turns)

LOG = """\
2026-10-08 10:00:00,001 INFO [voice-metrics] speech end (interaction=vi-aaaa method=silence_clock)
2026-10-08 10:00:00,120 INFO [turn-timing] interaction=vi-aaaa speech_end_to_commit_ms=118
2026-10-08 10:00:01,900 INFO [turn-timing] interaction=vi-aaaa commit_to_first_output_ms=1780
2026-10-08 10:00:02,400 INFO [turn-timing] interaction=vi-aaaa speech_end_to_first_speech_ms=2399
2026-10-08 10:00:02,900 INFO [voice-metrics] ack (interaction=vi-aaaa kind=reply latency_ms=2899)
2026-10-08 10:00:02,900 INFO [voice-metrics] answer (interaction=vi-aaaa kind=reply latency_ms=2899)
2026-10-08 10:00:02,950 INFO [turn] route=handled → realtime (main agent notified, stays silent) (event=voice, stt='what time is it', interaction_id=vi-aaaa)
2026-10-08 10:00:10,000 INFO [admission] short answer 'yeah' admitted: a question is pending
2026-10-08 10:00:10,001 INFO [voice-metrics] speech end (interaction=vi-bbbb method=smart_turn)
2026-10-08 10:00:10,050 INFO [turn] route=noise_dropped → nowhere (noise guard rejected turn) (event=voice, stt='(empty)', interaction_id=vi-bbbb)
"""


def test_report_joins_timing_ack_and_route_per_utterance():
    turns, admissions = voice_turns.parse(LOG.splitlines())
    assert [t["interaction_id"] for t in turns] == ["vi-aaaa", "vi-bbbb"]
    first = turns[0]
    assert first["endpoint"] == "silence_clock"
    assert first["speech_end_to_commit_ms"] == 118
    assert first["commit_to_first_output_ms"] == 1780
    assert first["speech_end_to_first_speech_ms"] == 2399
    assert first["ack_latency_ms"] == 2899 and first["answer_kind"] == "reply"
    assert first["route"] == "handled" and first["transcript"] == "'what time is it'"
    assert turns[1]["route"] == "noise_dropped"
    assert admissions == ["short answer 'yeah' admitted: a question is pending"]


def test_percentiles_and_rendering():
    assert voice_turns.percentile([], 50) is None
    assert voice_turns.percentile([100, 200, 300, 400], 50) == 250
    assert voice_turns.percentile([100, 200, 300, 400], 95) == 385
    turns, admissions = voice_turns.parse(LOG.splitlines())
    text = voice_turns.render(turns, admissions)
    assert "2 utterance(s)" in text
    assert "speech_end_to_commit_ms" in text and "handled=1" in text and "noise_dropped=1" in text
