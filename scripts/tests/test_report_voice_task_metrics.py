import importlib.util
import json
import io
from unittest.mock import patch
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    "report_voice_task_metrics", Path(__file__).resolve().parents[1] / "report_voice_task_metrics.py")
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)


def turn(identifier="v1", **overrides):
    params = dict(interaction_id=identifier, run_id="run-" + identifier,
                  task_schema_version=1, task_revision=1, task_started_at_ms=1000,
                  task_eligible=True, task_eligibility_known=True, route="agent")
    params.update(overrides)
    return dict(event_name="voice_metrics_interaction", device_id="lamp", params=params)


def execution(identifier="v1", **overrides):
    params = dict(run_id="run-" + identifier, schema_version=1,
                  outcome="completed", evidence="lifecycle_end", execution_at_ms=90000)
    params.update(overrides)
    return dict(event_name="voice_metrics_task_execution", device_id="lamp", params=params)


def started(identifier="v1", **overrides):
    params = dict(interaction_id=identifier, run_id="run-" + identifier,
                  schema_version=1, task_started_at_ms=1000, event_type="voice_followup")
    params.update(overrides)
    return dict(event_name="voice_metrics_task_started", device_id="lamp", params=params)


class ReportTests(unittest.TestCase):
    def summary(self, rows):
        return metrics.report(rows, now_ms=2000000)["aggregate"]

    def test_os_start_without_hal_is_eligible_and_can_complete(self):
        result = self.summary([started("os-voice-1"), execution("os-voice-1")])
        self.assertEqual(result["eligible_mature_turns"], 1)
        self.assertEqual(result["completed_turns"], 1)

    def test_os_and_hal_cohorts_deduplicate_by_interaction_or_run(self):
        for os_start in (started(), started("fallback", run_id="run-v1")):
            result = self.summary([turn(), os_start, execution(), execution()])
            self.assertEqual(result["eligible_mature_turns"], 1)
            self.assertEqual(result["completed_turns"], 1)

    def test_binding_keeps_earliest_start_and_latest_nonempty_run(self):
        initial = started(run_id="", task_started_at_ms=1000)
        bound = started(run_id="run-v1", task_started_at_ms=300000)
        later_empty = started(run_id="", task_started_at_ms=400000)
        for rows in ([initial, bound, later_empty], [later_empty, bound, initial]):
            result = metrics.report(rows + [execution()], 2000000,
                                    settle_seconds=1800)["aggregate"]
            self.assertEqual(result["eligible_mature_turns"], 1)
            self.assertEqual(result["completed_turns"], 1)
        newer_binding = started(run_id="new-run", task_started_at_ms=500000)
        result = self.summary([newer_binding, bound, initial, execution()])
        self.assertEqual(result["incomplete_turns"], 1)

    def test_transport_observation_loss_never_overrides_terminal(self):
        lost = execution(outcome="unknown", evidence="execution_observation_lost",
                         execution_at_ms=100000)
        result = self.summary([started(), lost])
        self.assertEqual(result["unknown_turns"], 1)
        self.assertEqual(result["completed_turns"], 0)
        for outcome in ("completed", "failed"):
            with self.subTest(outcome=outcome):
                terminal = execution(outcome=outcome)
                for rows in ([started(), terminal, lost], [lost, terminal, started()]):
                    result = self.summary(rows)
                    self.assertEqual(result[outcome + "_turns"], 1)
                    self.assertEqual(result["unknown_turns"], 0)

    def test_started_without_terminal_is_immediately_in_denominator(self):
        result = self.summary([started(task_started_at_ms=1999999)])
        self.assertEqual(result["eligible_mature_turns"], 1)
        self.assertEqual(result["incomplete_turns"], 1)
        self.assertEqual(result["kpi3_pct"], 0)

    def test_hundred_started_eighty_five_completed_is_eighty_five_percent(self):
        rows = [started(str(i)) for i in range(100)]
        rows += [execution(str(i)) for i in range(85)]
        result = self.summary(rows)
        self.assertEqual(result["eligible_mature_turns"], 100)
        self.assertEqual(result["completed_turns"], 85)
        self.assertEqual(result["kpi3_pct"], 85)
        self.assertTrue(result["meets_target"])

    def test_os_acceptance_upgrades_legacy_and_excluded_hal(self):
        for hal in (turn(task_schema_version=None), turn(task_eligible=False),
                    turn("other", run_id="run-v1", task_eligible=False)):
            result = self.summary([hal, started(), execution()])
            self.assertEqual(result["eligible_mature_turns"], 1)
            self.assertEqual(result["completed_turns"], 1)
            self.assertEqual(result["legacy_turns_excluded"], 0)
            self.assertEqual(result["ineligible_turns"], 0)

    def test_os_start_cannot_override_realtime_evidence_or_exclusion(self):
        for os_start in (started(), started("fallback", run_id="run-v1")):
            result = self.summary([turn(route="realtime_handled"), os_start, execution()])
            self.assertEqual(result["completed_turns"], 0)
            self.assertEqual(result["incomplete_turns"], 1)
            result = self.summary([turn(route="realtime_handled", task_eligible=False),
                                   os_start, execution()])
            self.assertEqual(result["eligible_mature_turns"], 0)

    def test_unmatched_execution_coverage_is_deduplicated(self):
        orphan = execution("orphan")
        result = metrics.report([started(), execution(), orphan, orphan,
                                 execution("failed", outcome="failed")], 2000000)
        self.assertEqual(result["aggregate"]["completed_turns"], 1)
        self.assertEqual(result["coverage"]["unmatched_execution_events"], 2)
        self.assertEqual(result["coverage"]["unmatched_completed_execution_events"], 1)

    def test_started_smoke_and_future_binding_are_excluded(self):
        result = metrics.report([started("vi-smoke-check"),
                                 started(run_id=""),
                                 {**started(), "observed_at_ms": 2000001}, execution()], 2000000)
        self.assertEqual(result["aggregate"]["eligible_mature_turns"], 1)
        self.assertEqual(result["aggregate"]["incomplete_turns"], 1)
        self.assertEqual(result["coverage"]["synthetic_events_excluded"], 1)
        self.assertEqual(result["coverage"]["future_events_excluded"], 1)

    def test_duplicate_revisions_and_completion_after_ten_seconds(self):
        old = turn(task_eligible=False)
        latest = turn(task_revision=2)
        for rows in ([old, latest, old, execution()], [latest, execution(), old]):
            report = self.summary(rows)
            self.assertEqual(report["eligible_mature_turns"], 1)
            self.assertEqual(report["kpi3_pct"], 100)

    def test_failed_unknown_unfinished_and_muted_remain_denominator(self):
        report = self.summary([turn("failed"), execution("failed", outcome="failed"),
                               turn("unknown"), execution("unknown", outcome="unknown"),
                               turn("unfinished"), turn("muted", failure_reason="muted"),
                               execution("muted")])
        self.assertEqual(report["eligible_mature_turns"], 4)
        self.assertEqual(report["completed_turns"], 1)
        self.assertEqual(report["kpi3_pct"], 25)
        self.assertFalse(report["meets_target"])

    def test_realtime_memory_sync_cannot_complete_turn(self):
        report = self.summary([turn(route="realtime_handled"), execution()])
        self.assertEqual(report["incomplete_turns"], 1)
        report = self.summary([turn(route="realtime_handled"), execution(),
                               execution(interaction_id="v1", evidence="realtime_turn_done")])
        self.assertEqual(report["completed_turns"], 1)

    def test_terminal_failure_is_sticky_even_if_cleanup_end_arrives_later(self):
        report = self.summary([turn(), execution(), execution(outcome="failed")])
        self.assertEqual(report["failed_turns"], 1)
        report = self.summary([turn(), execution(outcome="failed"), execution(execution_at_ms=91000)])
        self.assertEqual(report["failed_turns"], 1)
        report = self.summary([turn(), execution(outcome="unknown"), execution(execution_at_ms=91000)])
        self.assertEqual(report["completed_turns"], 1)

    def test_empty_and_fresh_are_na(self):
        self.assertIsNone(self.summary([])["kpi3_pct"])
        report = metrics.report([turn(task_started_at_ms=1999999)],
                                now_ms=2000000, settle_seconds=1800)["aggregate"]
        self.assertEqual(report["fresh_pending_turns"], 1)
        self.assertIsNone(report["meets_target"])

    def test_smoke_excluded_legacy_coverage_and_unknown_eligibility(self):
        report = metrics.report([turn("vi-smoke-test"), execution("vi-smoke-test"),
                                 turn("legacy", task_schema_version=None),
                                 turn("unknown", task_eligibility_known=False)], 2000000)
        self.assertEqual(report["coverage"]["synthetic_events_excluded"], 1)
        self.assertEqual(report["aggregate"]["legacy_turns_excluded"], 1)
        self.assertEqual(report["aggregate"]["eligible_mature_turns"], 1)
        self.assertEqual(report["aggregate"]["eligibility_unknown_turns"], 1)

    def test_journal_and_aa_normalization(self):
        params = turn()["params"]
        hal = dict(_HOSTNAME="lamp", MESSAGE="[telemetry] voice_metrics_interaction " + json.dumps(params))
        go = dict(_HOSTNAME="lamp", MESSAGE='time=now msg="[telemetry] event" event_name=voice_metrics_task_execution event_id=abc params=' + json.dumps(json.dumps(execution()["params"])))
        self.assertEqual(self.summary([hal, go])["completed_turns"], 1)
        aa = dict(event_name="voice_metrics_interaction", data=dict(user_pseudo_id="lamp",
                  event_params=[dict(key=key, value=value) for key, value in params.items()]))
        self.assertEqual(self.summary([aa, execution()])["completed_turns"], 1)

    def test_device_color_handler_logs_are_readable(self):
        params = json.dumps(execution()["params"])
        message = ('\x1b[32mINFO\x1b[0m [telemetry] event '
                   '\x1b[36mevent_name=\x1b[0mvoice_metrics_task_execution '
                   '\x1b[36mparams=\x1b[0m' + params)
        journal = dict(_HOSTNAME="lamp", MESSAGE=message)
        self.assertEqual(self.summary([turn(), journal])["completed_turns"], 1)
        journal["MESSAGE"] = list(message.encode("utf-8"))
        self.assertEqual(self.summary([turn(), journal])["completed_turns"], 1)

    def test_devices_do_not_share_execution_and_future_evidence_is_ignored(self):
        other = execution()
        other["device_id"] = "other-lamp"
        report = self.summary([turn(), other, execution(execution_at_ms=2000001)])
        self.assertEqual(report["incomplete_turns"], 1)
        self.assertEqual(report["completed_turns"], 0)

    def test_threshold_uses_unrounded_ratio(self):
        rows = [turn(str(i)) for i in range(101)]
        rows.extend(execution(str(i)) for i in range(85))
        report = self.summary(rows)
        self.assertGreater(report["kpi3_pct"], 84)
        self.assertLess(report["kpi3_pct"], 85)
        self.assertFalse(report["meets_target"])

    def test_hal_execution_journal_can_complete_realtime_without_aa(self):
        params = execution(interaction_id="v1", evidence="realtime_turn_done")["params"]
        journal = dict(_HOSTNAME="lamp", MESSAGE="[telemetry] voice_metrics_task_execution " + json.dumps(params))
        report = self.summary([turn(route="realtime_handled"), journal])
        self.assertEqual(report["completed_turns"], 1)

    def test_device_flag_is_only_fallback_for_aa(self):
        aa = dict(event_name="voice_metrics_interaction", data=dict(
            user_pseudo_id="real-device", event_params=turn()["params"]))
        event = metrics.normalize(aa, "fallback-device")
        self.assertEqual(event["device_id"], "real-device")

    def test_malformed_numeric_fields_count_coverage_without_crashing(self):
        rows = [turn(task_revision="bad"), turn(task_started_at_ms="bad"),
                execution(execution_at_ms="bad"), turn(hal_failed_total="bad"),
                {"event_name": "voice_metrics_interaction", "params": []},
                None, {"data": "invalid"}, turn()]
        report = metrics.report(rows, 2000000)
        self.assertEqual(report["coverage"]["malformed_events"], 6)
        self.assertEqual(report["aggregate"]["incomplete_turns"], 1)

    def test_completion_without_observation_time_is_not_a_pass(self):
        report = metrics.report([turn(), execution(execution_at_ms=None)], 2000000)
        self.assertEqual(report["coverage"]["malformed_events"], 1)
        self.assertEqual(report["aggregate"]["completed_turns"], 0)
        self.assertEqual(report["aggregate"]["incomplete_turns"], 1)

    def test_future_aa_revision_does_not_change_historical_cohort(self):
        old = {**turn(hal_failed_total=2), "event_timestamp": 100}
        future = {**turn(task_revision=2, task_eligible=False, hal_failed_total=99),
                  "event_timestamp": 2100}
        for rows in ([old, future], [future, old]):
            result = metrics.report(rows, 2000000)
            self.assertEqual(result["aggregate"]["eligible_mature_turns"], 1)
            self.assertEqual(result["aggregate"]["hal_failed_total"], 2)
            self.assertEqual(result["coverage"]["future_events_excluded"], 1)

    def test_journal_timestamp_filters_future_binding_and_terminal_arrival(self):
        def journal(params, name, stamp):
            return dict(_HOSTNAME="lamp", __REALTIME_TIMESTAMP=str(stamp * 1000),
                        MESSAGE="[telemetry] " + name + " " + json.dumps(params))
        old = journal(turn(run_id="")["params"], "voice_metrics_interaction", 100000)
        future = journal(turn(task_revision=2)["params"], "voice_metrics_interaction", 2100000)
        result = metrics.report([old, future, execution()], 2000000)
        self.assertEqual(result["aggregate"]["incomplete_turns"], 1)
        # A result observed after cutoff cannot be used just because execution
        # itself happened earlier (e.g. a delayed transport/log write).
        late = journal(execution()["params"], "voice_metrics_task_execution", 2100000)
        result = metrics.report([turn(), late], 2000000)
        self.assertEqual(result["aggregate"]["incomplete_turns"], 1)

    def test_normalized_observation_cutoff_is_inclusive_and_preserved(self):
        row = {**turn(), "observed_at_ms": 2000000}
        normalized = metrics.normalize(row)
        self.assertEqual(normalized["observed_at_ms"], 2000000)
        result = metrics.report([normalized], 2000000)
        self.assertEqual(result["aggregate"]["eligible_mature_turns"], 1)
        self.assertNotIn("missing_observation_timestamp_events", result["coverage"])
        row["observed_at_ms"] = "bad"
        self.assertEqual(metrics.report([row], 2000000)["coverage"]["malformed_events"], 1)

    def test_untimestamped_exports_flag_historical_coverage_gap(self):
        result = metrics.report([turn(), execution()], 2000000)
        self.assertEqual(result["aggregate"]["completed_turns"], 1)
        self.assertEqual(result["coverage"]["missing_observation_timestamp_events"], 2)

    def test_jsonl_reader_is_lazy_and_identifies_invalid_line(self):
        stream = io.StringIO(json.dumps(turn()) + "\ninvalid\n")
        with patch.object(metrics.sys, "stdin", stream):
            rows = metrics.read_rows([])
            self.assertEqual(stream.tell(), 0)
            self.assertEqual(next(rows)["event_name"], "voice_metrics_interaction")
            with self.assertRaisesRegex(ValueError, "-:2: invalid JSON"):
                next(rows)
            self.assertFalse(stream.closed)

    def test_loss_counters_are_max_per_device_including_execution(self):
        rows = [turn(hal_failed_total=3, unknown_owner_playbacks=2),
                turn(task_revision=2, hal_failed_total=1),
                execution(hal_failed_total=5, telemetry_dropped_total=7)]
        other = execution(hal_failed_total=4)
        other["device_id"] = "other"
        counters_only = dict(_HOSTNAME="lamp", MESSAGE='[telemetry] voice_metrics_playback {"hal_dropped_total": 8}')
        report = metrics.report(rows + [other, counters_only], 2000000)
        self.assertEqual(report["devices"]["lamp"]["hal_failed_total"], 5)
        self.assertEqual(report["devices"]["lamp"]["unknown_owner_playbacks"], 2)
        self.assertEqual(report["aggregate"]["hal_failed_total"], 9)
        self.assertEqual(report["aggregate"]["telemetry_dropped_total"], 7)
        self.assertEqual(report["aggregate"]["hal_dropped_total"], 8)


