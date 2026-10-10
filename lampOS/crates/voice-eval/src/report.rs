//! Readable and machine-readable reports. Every stratum is summarized on its
//! own; denominators are explicit and withheld/unscored items are listed.
use crate::{
    evaluate::{
        AnswerOutcome, AttemptScore, CheckStatus, FindingKind, Metric, MetricKind, Outcome,
        Severity,
    },
    events::EventKind,
    plan::{Expectation, Targets},
    record::{AttemptRecord, Stratum},
    stats::{Distribution, distribution},
};
use serde::Serialize;
use std::{collections::BTreeMap, fmt::Write as _};

#[derive(Clone, Debug, Default, Serialize)]
pub struct Ratio {
    pub count: usize,
    pub of: usize,
}
impl Ratio {
    fn text(&self) -> String {
        if self.of == 0 {
            "n/a (0 opportunities)".into()
        } else {
            format!(
                "{}/{} ({:.0}%)",
                self.count,
                self.of,
                100.0 * self.count as f64 / self.of as f64
            )
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct LatencySummary {
    pub metric: Metric,
    pub kind: MetricKind,
    pub boundary: &'static str,
    pub distribution: Distribution,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct StratumSummary {
    pub attempts: usize,
    pub outcomes: BTreeMap<String, usize>,
    /// Invalid (aborted) attempts appear in `outcomes` but in no rate or latency.
    pub invalid_excluded: usize,
    pub complete_answers: Ratio,
    pub complete_answers_without_gaps: Ratio,
    pub answers_yielded_as_planned: usize,
    pub answers_unsupported: usize,
    pub false_interruptions: usize,
    pub false_interruption_opportunities: Ratio,
    pub playback_minutes: f64,
    pub missed_interruptions: Ratio,
    pub lost_opening_words: Ratio,
    pub lost_opening_unscored: usize,
    pub possible_lost_opening_hypotheses: usize,
    pub unexpected_responses: usize,
    pub silence_kept: Ratio,
    pub turn_splits: usize,
    pub late_answers: usize,
    pub runtime_failures: usize,
    pub unannounced_failures: usize,
    /// Stale output/cue, overlapping replies, unterminated turns, ring mismatch.
    pub state_inconsistencies: usize,
    pub withheld_steps: usize,
    pub ring_checked: usize,
    pub latencies: Vec<LatencySummary>,
    pub acoustic_scored: usize,
    pub acoustic_unscored: BTreeMap<String, usize>,
    pub missing_answers: usize,
    pub truncated_answers: usize,
    pub duplicate_answers: usize,
    pub playback_gap_answers: usize,
    /// Heard answers (audio started) whose speech-end -> first word could be measured.
    pub speech_end_opportunities: usize,
    pub speech_end_measured: usize,
    /// Planned yields whose interrupting speech -> audible stop could be measured.
    pub interruption_opportunities: usize,
    pub interruption_measured: usize,
}

pub fn summarize(scores: &[&AttemptScore]) -> StratumSummary {
    let mut s = StratumSummary {
        attempts: scores.len(),
        ..StratumSummary::default()
    };
    let mut values: BTreeMap<(Metric, MetricKind), Vec<f64>> = BTreeMap::new();
    for score in scores {
        *s.outcomes
            .entry(format!("{:?}", score.outcome).to_lowercase())
            .or_default() += 1;
        if score.outcome == Outcome::Invalid {
            s.invalid_excluded += 1;
            continue;
        }
        s.playback_minutes += score.playback_ms / 60_000.0;
        s.ring_checked += usize::from(score.ring_checked);
        for step in &score.steps {
            if matches!(step.status, CheckStatus::Withheld { .. }) {
                s.withheld_steps += 1;
                continue;
            }
            if matches!(
                step.answer,
                Some(
                    AnswerOutcome::Complete
                        | AnswerOutcome::CompleteWithGaps
                        | AnswerOutcome::Truncated { .. }
                        | AnswerOutcome::Late
                )
            ) {
                s.speech_end_opportunities += 1;
            }
            match &step.answer {
                Some(AnswerOutcome::Missing { .. } | AnswerOutcome::NotAdmitted) => {
                    s.missing_answers += 1
                }
                Some(AnswerOutcome::Truncated { .. }) => s.truncated_answers += 1,
                Some(AnswerOutcome::CompleteWithGaps) => s.playback_gap_answers += 1,
                _ => {}
            }
            if step.interruption == Some(true) {
                s.interruption_opportunities += 1;
            }
            match &step.answer {
                Some(AnswerOutcome::Complete) => {
                    s.complete_answers.count += 1;
                    s.complete_answers.of += 1;
                    s.complete_answers_without_gaps.count += 1;
                    s.complete_answers_without_gaps.of += 1;
                }
                Some(AnswerOutcome::CompleteWithGaps) => {
                    s.complete_answers.count += 1;
                    s.complete_answers.of += 1;
                    s.complete_answers_without_gaps.of += 1;
                }
                Some(AnswerOutcome::YieldedAsPlanned) => s.answers_yielded_as_planned += 1,
                Some(AnswerOutcome::Unsupported { .. }) => s.answers_unsupported += 1,
                Some(_) => {
                    s.complete_answers.of += 1;
                    s.complete_answers_without_gaps.of += 1;
                }
                None => {}
            }
            if let Some(yielded) = step.interruption {
                s.missed_interruptions.of += 1;
                s.missed_interruptions.count += usize::from(!yielded);
            }
            let unscored = matches!(step.status, CheckStatus::Unscored { .. });
            match step.expect {
                Expectation::NoInterrupt | Expectation::Observe if !unscored => {
                    s.false_interruption_opportunities.of += 1
                }
                Expectation::Silence if !unscored => {
                    s.silence_kept.of += 1;
                    if step.status == CheckStatus::Pass {
                        s.silence_kept.count += 1;
                    }
                }
                _ => {}
            }
            if !step.turns.is_empty()
                && matches!(
                    step.expect,
                    Expectation::Answer
                        | Expectation::InterruptAndAnswer
                        | Expectation::HonestFailure
                )
            {
                match step.lost_opening_ms {
                    Some(_) => s.lost_opening_words.of += 1,
                    None => s.lost_opening_unscored += 1,
                }
            }
        }
        for finding in &score.findings {
            // Hypotheses (for example under incomplete declared attribution)
            // are listed per attempt but not counted as failures.
            if finding.severity == Severity::Hypothesis
                && finding.kind != FindingKind::PossibleLostOpeningWords
            {
                continue;
            }
            match finding.kind {
                FindingKind::FalseInterruption => s.false_interruptions += 1,
                FindingKind::LostOpeningWords => s.lost_opening_words.count += 1,
                FindingKind::PossibleLostOpeningWords => s.possible_lost_opening_hypotheses += 1,
                FindingKind::UnexpectedResponse => s.unexpected_responses += 1,
                FindingKind::TurnSplit => s.turn_splits += 1,
                FindingKind::DuplicateAnswer => s.duplicate_answers += 1,
                FindingKind::LateAnswer => s.late_answers += 1,
                FindingKind::RuntimeFailure => s.runtime_failures += 1,
                FindingKind::UnannouncedFailure => s.unannounced_failures += 1,
                FindingKind::StaleOutput
                | FindingKind::OverlappingOutput
                | FindingKind::UnterminatedTurn
                | FindingKind::RingMismatch => s.state_inconsistencies += 1,
                _ => {}
            }
        }
        s.false_interruption_opportunities.count += score
            .steps
            .iter()
            .filter(|st| {
                matches!(st.expect, Expectation::NoInterrupt | Expectation::Observe)
                    && st.status == CheckStatus::Fail
            })
            .count();
        for latency in &score.latencies {
            match latency.metric {
                Metric::AcousticSpeechEndToAnswer => s.speech_end_measured += 1,
                Metric::AcousticInterruptionToSilence => s.interruption_measured += 1,
                _ => {}
            }
            values
                .entry((latency.metric, latency.kind))
                .or_default()
                .push(latency.value_ms);
        }
        for acoustic in &score.acoustic {
            match &acoustic.unscored_reason {
                None => s.acoustic_scored += 1,
                Some(reason) => *s.acoustic_unscored.entry(reason.clone()).or_default() += 1,
            }
        }
    }
    s.latencies = values
        .into_iter()
        .filter_map(|((metric, kind), values)| {
            Some(LatencySummary {
                metric,
                kind,
                boundary: metric.boundary(),
                distribution: distribution(&values)?,
            })
        })
        .collect();
    s
}

#[derive(Debug, Serialize)]
pub struct Report<'a> {
    pub run_id: String,
    pub plan_id: String,
    pub plan_sha256: String,
    pub strata: BTreeMap<Stratum, StratumSummary>,
    pub attempts: &'a [AttemptScore],
    pub limitations: Vec<String>,
    pub incomplete_ledger: Vec<String>,
}

pub fn build<'a>(
    run_id: &str,
    plan_id: &str,
    plan_sha256: &str,
    scores: &'a [AttemptScore],
    incomplete_ledger: Vec<String>,
) -> Report<'a> {
    let mut grouped: BTreeMap<Stratum, Vec<&AttemptScore>> = BTreeMap::new();
    for score in scores {
        grouped.entry(score.stratum).or_default().push(score);
    }
    let strata = grouped
        .into_iter()
        .map(|(k, v)| (k, summarize(&v)))
        .collect();
    Report {
        run_id: run_id.into(),
        plan_id: plan_id.into(),
        plan_sha256: plan_sha256.into(),
        strata,
        attempts: scores,
        limitations: limitations(scores),
        incomplete_ledger,
    }
}

fn limitations(scores: &[AttemptScore]) -> Vec<String> {
    let mut notes = vec![
        "Strata are never pooled. Fake-runtime rows describe the turn policy against declared or digital stimuli; they contain no room acoustics, loudspeaker, microphone, AEC or physical timing.".to_owned(),
        "Software timestamps (runtime host clock, ALSA acceptance/retirement) are not acoustic boundaries. Acoustic latency appears only from reviewed room-audio annotations; anything else is unscored.".to_owned(),
        "Simulated latencies restate the fake profile's configured provider and speaker delays; they are not predictions.".to_owned(),
    ];
    if scores.iter().any(|s| s.voices > 1) {
        notes.push("Several synthetic voices played from one loudspeaker cannot establish spatial speaker discrimination; multi-voice scenarios test restraint under single-source playback only.".into());
    }
    if !scores.iter().any(|s| s.stratum.is_physical()) {
        notes.push("No physical attempts are in this report, so there is no positive physical overlap/interruption cohort here.".into());
    }
    notes
}

fn outcome_label(outcome: Outcome) -> &'static str {
    match outcome {
        Outcome::Passed => "passed",
        Outcome::Failed => "FAILED",
        Outcome::Incomplete => "incomplete",
        Outcome::Withheld => "withheld",
        Outcome::Invalid => "INVALID",
    }
}

