//! Readable and machine-readable reports. Every stratum is summarized on its
//! own; denominators are explicit and withheld/unscored items are listed.
use crate::{
    evaluate::{
        AnswerOutcome, AttemptScore, CheckStatus, FindingKind, Metric, MetricKind, Outcome,
        Severity,
    },
    plan::{Expectation, Targets},
    record::Stratum,
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
    pub withheld_steps: usize,
    pub latencies: Vec<LatencySummary>,
    pub acoustic_scored: usize,
    pub acoustic_unscored: BTreeMap<String, usize>,
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
        for step in &score.steps {
            if matches!(step.status, CheckStatus::Withheld { .. }) {
                s.withheld_steps += 1;
                continue;
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
                FindingKind::LateAnswer => s.late_answers += 1,
                FindingKind::RuntimeFailure => s.runtime_failures += 1,
                FindingKind::UnannouncedFailure => s.unannounced_failures += 1,
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
            (
                "Unexpected responses (all causes)",
                s.unexpected_responses.to_string(),
            ),
            ("Expected-silence steps kept silent", s.silence_kept.text()),
            ("Turn splits", s.turn_splits.to_string()),
            ("Late answers (right-censored)", s.late_answers.to_string()),
            ("Runtime failures", s.runtime_failures.to_string()),
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
        if stratum.is_physical() {
            let unscored: Vec<String> = s
                .acoustic_unscored
                .iter()
                .map(|(k, v)| format!("{v} {k}"))
                .collect();
            let _ = writeln!(
                out,
                "Acoustic measurements scored: {}; unscored: {}.",
                s.acoustic_scored,
                if unscored.is_empty() {
                    "none".into()
                } else {
                    unscored.join(", ")
                }
            );
        }
    }
    let _ = writeln!(out, "\n## Every attempt\n");
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
            "| `{}` | {} | {:?} | {}{} | {} |",
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