class GroupReportTests(unittest.TestCase):
    @staticmethod
    def start(group, identifier, **overrides):
        event = started(identifier, **overrides)
        event["event_name"] = f"{group}_metrics_task_started"
        return event

    def test_harness_handoff_requires_remote_evidence_for_voice_and_chat(self):
        for group in ("voice", "chat"):
            start = self.start(group, "harness")
            handoff = execution("harness", outcome="unknown", evidence="harness_delegated",
                                execution_at_ms=2000)
            local_end = execution("harness", execution_at_ms=90000)
            for evidence, outcome in ((None, "unknown"), ("harness_question_open", "unknown"),
                                      ("harness_turn_done", "completed"),
                                      ("harness_turn_summary", "completed"),
                                      ("harness_turn_error", "failed")):
                with self.subTest(group=group, evidence=evidence):
                    rows = [start, handoff, local_end]
                    if evidence:
                        remote = execution("harness", outcome=outcome, evidence=evidence,
                                           execution_at_ms=10000)
                        rows += [remote, remote]
                    for order in (rows, list(reversed(rows))):
                        result = metrics.report(order, 2000000, group=group)["aggregate"]
                        self.assertEqual(result["eligible_mature_turns"], 1)
                        self.assertEqual(result[outcome + "_turns"], 1)

    def test_harness_grouped_result_preserves_terminal_outcomes(self):
        for group in ("voice", "chat", "sensing"):
            for outcome in ("completed", "failed", "cancelled"):
                with self.subTest(group=group, outcome=outcome):
                    rows = [self.start(group, "harness"),
                            execution("harness", outcome="unknown", evidence="harness_delegated",
                                      execution_at_ms=2000),
                            execution("harness", outcome=outcome, evidence="harness_correlated_summary",
                                      execution_at_ms=10000),
                            execution("harness", evidence="lifecycle_end", execution_at_ms=15000),
                            execution("harness", outcome="unknown", evidence="execution_observation_lost",
                                      execution_at_ms=20000)]
                    rows.append(rows[2])
                    for order in (rows, list(reversed(rows))):
                        result = metrics.report(order, 2000000, group=group)["aggregate"]
                        self.assertEqual(result["eligible_mature_turns"], 1)
                        self.assertEqual(result[outcome + "_turns"], 1)
                        self.assertEqual(result["unknown_turns"], 0)
                        self.assertEqual(result["completion_pct"], 100 if outcome == "completed" else 0)
                        if outcome == "cancelled":
                            self.assertEqual(result["failed_turns"], 0)

    def test_cancelled_is_terminal_and_never_cleanup_success(self):
        rows = [self.start("voice", "cancelled"),
                execution("cancelled", evidence="harness_correlated_summary", outcome="cancelled"),
                execution("cancelled", execution_at_ms=100000)]
        result = metrics.report(rows, 2000000)["aggregate"]
        self.assertEqual(result["cancelled_turns"], 1)
        self.assertEqual(result["completed_turns"], 0)
        self.assertEqual(result["failed_turns"], 0)

    def test_harness_marker_does_not_override_remote_terminal_or_gemini(self):
        handoff = execution("v1", outcome="unknown", evidence="harness_delegated",
                            execution_at_ms=100000)
        remote = execution("v1", evidence="harness_turn_done", execution_at_ms=50000)
        result = metrics.report([self.start("voice", "v1"), handoff, remote], 2000000)["aggregate"]
        self.assertEqual(result["completed_turns"], 1)
        realtime = turn("v1", route="realtime_handled")
        result = metrics.report([realtime, handoff, remote], 2000000)["aggregate"]
        self.assertEqual(result["completed_turns"], 0)
        self.assertEqual(result["incomplete_turns"], 1)
        result = metrics.report([realtime, handoff, remote,
                                 execution("v1", evidence="realtime_turn_done")], 2000000)["aggregate"]
        self.assertEqual(result["completed_turns"], 1)

    def test_mixed_cohorts_keep_independent_denominators_and_results(self):
        rows = [turn("hal"), execution("hal")]
        for group, outcome in (("voice", "completed"), ("chat", "failed"),
                               ("sensing", "unknown")):
            rows += [self.start(group, group), execution(group, outcome=outcome)]
        expected = {"voice": (2, 2, 0, 0), "chat": (1, 0, 1, 0),
                    "sensing": (1, 0, 0, 1)}
        for group, counts in expected.items():
            with self.subTest(group=group):
                result = metrics.report(rows, 2000000, group=group)
                self.assertEqual(result["group"], group)
                aggregate = result["aggregate"]
                self.assertEqual(tuple(aggregate[key] for key in
                                       ("eligible_mature_turns", "completed_turns",
                                        "failed_turns", "unknown_turns")), counts)
                self.assertEqual(aggregate["completion_pct"], aggregate["kpi3_pct"])
        self.assertEqual(metrics.report(rows, 2000000),
                         metrics.report(rows, 2000000, group="voice"))

    def test_no_lifecycle_final_completes_chat_but_not_realtime_voice(self):
        done = execution("slash", evidence="chat_final_no_lifecycle")
        rows = [self.start("chat", "slash"), done, done]
        result = metrics.report(rows, 2000000, group="chat")["aggregate"]
        self.assertEqual(result["eligible_mature_turns"], 1)
        self.assertEqual(result["completed_turns"], 1)
        self.assertEqual(result["incomplete_turns"], 0)
        voice = metrics.report([turn("slash", route="realtime_handled"), done],
                               2000000)["aggregate"]
        self.assertEqual(voice["completed_turns"], 0)
        self.assertEqual(voice["incomplete_turns"], 1)

    def test_each_group_hundred_starts_eighty_five_completed(self):
        rows = []
        for group in ("voice", "chat", "sensing"):
            rows += [self.start(group, f"{group}-{i}") for i in range(100)]
            rows += [execution(f"{group}-{i}") for i in range(85)]
        for group in ("voice", "chat", "sensing"):
            with self.subTest(group=group):
                result = metrics.report(rows, 2000000, group=group)["aggregate"]
                self.assertEqual(result["eligible_mature_turns"], 100)
                self.assertEqual(result["completed_turns"], 85)
                self.assertEqual(result["incomplete_turns"], 15)
                self.assertEqual(result["completion_pct"], 85)
                self.assertTrue(result["meets_target"])

    def test_queue_binding_duplicates_and_terminal_error_for_each_group(self):
        for group in ("voice", "chat", "sensing"):
            with self.subTest(group=group):
                queued = self.start(group, "queued", run_id="")
                bound = self.start(group, "queued", run_id="actual-run")
                done = execution("other", run_id="actual-run")
                result = metrics.report([queued, queued], 2000000, group=group)["aggregate"]
                self.assertEqual(result["eligible_mature_turns"], 1)
                self.assertEqual(result["incomplete_turns"], 1)
                rows = [queued, bound, bound, done, done]
                result = metrics.report(rows, 2000000, group=group)["aggregate"]
                self.assertEqual(result["eligible_mature_turns"], 1)
                self.assertEqual(result["completed_turns"], 1)
                failure = execution("other", run_id="actual-run", outcome="failed",
                                    evidence="lifecycle_error", execution_at_ms=10000)
                result = metrics.report(rows + [failure], 2000000, group=group)["aggregate"]
                self.assertEqual(result["failed_turns"], 1)
                self.assertEqual(result["completed_turns"], 0)

    def test_synthetic_prefixes_excluded_unless_requested(self):
        for group, prefix in (("voice", "vi-smoke-"), ("chat", "chat-smoke-"),
                              ("sensing", "sensing-smoke-")):
            with self.subTest(group=group):
                identifier = prefix + "test"
                rows = [self.start(group, identifier), execution(identifier)]
                result = metrics.report(rows, 2000000, group=group)["aggregate"]
                self.assertEqual(result["eligible_mature_turns"], 0)
                self.assertIsNone(result["completion_pct"])
                result = metrics.report(rows, 2000000, include_synthetic=True,
                                        group=group)["aggregate"]
                self.assertEqual(result["completed_turns"], 1)

    def test_cli_group_selects_cohort_and_rejects_invalid_group(self):
        row = self.start("chat", "web")
        output = io.StringIO()
        with patch.object(metrics.sys, "argv", ["report", "--group", "chat", "--now-ms", "2000000"]), \
                patch.object(metrics.sys, "stdin", io.StringIO(json.dumps(row))), \
                patch.object(metrics.sys, "stdout", output):
            metrics.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result["group"], "chat")
        self.assertEqual(result["aggregate"]["incomplete_turns"], 1)
        with self.assertRaisesRegex(ValueError, "unsupported task group"):
            metrics.report([], 2000000, group="invalid")


if __name__ == "__main__":
    unittest.main()