fn kind_label(kind: MetricKind) -> &'static str {
    match kind {
        MetricKind::Simulated => "simulated",
        MetricKind::Software => "software",
        MetricKind::Runner => "runner",
        MetricKind::Acoustic => "ACOUSTIC",
    }
}

pub fn markdown(report: &Report, targets: &Targets) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "# Voice acceptance report `{}`\n", report.run_id);
    let _ = writeln!(
        out,
        "Plan `{}` (sha256 `{}`).\n",
        report.plan_id, report.plan_sha256
    );
    let _ = writeln!(out, "## Limits of this evidence\n");
    for note in &report.limitations {
        let _ = writeln!(out, "- {note}");
    }
    if !report.incomplete_ledger.is_empty() {
        let _ = writeln!(
            out,
            "- Ledger problems: {}",
            report.incomplete_ledger.join("; ")
        );
    }
    for (stratum, s) in &report.strata {
        let _ = writeln!(out, "\n## {}\n", stratum.label());
        let outcomes: Vec<String> = s.outcomes.iter().map(|(k, v)| format!("{k} {v}")).collect();
        let _ = writeln!(out, "{} attempts: {}.\n", s.attempts, outcomes.join(", "));
        if s.invalid_excluded > 0 {
            let _ = writeln!(
                out,
                "{} invalid attempts are excluded from every rate and latency below.\n",
                s.invalid_excluded
            );
        }
        let _ = writeln!(out, "| Measure | Result |\n|---|---|");
        let rows = [
            (
                "Complete answers (within the answer deadline)",
                s.complete_answers.text(),
            ),
            (
                "Complete answers without playback gaps",
                s.complete_answers_without_gaps.text(),
            ),
            (
                "Answers yielded to a planned interruption (not in the denominator)",
                s.answers_yielded_as_planned.to_string(),
            ),
            (
                "Answers unsupported by the provider (not in the denominator)",
                s.answers_unsupported.to_string(),
            ),
            (
                "False interruptions (all causes)",
                format!(
                    "{} over {:.2} min of software playback",
                    s.false_interruptions, s.playback_minutes
                ),
            ),
            (
                "Acknowledgment/echo windows that interrupted Lamp",
                s.false_interruption_opportunities.text(),
            ),
            ("Missed interruptions", s.missed_interruptions.text()),
            ("Lost opening words (measured)", s.lost_opening_words.text()),
            (
                "Admissions with opening retention unscored",
                s.lost_opening_unscored.to_string(),
            ),
            (
                "Transcript hypotheses of lost opening words (not proof)",
                s.possible_lost_opening_hypotheses.to_string(),
            ),
            ("Missing answers (no audio)", s.missing_answers.to_string()),
            (
                "Truncated answers (started, not finished)",
                s.truncated_answers.to_string(),
            ),
            (
                "Duplicate answers (one request answered aloud twice)",
                s.duplicate_answers.to_string(),
            ),
            (
                "Answers with playback gaps",
                s.playback_gap_answers.to_string(),
            ),
            (
                "Unwanted responses (all causes)",
                s.unexpected_responses.to_string(),
            ),
            ("Expected-silence steps kept silent", s.silence_kept.text()),
            ("Turn splits", s.turn_splits.to_string()),
            ("Late answers (right-censored)", s.late_answers.to_string()),
            ("Runtime failures", s.runtime_failures.to_string()),
            (
                "Voice/ring state inconsistencies through cancellation",
                format!(
                    "{} (ring requests checked in {}/{} attempts)",
                    s.state_inconsistencies,
                    s.ring_checked,
                    s.attempts - s.invalid_excluded
                ),
            ),
            (
                "Failures without a spoken notice",
                s.unannounced_failures.to_string(),
            ),
            (
                "Steps withheld (trigger missed, precondition lost, unavailable)",
                s.withheld_steps.to_string(),
            ),
        ];
        for (label, value) in rows {
            let _ = writeln!(out, "| {label} | {value} |");
        }
        let _ = writeln!(out, "\n### Latency distributions (nearest-rank)\n");
        if s.latencies.is_empty() {
            let _ = writeln!(out, "No latency samples.");
        } else {
            let _ = writeln!(
                out,
                "| Metric | Kind | Boundary | n | p50 | p95 | p99 | max |\n|---|---|---|---|---|---|---|---|"
            );
            for l in &s.latencies {
                let d = &l.distribution;
                let _ = writeln!(
                    out,
                    "| {:?} | {} | {} | {} | {:.0} | {:.0} | {:.0} | {:.0} |",
                    l.metric,
                    kind_label(l.kind),
                    l.boundary,
                    d.n,
                    d.p50,
                    d.p95,
                    d.p99,
                    d.max
                );
            }
        }
        let _ = writeln!(
            out,
            "\nRelease targets apply only to ACOUSTIC rows: answer p50 <= {} ms and p95 <= {} ms; interruption to silence p95 <= {} ms.",
            targets.answer_p50_ms, targets.answer_p95_ms, targets.yield_p95_ms
        );
        let reasons = if stratum.is_physical() {
            let unscored: Vec<String> = s
                .acoustic_unscored
                .iter()
                .map(|(k, v)| format!("{v} {k}"))
                .collect();
            if unscored.is_empty() {
                "none recorded".into()
            } else {
                unscored.join(", ")
            }
        } else {
            "simulation: no room audio exists".to_owned()
        };
        let _ = writeln!(out, "\n### Acoustic boundaries (room audio only)\n");
        let _ = writeln!(
            out,
            "| Boundary | Measured | Unmeasured | Why unmeasured |\n|---|---|---|---|"
        );
        for (label, measured, of) in [
            (
                "Final user speech -> first substantive audible answer word",
                s.speech_end_measured,
                s.speech_end_opportunities,
            ),
            (
                "Interrupting speech onset -> Lamp audibly silent",
                s.interruption_measured,
                s.interruption_opportunities,
            ),
        ] {
            let _ = writeln!(
                out,
                "| {label} | {measured} | {} | {reasons} |",
                of.saturating_sub(measured)
            );
        }
    }
    let _ = writeln!(out, "\n## Every attempt\n");
    let _ = writeln!(
        out,
        "Each attempt links to its evidence page: stimulus identity, playback settings, triggers, timeline and findings.\n"
    );
    let _ = writeln!(
        out,
        "| Attempt | Scenario | Stratum | Outcome | Findings |\n|---|---|---|---|---|"
    );
    for score in report.attempts {
        let mut counts: BTreeMap<FindingKind, usize> = BTreeMap::new();
        for finding in score
            .findings
            .iter()
            .filter(|f| f.severity != Severity::Withheld)
        {
            *counts.entry(finding.kind).or_default() += 1;
        }
        let findings: Vec<String> = counts.iter().map(|(k, v)| format!("{k:?} x{v}")).collect();
        let reason = score
            .reason
            .as_deref()
            .map(|r| format!(" ({r})"))
            .unwrap_or_default();
        let _ = writeln!(
            out,
            "| [`{0}`](attempts/{0}.md) | {1} | {2:?} | {3}{4} | {5} |",
            score.attempt_id,
            score.scenario,
            score.stratum,
            outcome_label(score.outcome),
            reason,
            if findings.is_empty() {
                "-".into()
            } else {
                findings.join(", ")
            }
        );
    }
    let failures: Vec<&AttemptScore> = report
        .attempts
        .iter()
        .filter(|s| matches!(s.outcome, Outcome::Failed | Outcome::Invalid))
        .collect();
    if !failures.is_empty() {
        let _ = writeln!(out, "\n## Failure details and reproduction\n");
        for score in failures {
            let _ = writeln!(out, "### `{}` ({})\n", score.attempt_id, score.scenario);
            for finding in &score.findings {
                let _ = writeln!(
                    out,
                    "- {:?} [{:?}, {:?}]{}{}: {}",
                    finding.kind,
                    finding.severity,
                    finding.evidence,
                    finding
                        .step
                        .as_deref()
                        .map(|s| format!(" step `{s}`"))
                        .unwrap_or_default(),
                    finding
                        .turn
                        .map(|t| format!(" turn {t}"))
                        .unwrap_or_default(),
                    finding.detail
                );
            }
            let _ = writeln!(out, "\nReproduce: `{}`\n", score.reproduce);
        }
    }
    out
}

