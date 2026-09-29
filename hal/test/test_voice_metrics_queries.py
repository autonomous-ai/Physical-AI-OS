"""Execute the documented KPI-1 and KPI-2 aggregations against tracker observations."""

import sqlite3
from pathlib import Path

from hal.telemetry import voice_metrics
from hal.test.test_voice_metrics import FakeTTS, kpi  # noqa: F401 -- shared clock/transport fixture


class _CountIf:
    def __init__(self):
        self.count = 0

    def step(self, condition):
        self.count += bool(condition)

    def finalize(self):
        # SQLite needs a float to match BigQuery's fractional division.
        return float(self.count)


def _aggregate(rows):
    """Run the actual SELECT; only warehouse extraction is replaced by rows."""
    doc = Path(__file__).resolve().parents[2] / "docs" / "voice-metrics.md"
    block = doc.read_text().split("-- KPI-2: stale playback per suppression policy.", 1)[1]
    query = "SELECT\n" + block.split("\nSELECT\n", 1)[1].split("```", 1)[0]
    with sqlite3.connect(":memory:") as db:
        db.create_aggregate("COUNTIF", 1, _CountIf)
        db.execute("CREATE TABLE s (reason TEXT, stale TEXT, complete TEXT, applicable INTEGER)")
        db.executemany("INSERT INTO s VALUES (?, ?, ?, ?)", [
            (row["suppression_reason"], str(row["stale_observed"]).lower(),
             str(row["observation_complete"]).lower(), row["applicable_interactions"])
            for row in rows
        ])
        return db.execute(query).fetchall()


def test_confirmed_stale_is_counted_when_its_turn_outlives_the_window(kpi):
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "old-run")
    kpi.clock.advance(1000)
    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)
    boundary = voice_metrics._watchers[0]
    kpi.clock.advance(20000)
    voice_metrics.playback_audio("run:old-run", FakeTTS(realtime_feedback=True))
    kpi.clock.advance(1000)
    voice_metrics.playback_end()
    kpi.clock.advance(39000)
    voice_metrics._close_boundary(boundary)
    stale = kpi.one(voice_metrics.EVENT_SUPPRESSION)
    assert stale["stale_observed"] is True
    assert stale["observation_complete"] is False

    clean = {**stale, "stale_observed": False, "observation_complete": True}
    unresolved = {**stale, "stale_observed": False}
    inapplicable = {**stale, "applicable_interactions": 0}
    assert _aggregate([stale, clean, unresolved, inapplicable]) == [
        ("explicit_stop", 2, 1, 1, 50.0),
    ]


def test_kpi_is_unavailable_when_only_unresolved_observations_exist():
    unresolved = {
        "suppression_reason": "auto_supersede", "stale_observed": False,
        "observation_complete": False, "applicable_interactions": 1,
    }
    assert _aggregate([unresolved]) == [("auto_supersede", 0, 0, 1, None)]


def _aggregate_ack(rows):
    doc = Path(__file__).resolve().parents[2] / "docs" / "voice-metrics.md"
    block = doc.read_text().split("-- KPI-1: acknowledged within 3s", 1)[1]
    query = "SELECT\n" + block.split("\nSELECT\n", 1)[1].split("```", 1)[0]
    with sqlite3.connect(":memory:") as db:
        db.create_aggregate("COUNTIF", 1, _CountIf)
        db.execute("CREATE TABLE i (ack_schema_version INTEGER, eligible TEXT, outcome TEXT, exclusion_reason TEXT, endpoint_known TEXT, ack_ms INTEGER)")
        db.executemany("INSERT INTO i VALUES (?, ?, ?, ?, ?, ?)", rows)
        return sorted(db.execute(query).fetchall())


def test_kpi1_missing_endpoint_cohort_stays_na_with_visible_coverage():
    rows = [(2, "false", "excluded", "speech_endpoint_unavailable", "false", None)] * 44
    rows += [(2, "false", "excluded", "interrupted_by_user", "false", None)] * 13
    rows += [(2, "false", "excluded", "rejected_non_user", "false", None)] * 30
    assert _aggregate_ack(rows) == [(2, 87, 0, 87, 44, 0, 0, 0, None)]


def test_kpi1_no_ack_remains_in_denominator():
    rows = [
        (2, "true", "acknowledged", "", "true", 300),
        (2, "true", "no_ack", "", "true", None),
        (2, "false", "excluded", "speech_endpoint_unavailable", "false", None),
    ]
    assert _aggregate_ack(rows) == [(2, 3, 2, 1, 1, 2, 1, 1, 50.0)]


def test_kpi1_different_ack_schemas_are_not_combined():
    rows = [
        (1, "true", "acknowledged", "", "true", 300),
        (2, "true", "no_ack", "", "true", None),
    ]
    assert _aggregate_ack(rows) == [
        (1, 1, 1, 0, 0, 1, 0, 1, 100.0),
        (2, 1, 1, 0, 0, 1, 1, 0, 0.0),
    ]