/// One evidence page per attempt: what was played (identity, settings, timing),
/// what triggered it, what the runtime did, and how it was scored.
pub fn attempt_markdown(record: &AttemptRecord, score: &AttemptScore) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "# Attempt `{}`\n", record.attempt_id);
    let _ = writeln!(
        out,
        "Scenario `{}` ({:?}, {:?}), provider {:?}, repetition {}, order {}.\n\nStratum: {}.\n\nOutcome: **{}**{}.\n\nReproduce: `{}`\n",
        record.scenario,
        record.cohort,
        record.capability,
        record.provider,
        record.repetition,
        record.order,
        record.stratum.label(),
        outcome_label(score.outcome),
        score
            .reason
            .as_deref()
            .map(|r| format!(" ({r})"))
            .unwrap_or_default(),
        record.reproduce
    );
    if let Some(profile) = &record.profile {
        let _ = writeln!(
            out,
            "Fake profile `{profile}`, attempt seed `{}`.\n",
            record.seed.unwrap_or(0)
        );
    }
    let origin = record
        .steps
        .iter()
        .find_map(|s| s.trigger.as_ref().map(|t| t.runner_us))
        .unwrap_or(0);
    let rel = |us: Option<u64>| {
        us.map_or("-".to_owned(), |us| {
            format!("{:+.1}", (us as f64 - origin as f64) / 1000.0)
        })
    };
    let _ = writeln!(out, "## Stimuli, triggers and playback\n");
    let _ = writeln!(
        out,
        "Times are milliseconds on the runner clock ({:?}) from the first trigger event.\n",
        record.step_domain
    );
    let _ = writeln!(
        out,
        "| Step | Expect | Status | Trigger | Planned | Started | Late ms | Scene | Audio identity |\n|---|---|---|---|---|---|---|---|---|"
    );
    for step in &record.steps {
        let trigger = step.trigger.as_ref().map_or("-".to_owned(), |t| {
            format!(
                "{:?} turn {} ({})",
                t.event,
                t.turn.map_or("-".into(), |t| t.to_string()),
                t.runner_time_method
            )
        });
        let late = step
            .started_us
            .zip(step.target_us)
            .map_or("-".to_owned(), |(s, t)| {
                format!("{:.1}", (s as f64 - t as f64) / 1000.0)
            });
        let identity = match (
            step.receipt.get("wav_sha256"),
            step.receipt.get("asset_key"),
        ) {
            (Some(sha), _) if sha.is_string() => {
                format!("wav sha256 `{}`", sha.as_str().unwrap_or_default())
            }
            _ if step.scene.is_none() => "observation only".into(),
            _ if record.stratum == Stratum::FakeTiming => "declared timing; no audio played".into(),
            _ => "not recorded".into(),
        };
        let _ = writeln!(
            out,
            "| {} | {:?} | {:?}{} | {} | {} | {} | {} | {} | {} |",
            step.step,
            step.expect,
            step.status,
            step.detail
                .as_deref()
                .map(|d| format!(": {d}"))
                .unwrap_or_default(),
            trigger,
            rel(step.target_us),
            rel(step.started_us),
            late,
            step.scene.as_deref().unwrap_or("-"),
            identity
        );
    }
    for step in &record.steps {
        let Some(timing) = &step.timing else { continue };
        let _ = writeln!(
            out,
            "\nStep `{}` scene `{}` ({} ms, {:?}, {} voice(s){}):",
            step.step,
            timing.scene,
            timing.duration_ms,
            timing.source,
            timing.voices,
            if timing.noise {
                ", synthetic noise"
            } else {
                ""
            }
        );
        for clip in &timing.clips {
            let _ = writeln!(
                out,
                "- `{}` {} at {:+.1} dB, speech {}-{} ms: \"{}\"",
                clip.utterance, clip.voice, clip.gain_db, clip.start_ms, clip.end_ms, clip.text
            );
        }
    }
    if let Some(playbacks) = record.evidence.get("playbacks").and_then(|p| p.as_array()) {
        let _ = writeln!(out, "\n### Playback receipts\n");
        for playback in playbacks {
            let report = &playback["report"];
            let _ = writeln!(
                out,
                "- `{}`: status {}, valid {}, output {}, prepared {}, start request {} ns (host clock)",
                playback["step"].as_str().unwrap_or("?"),
                report["status"],
                report["valid"],
                report["selected_output"]["name"],
                report["prepared"],
                report["start_requested_host_monotonic_ns"]
            );
        }
    }
    let _ = writeln!(out, "\n## Runtime timeline\n");
    let zero = record
        .events
        .iter()
        .find(|e| matches!(e.kind, EventKind::ListeningReady))
        .and_then(|e| e.at_us);
    match zero {
        None => {
            let _ = writeln!(out, "No readiness event in the authoritative trace.");
        }
        Some(zero) => {
            let _ = writeln!(
                out,
                "Milliseconds from listening readiness on the runtime clock.\n\n| t ms | Event | Turn | Detail |\n|---|---|---|---|"
            );
            for event in record.events.iter().take(400) {
                let detail = match &event.kind {
                    EventKind::TurnCancelled { reason, .. } => reason.clone().unwrap_or_default(),
                    EventKind::TurnCompleted { outcome, .. } => outcome.clone(),
                    EventKind::RunEnd { status, error } => format!(
                        "{} {}",
                        status.clone().unwrap_or_default(),
                        error.clone().unwrap_or_default()
                    ),
                    EventKind::RingRequested { phase } => phase.clone(),
                    EventKind::InputTranscript { text, .. }
                    | EventKind::OutputTranscript { text, .. } => format!("\"{text}\""),
                    EventKind::RuntimeFault { reason } => reason.clone(),
                    EventKind::PlaybackGap { phase } => phase.clone(),
                    _ => String::new(),
                };
                let _ = writeln!(
                    out,
                    "| {} | {} | {} | {} |",
                    event.at_us.map_or("-".to_owned(), |at| format!(
                        "{:.1}",
                        (at as f64 - zero as f64) / 1000.0
                    )),
                    event.name(),
                    event.turn.map_or("-".into(), |t| t.to_string()),
                    detail
                );
            }
            if record.events.len() > 400 {
                let _ = writeln!(
                    out,
                    "\n{} further events are in the ledger record.",
                    record.events.len() - 400
                );
            }
        }
    }
    let _ = writeln!(
        out,
        "\n## Scoring\n\n| Step | Check | Turns | Answer | Lost opening ms | Yielded |\n|---|---|---|---|---|---|"
    );
    for step in &score.steps {
        let _ = writeln!(
            out,
            "| {} | {:?} | {:?} | {:?} | {} | {} |",
            step.step,
            step.status,
            step.turns,
            step.answer,
            step.lost_opening_ms
                .map_or("-".into(), |v| format!("{v:.0}")),
            step.interruption.map_or("-".into(), |v| v.to_string())
        );
    }
    let _ = writeln!(out, "\n### Findings\n");
    if score.findings.is_empty() {
        let _ = writeln!(out, "None.");
    }
    for f in &score.findings {
        let _ = writeln!(
            out,
            "- {:?} [{:?}, {:?}]{}: {}",
            f.kind,
            f.severity,
            f.evidence,
            f.step
                .as_deref()
                .map(|s| format!(" step `{s}`"))
                .unwrap_or_default(),
            f.detail
        );
    }
    let _ = writeln!(out, "\n### Latency samples\n");
    if score.latencies.is_empty() {
        let _ = writeln!(out, "None.");
    }
    for l in &score.latencies {
        let _ = writeln!(
            out,
            "- {:?} ({}), step `{}`: {:.1} ms",
            l.metric,
            kind_label(l.kind),
            l.step,
            l.value_ms
        );
    }
    let _ = writeln!(out, "\n### Acoustic boundaries\n");
    if score.acoustic.is_empty() {
        let _ = writeln!(
            out,
            "Unmeasured: {}.",
            if record.stratum.is_physical() {
                "no stimulus step to annotate"
            } else {
                "simulation, no room audio"
            }
        );
    }
    for a in &score.acoustic {
        match (a.metric, a.value_ms) {
            (Some(metric), Some(value)) => {
                let _ = writeln!(
                    out,
                    "- `{}` {:?}: {:.0} ms (+/- {:.0} ms)",
                    a.step,
                    metric,
                    value,
                    a.uncertainty_ms.unwrap_or(0.0)
                );
            }
            _ => {
                let _ = writeln!(
                    out,
                    "- `{}` unmeasured: {}",
                    a.step,
                    a.unscored_reason.as_deref().unwrap_or("unknown")
                );
            }
        }
    }
    let _ = writeln!(
        out,
        "\n## Evidence record\n\n```json\n{}\n```",
        serde_json::to_string_pretty(&record.evidence).unwrap_or_default()
    );
    out
}
