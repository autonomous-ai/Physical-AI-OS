//! Score retained attempts. Software outcomes come from runtime events; room
//! audio is scored only from reviewed annotations. Missing evidence is
//! `unscored`, never a pass, and strata are scored separately.
use crate::{
    annotations::{AcousticScore, Annotations},
    events::{EventKind, RuntimeEvent},
    plan::{Capability, Cohort, Expectation, ProviderKind},
    record::{
        AttemptRecord, AttemptStatus, Attribution, StepRecord, StepStatus, StimulusSource, Stratum,
    },
};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

/// Admissions within this distance of a cancellation caused it (the
/// coordinator stamps admission just before it cancels the old reply).
const CAUSE_TOLERANCE_US: u64 = 20_000;
/// Start-request lateness beyond this changes the tested overlap phase.
const LATE_STIMULUS_US: u64 = 250_000;
/// Lost speech shorter than two 10 ms blocks is grid rounding.
const LOST_OPENING_MIN_MS: f64 = 20.0;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FindingKind {
    FalseInterruption,
    MissedInterruption,
    UnexpectedResponse,
    MissedInput,
    TurnSplit,
    IncompleteAnswer,
    TruncatedAnswer,
    MissingAnswer,
    DuplicateAnswer,
    LateAnswer,
    LostOpeningWords,
    PossibleLostOpeningWords,
    PlaybackGaps,
    RuntimeFailure,
    UnannouncedFailure,
    FabricatedCompletion,
    TriggerMissed,
    PreconditionLost,
    SessionEnded,
    NotReached,
    TriggerStale,
    NoRecovery,
    StimulusNotDelivered,
    LateStimulus,
    IrrelevantAnswer,
    StaleOutput,
    OverlappingOutput,
    UnterminatedTurn,
    RingMismatch,
    OverlapNotAchieved,
    EvidenceMissing,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Severity {
    /// The expected behavior did not happen.
    Failure,
    /// Delivered, but with a quality defect (for example playback gaps).
    Defect,
    /// Not tested as planned; neither pass nor fail.
    Withheld,
    /// Suggestive evidence (for example ASR text) that is not proof.
    Hypothesis,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Evidence {
    FakeSimulation,
    SoftwareTrace,
    DeclaredAttribution,
    ProviderTranscript,
    RoomAnnotation,
    Runner,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Finding {
    pub kind: FindingKind,
    pub severity: Severity,
    pub step: Option<String>,
    pub turn: Option<u64>,
    pub evidence: Evidence,
    pub detail: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(tag = "check", rename_all = "snake_case")]
pub enum CheckStatus {
    Pass,
    Fail,
    Unscored { reason: String },
    Withheld { reason: String },
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum AnswerOutcome {
    Complete,
    CompleteWithGaps,
    YieldedAsPlanned,
    Late,
    /// Reply audio started but the answer did not finish.
    Truncated {
        cause: String,
    },
    /// No reply audio was accepted for an admitted request.
    Missing {
        cause: String,
    },
    NotAdmitted,
    Unsupported {
        reason: String,
    },
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct StepScore {
    pub step: String,
    pub expect: Expectation,
    pub status: CheckStatus,
    pub turns: Vec<u64>,
    /// Outcome of the answer this step requested, when it requested one.
    pub answer: Option<AnswerOutcome>,
    /// For interruption steps: whether the playing answer yielded to this
    /// stimulus (`None` when not scorable, e.g. overlap not achieved).
    pub interruption: Option<bool>,
    pub lost_opening_ms: Option<f64>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Metric {
    EndpointToFirstWrite,
    ProviderFirstAudioToFirstWrite,
    StimulusEndToFirstWrite,
    StimulusOnsetToAdmission,
    AdmissionToCancel,
    CancelToTailRetired,
    TriggerLateness,
    AcousticSpeechEndToAnswer,
    AcousticInterruptionToSilence,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MetricKind {
    /// Produced by the fake runtime's configured model; not a measurement.
    Simulated,
    /// Runtime host timestamps; no acoustic boundary.
    Software,
    /// Runner host timestamps around stimulus playback.
    Runner,
    /// Reviewed continuous room audio.
    Acoustic,
}

impl Metric {
    pub fn boundary(self) -> &'static str {
        match self {
            Self::EndpointToFirstWrite => {
                "local endpoint (after the 600 ms silence wait) -> first ALSA-accepted reply write"
            }
            Self::ProviderFirstAudioToFirstWrite => {
                "provider first-audio event -> first ALSA-accepted reply write"
            }
            Self::StimulusEndToFirstWrite => {
                "declared/digital stimulus speech end -> first reply write (same simulated clock)"
            }
            Self::StimulusOnsetToAdmission => {
                "declared/digital stimulus speech onset -> turn admission (same simulated clock)"
            }
            Self::AdmissionToCancel => {
                "speech candidate (admission if no candidate is traced) -> old reply revoked"
            }
            Self::CancelToTailRetired => {
                "old reply revoked -> ALSA retirement of its queued tail (not audible silence)"
            }
            Self::TriggerLateness => "intended stimulus start -> runner start request",
            Self::AcousticSpeechEndToAnswer => {
                "final user speech sound -> first substantive audible answer word (room audio)"
            }
            Self::AcousticInterruptionToSilence => {
                "interrupting speech onset -> Lamp audibly silent (room audio)"
            }
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Latency {
    pub metric: Metric,
    pub kind: MetricKind,
    pub value_ms: f64,
    pub step: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Passed,
    Failed,
    /// Started, but some planned checks could not be scored.
    Incomplete,
    Withheld,
    /// Runner/backend failure; evidence is not acceptable.
    Invalid,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AttemptScore {
    pub attempt_id: String,
    pub scenario: String,
    pub cohort: Cohort,
    pub capability: Capability,
    pub stratum: Stratum,
    pub source: StimulusSource,
    pub repetition: u32,
    pub outcome: Outcome,
    pub reason: Option<String>,
    pub steps: Vec<StepScore>,
    pub findings: Vec<Finding>,
    pub latencies: Vec<Latency>,
    pub acoustic: Vec<AcousticScore>,
    pub admissions: usize,
    pub unattributed_admissions: usize,
    /// Software playback exposure (first write -> retirement/cancellation).
    pub playback_ms: f64,
    /// Whether the trace contained ring requests to check against voice state.
    pub ring_checked: bool,
    pub voices: usize,
    pub reproduce: String,
}

#[derive(Clone, Debug, Default)]
struct Turn {
    admitted: Option<u64>,
    candidate: Option<u64>,
    prefix: Option<u64>,
    endpoint: Option<u64>,
    first_audio: Option<u64>,
    first_write: Option<u64>,
    retired: Option<u64>,
    completed: Option<(u64, String)>,
    cancelled: Option<(u64, Option<String>)>,
    tail_retired: Option<u64>,
}
impl Turn {
    /// When replacement was decided: the candidate if traced, else admission.
    fn decided(&self) -> Option<u64> {
        self.candidate.or(self.admitted)
    }
    fn terminal(&self) -> Option<u64> {
        [
            self.retired,
            self.completed.as_ref().map(|c| c.0),
            self.cancelled.as_ref().map(|c| c.0),
        ]
        .into_iter()
        .flatten()
        .min()
    }
}

fn timeline(events: &[RuntimeEvent]) -> BTreeMap<u64, Turn> {
    let mut turns: BTreeMap<u64, Turn> = BTreeMap::new();
    let candidates: BTreeMap<String, u64> = events
        .iter()
        .filter_map(|e| match (&e.kind, e.at_us) {
            (EventKind::InputCandidate { candidate }, Some(at)) => {
                Some((candidate.to_string(), at))
            }
            _ => None,
        })
        .collect();
    for event in events {
        let (Some(turn), Some(at)) = (event.turn, event.at_us) else {
            continue;
        };
        let entry = turns.entry(turn).or_default();
        match &event.kind {
            EventKind::InputAdmitted {
                prefix_first_read_us,
                candidate,
                ..
            } => {
                entry.admitted.get_or_insert(at);
                entry.prefix = entry.prefix.or(*prefix_first_read_us);
                // The decision point is the candidate; admission is published after
                // the old reply is revoked.
                if let Some(id) = candidate {
                    entry.candidate = entry
                        .candidate
                        .or_else(|| candidates.get(&id.to_string()).copied());
                }
            }
            EventKind::LocalEndpoint { .. } => {
                entry.endpoint.get_or_insert(at);
            }
            EventKind::ProviderFirstAudio => {
                entry.first_audio.get_or_insert(at);
            }
            EventKind::SpeakerFirstWrite => {
                entry.first_write.get_or_insert(at);
            }
            EventKind::SpeechRetired => {
                entry.retired.get_or_insert(at);
            }
            EventKind::TurnCompleted { outcome, .. } => {
                entry.completed.get_or_insert((at, outcome.clone()));
            }
            EventKind::TurnCancelled { reason, .. } => {
                entry.cancelled.get_or_insert((at, reason.clone()));
            }
            EventKind::CancelledTailRetired => {
                entry.tail_retired.get_or_insert(at);
            }
            _ => {}
        }
    }
    turns
}

fn ms(value_us: i128) -> f64 {
    value_us as f64 / 1000.0
}

struct Window {
    step: usize,
    from: u64,
    to: u64,
    /// Clip intervals in the event domain, for lost-opening analysis.
    clips: Vec<(u64, u64)>,
    utterances: Vec<String>,
    /// Admission tolerance after a clip ends (clock uncertainty + path).
    slack: u64,
}

struct Context<'a> {
    record: &'a AttemptRecord,
    turns: BTreeMap<u64, Turn>,
    attributed: BTreeMap<usize, Vec<u64>>,
    unattributed: Vec<u64>,
    windows: Vec<Window>,
    findings: Vec<Finding>,
    latencies: Vec<Latency>,
    simulated: bool,
    same_clock: bool,
    planned_yields: BTreeSet<u64>,
    false_interrupted: BTreeMap<u64, String>,
    /// Admissions that caused a false interruption (counted once there).
    interrupters: BTreeSet<u64>,
    /// Replies whose final sample retired before a later cancellation.
    fully_played: BTreeSet<u64>,
    /// Declared-attribution stimulus steps with no declared turn.
    undeclared: BTreeSet<usize>,
    /// Admissions of background (unaddressed) talk inside multi-talker scenes.
    background: BTreeSet<u64>,
}

impl Context<'_> {
    fn evidence(&self) -> Evidence {
        if self.simulated {
            Evidence::FakeSimulation
        } else if matches!(self.record.attribution, Attribution::Declared { .. }) {
            Evidence::DeclaredAttribution
        } else {
            Evidence::SoftwareTrace
        }
    }
    fn kind(&self) -> MetricKind {
        if self.simulated {
            MetricKind::Simulated
        } else {
            MetricKind::Software
        }
    }
    fn finding(
        &mut self,
        kind: FindingKind,
        severity: Severity,
        step: Option<&str>,
        turn: Option<u64>,
        detail: String,
    ) {
        let evidence = self.evidence();
        self.findings.push(Finding {
            kind,
            severity,
            step: step.map(str::to_owned),
            turn,
            evidence,
            detail,
        });
    }
    fn latency(&mut self, metric: Metric, kind: MetricKind, value_us: i128, step: &str) {
        if value_us >= 0 {
            self.latencies.push(Latency {
                metric,
                kind,
                value_ms: ms(value_us),
                step: step.into(),
            });
        }
    }
    fn step_of(&self, turn: u64) -> Option<usize> {
        self.attributed
            .iter()
            .find(|(_, turns)| turns.contains(&turn))
            .map(|(step, _)| *step)
    }
}

/// Map injected steps into the event clock and attribute each admission to the
/// stimulus whose speech window contains it.
fn attribute(cx: &mut Context) -> Option<String> {
    let record = cx.record;
    let admissions: Vec<(u64, u64)> = cx
        .turns
        .iter()
        .filter_map(|(turn, t)| t.admitted.map(|at| (*turn, at)))
        .collect();
    match &record.attribution {
        Attribution::Declared { turns, absent } => {
            for (step_id, turn) in turns {
                if let Some(index) = record.steps.iter().position(|s| &s.step == step_id) {
                    cx.attributed.entry(index).or_default().push(*turn);
                }
            }
            cx.undeclared = record
                .steps
                .iter()
                .enumerate()
                .filter(|(_, s)| {
                    s.scene.is_some()
                        && !turns.iter().any(|(id, _)| id == &s.step)
                        && !absent.contains(&s.step)
                })
                .map(|(index, _)| index)
                .collect();
            let claimed: BTreeSet<u64> = turns.iter().map(|(_, t)| *t).collect();
            cx.unattributed = admissions
                .iter()
                .map(|(t, _)| *t)
                .filter(|t| !claimed.contains(t))
                .collect();
            None
        }
        Attribution::Timed => {
            let Some(clock) = &record.clock else {
                return Some("no clock mapping between stimulus and runtime clocks".into());
            };
            for (index, step) in record.steps.iter().enumerate() {
                if step.status != StepStatus::Injected {
                    continue;
                }
                let (Some(started), Some(timing)) = (step.started_us, &step.timing) else {
                    continue;
                };
                // A scene without speech (noise only) owns its whole duration.
                let (onset, end) = timing.speech_span_ms.unwrap_or((0, timing.duration_ms));
                let base = clock.to_event(started);
                let tolerance = clock.uncertainty_us;
                cx.windows.push(Window {
                    step: index,
                    from: (base + u64::from(onset) * 1000).saturating_sub(tolerance + 20_000),
                    to: base
                        + u64::from(end) * 1000
                        + 300_000
                        + tolerance
                        + clock.path_allowance_us,
                    clips: timing
                        .clips
                        .iter()
                        .map(|c| {
                            (
                                base + u64::from(c.start_ms) * 1000,
                                base + u64::from(c.end_ms) * 1000,
                            )
                        })
                        .collect(),
                    utterances: timing.clips.iter().map(|c| c.utterance.clone()).collect(),
                    slack: 300_000 + tolerance + clock.path_allowance_us,
                });
            }
            for (turn, at) in admissions {
                let owner = cx
                    .windows
                    .iter()
                    .filter(|w| (w.from..=w.to).contains(&at))
                    .min_by_key(|w| w.from)
                    .map(|w| w.step);
                match owner {
                    Some(step) => cx.attributed.entry(step).or_default().push(turn),
                    None => cx.unattributed.push(turn),
                }
            }
            None
        }
    }
}

/// The admission that caused a `user_interrupted` cancellation at `at`.
fn interrupter(cx: &Context, cancelled: u64, at: u64) -> Option<u64> {
    cx.turns
        .iter()
        .filter(|(turn, t)| {
            **turn != cancelled
                && t.decided()
                    .is_some_and(|a| a.abs_diff(at) <= CAUSE_TOLERANCE_US)
        })
        .min_by_key(|(_, t)| t.decided().map(|a| a.abs_diff(at)))
        .map(|(turn, _)| *turn)
}

fn analyse_cancellations(cx: &mut Context) {
    let cancelled: Vec<(u64, u64, Option<String>, bool)> = cx
        .turns
        .iter()
        .filter_map(|(turn, t)| {
            t.cancelled
                .as_ref()
                .map(|(at, reason)| (*turn, *at, reason.clone(), t.first_write.is_some()))
        })
        .collect();
    let record = cx.record;
    for (turn, at, reason, audible) in cancelled {
        // The runtime finishes a turn only when the provider is idle too, so a
        // reply can retire completely and still be revoked by the next input.
        if cx
            .turns
            .get(&turn)
            .and_then(|t| t.retired)
            .is_some_and(|r| r <= at)
        {
            cx.fully_played.insert(turn);
            continue;
        }
        let state = if audible {
            "playing"
        } else {
            "pending (not yet audible)"
        };
        match reason.as_deref() {
            Some("user_interrupted") => {
                let Some(cause) = interrupter(cx, turn, at) else {
                    cx.finding(
                        FindingKind::RuntimeFailure,
                        Severity::Failure,
                        None,
                        Some(turn),
                        "user_interrupted cancellation without a matching admission".into(),
                    );
                    continue;
                };
                let own_step = cx.step_of(turn);
                match cx.step_of(cause) {
                    Some(step) if own_step == Some(step) && !cx.background.contains(&cause) => {
                        // One stimulus split into several turns; scored as a split.
                    }
                    Some(step)
                        if record.steps[step].expect == Expectation::InterruptAndAnswer
                            && record.steps[step]
                                .bound_turn
                                .is_none_or(|bound| bound == turn) =>
                    {
                        cx.planned_yields.insert(turn);
                    }
                    Some(step) => {
                        let step_id = record.steps[step].step.clone();
                        let source = if cx.background.contains(&cause) {
                            "background speech".to_owned()
                        } else {
                            format!("{:?}", record.steps[step].expect)
                        };
                        let answered = cx
                            .turns
                            .get(&cause)
                            .is_some_and(|t| t.first_write.is_some());
                        let detail = format!(
                            "{state} reply of turn {turn} cancelled by turn {cause}, admitted from step '{step_id}' ({source}){}",
                            if answered {
                                "; that turn was then answered aloud"
                            } else {
                                ""
                            }
                        );
                        cx.false_interrupted
                            .insert(turn, format!("false interruption by step {step_id}"));
                        cx.interrupters.insert(cause);
                        cx.finding(
                            FindingKind::FalseInterruption,
                            Severity::Failure,
                            Some(&step_id),
                            Some(turn),
                            detail,
                        );
                    }
                    None => {
                        let answered = cx
                            .turns
                            .get(&cause)
                            .is_some_and(|t| t.first_write.is_some());
                        let detail = format!(
                            "{state} reply of turn {turn} cancelled by turn {cause}, which matches no stimulus (residual echo, noise or unplanned sound){}",
                            if answered {
                                "; the spurious turn was then answered aloud"
                            } else {
                                ""
                            }
                        );
                        cx.false_interrupted
                            .insert(turn, "false interruption by unattributed input".into());
                        cx.interrupters.insert(cause);
                        // With undeclared stimulus steps the cause may be one of them.
                        let severity = if cx.undeclared.is_empty() {
                            Severity::Failure
                        } else {
                            Severity::Hypothesis
                        };
                        cx.finding(
                            FindingKind::FalseInterruption,
                            severity,
                            None,
                            Some(turn),
                            detail,
                        );
                    }
                }
            }
            Some("provider_interrupted") => {
                let planned = record.steps.iter().enumerate().any(|(index, step)| {
                    step.expect == Expectation::InterruptAndAnswer
                        && step.bound_turn == Some(turn)
                        && cx.attributed.get(&index).is_some_and(|t| !t.is_empty())
                });
                if planned {
                    cx.planned_yields.insert(turn);
                } else {
                    cx.false_interrupted
                        .insert(turn, "provider interruption without user speech".into());
                    cx.finding(FindingKind::FalseInterruption, Severity::Failure, None, Some(turn),
                        format!("{state} reply of turn {turn} cancelled by a provider interruption with no planned interrupting speech"));
                }
            }
            _ => {}
        }
    }
}

/// Speech lost before the retained prefix, from the same simulated clock.
fn lost_opening(cx: &mut Context, step: &StepRecord, index: usize, turn: u64) -> Option<f64> {
    if !cx.same_clock {
        return None;
    }
    let t = cx.turns.get(&turn)?;
    let (admitted, prefix) = (t.admitted?, t.prefix?);
    let window = cx.windows.iter().find(|w| w.step == index)?;
    let (start, _) = window
        .clips
        .iter()
        .copied()
        .filter(|(start, end)| *start <= admitted && admitted <= end + 300_000)
        .max_by_key(|(start, _)| *start)?;
    // `prefix` stamps the end of its 10 ms block.
    let retained_from = prefix.saturating_sub(10_000);
    let lost = ms(i128::from(retained_from) - i128::from(start)).max(0.0);
    if lost >= LOST_OPENING_MIN_MS {
        cx.finding(
            FindingKind::LostOpeningWords,
            Severity::Failure,
            Some(&step.step),
            Some(turn),
            format!("{lost:.0} ms of speech preceded the retained 300 ms prefix"),
        );
    }
    cx.latency(
        Metric::StimulusOnsetToAdmission,
        MetricKind::Simulated,
        i128::from(admitted) - i128::from(start),
        &step.step,
    );
    Some(lost)
}

fn transcript_check(cx: &mut Context, step: &StepRecord, turn: u64) {
    if cx.simulated {
        return;
    }
    let Some(timing) = &step.timing else { return };
    let Some(t) = cx.turns.get(&turn) else { return };
    let (Some(from), until) = (t.admitted, t.endpoint.map(|e| e + 3_000_000)) else {
        return;
    };
    let normalize = |text: &str| -> Vec<String> {
        text.split_whitespace()
            .map(|w| {
                w.chars()
                    .filter(|c| c.is_alphanumeric())
                    .collect::<String>()
                    .to_lowercase()
            })
            .filter(|w| !w.is_empty())
            .collect()
    };
    let heard: Vec<String> = cx
        .record
        .events
        .iter()
        .filter(|e| {
            e.at_us
                .is_some_and(|at| at >= from && until.is_none_or(|u| at <= u))
        })
        .filter_map(|e| match &e.kind {
            EventKind::InputTranscript { text, .. } => Some(normalize(text)),
            _ => None,
        })
        .flatten()
        .collect();
    let Some(first) = timing
        .clips
        .first()
        .and_then(|c| normalize(&c.text).into_iter().next())
    else {
        return;
    };
    if !heard.is_empty() && !heard.iter().take(3).any(|w| *w == first) {
        cx.finding(FindingKind::PossibleLostOpeningWords, Severity::Hypothesis, Some(&step.step), Some(turn),
            format!("provider input transcript does not start with '{first}'; ASR can omit words that were retained"));
    }
}

/// Score the answer a step requested from turn `turn`.
fn score_answer(
    cx: &mut Context,
    step: &StepRecord,
    turn: u64,
    answer_deadline_ms: u32,
) -> (AnswerOutcome, bool) {
    let record = cx.record;
    let first_turn = cx.turns.keys().next().copied();
    let t = cx.turns.get(&turn).cloned().unwrap_or_default();
    if record.provider == ProviderKind::FixedReply && Some(turn) != first_turn {
        return (
            AnswerOutcome::Unsupported {
                reason: "the fixture provider has no reply after the first turn".into(),
            },
            true,
        );
    }
    let kind = cx.kind();
    if let (Some(endpoint), Some(write)) = (t.endpoint, t.first_write) {
        cx.latency(
            Metric::EndpointToFirstWrite,
            kind,
            i128::from(write) - i128::from(endpoint),
            &step.step,
        );
        if let Some(audio) = t.first_audio {
            cx.latency(
                Metric::ProviderFirstAudioToFirstWrite,
                kind,
                i128::from(write) - i128::from(audio),
                &step.step,
            );
        }
        if cx.same_clock
            && let Some(window) = cx
                .windows
                .iter()
                .find(|w| record.steps[w.step].step == step.step)
        {
            let end = window.clips.iter().map(|c| c.1).max().unwrap_or(window.to);
            cx.latency(
                Metric::StimulusEndToFirstWrite,
                MetricKind::Simulated,
                i128::from(write) - i128::from(end),
                &step.step,
            );
        }
    }
    let late = match (t.endpoint, t.first_write) {
        (Some(endpoint), Some(write)) => {
            write.saturating_sub(endpoint) > u64::from(answer_deadline_ms) * 1000
        }
        _ => false,
    };
    if let Some((_, reason)) = &t.cancelled {
        if cx.fully_played.contains(&turn) {
            return (AnswerOutcome::Complete, true);
        }
        if cx.planned_yields.contains(&turn) {
            return (AnswerOutcome::YieldedAsPlanned, true);
        }
        let cause = cx
            .false_interrupted
            .get(&turn)
            .cloned()
            .unwrap_or_else(|| reason.clone().unwrap_or_else(|| "cancelled".into()));
        return if t.first_write.is_some() {
            cx.finding(
                FindingKind::TruncatedAnswer,
                Severity::Failure,
                Some(&step.step),
                Some(turn),
                format!("answer started but ended before completion: {cause}"),
            );
            (AnswerOutcome::Truncated { cause }, false)
        } else {
            cx.finding(
                FindingKind::MissingAnswer,
                Severity::Failure,
                Some(&step.step),
                Some(turn),
                format!("answer revoked before any audio: {cause}"),
            );
            (AnswerOutcome::Missing { cause }, false)
        };
    }
    if late {
        cx.finding(FindingKind::LateAnswer, Severity::Failure, Some(&step.step), Some(turn),
            format!("first reply write more than {answer_deadline_ms} ms after the local endpoint (right-censored)"));
        return (AnswerOutcome::Late, false);
    }
    match t.completed.as_ref().map(|(_, outcome)| outcome.as_str()) {
        Some("audio_written_unscored") => (AnswerOutcome::Complete, true),
        Some("audio_written_with_playback_gaps_unscored") => {
            cx.finding(
                FindingKind::PlaybackGaps,
                Severity::Defect,
                Some(&step.step),
                Some(turn),
                "the reply completed with accepted-zero supply gaps; audible smoothness unscored"
                    .into(),
            );
            (AnswerOutcome::CompleteWithGaps, true)
        }
        Some(other) => {
            let cause = format!("turn completed without an answer ({other})");
            cx.finding(
                FindingKind::MissingAnswer,
                Severity::Failure,
                Some(&step.step),
                Some(turn),
                cause.clone(),
            );
            (AnswerOutcome::Missing { cause }, false)
        }
        None if t.first_write.is_some() => {
            let cause = "no terminal outcome before the session ended".to_owned();
            cx.finding(
                FindingKind::TruncatedAnswer,
                Severity::Failure,
                Some(&step.step),
                Some(turn),
                cause.clone(),
            );
            (AnswerOutcome::Truncated { cause }, false)
        }
        None => {
            let cause = "no reply audio before the session ended".to_owned();
            cx.finding(
                FindingKind::MissingAnswer,
                Severity::Failure,
                Some(&step.step),
                Some(turn),
                cause.clone(),
            );
            (AnswerOutcome::Missing { cause }, false)
        }
    }
}

fn withheld_status(step: &StepRecord) -> Option<(FindingKind, String)> {
    let reason = step.detail.clone().unwrap_or_default();
    match step.status {
        StepStatus::Injected => None,
        StepStatus::TriggerMissed => Some((
            FindingKind::TriggerMissed,
            format!("trigger missed: {reason}"),
        )),
        StepStatus::PreconditionLost => Some((
            FindingKind::PreconditionLost,
            format!("overlap precondition lost: {reason}"),
        )),
        StepStatus::SessionEnded => Some((
            FindingKind::SessionEnded,
            "runtime session ended before the trigger".into(),
        )),
        StepStatus::Withheld => Some((
            FindingKind::NotReached,
            "not reached: an earlier step did not run".into(),
        )),
        StepStatus::TriggerUnavailable => Some((
            FindingKind::TriggerMissed,
            "trigger event unavailable on this backend".into(),
        )),
        StepStatus::TriggerStale => Some((
            FindingKind::TriggerStale,
            format!("trigger stale: {reason}"),
        )),
        StepStatus::DeliveryFailed => Some((FindingKind::StimulusNotDelivered, reason)),
    }
}

pub fn score(
    record: &AttemptRecord,
    answer_deadline_ms: u32,
    annotations: Option<&Annotations>,
) -> AttemptScore {
    let voices = record
        .steps
        .iter()
        .filter_map(|s| s.timing.as_ref())
        .map(|t| t.voices)
        .max()
        .unwrap_or(0);
    let mut score = AttemptScore {
        attempt_id: record.attempt_id.clone(),
        scenario: record.scenario.clone(),
        cohort: record.cohort,
        capability: record.capability,
        stratum: record.stratum,
        source: record.source,
        repetition: record.repetition,
        outcome: Outcome::Withheld,
        reason: None,
        steps: Vec::new(),
        findings: Vec::new(),
        latencies: Vec::new(),
        acoustic: Vec::new(),
        admissions: 0,
        unattributed_admissions: 0,
        playback_ms: 0.0,
        ring_checked: false,
        voices,
        reproduce: record.reproduce.clone(),
    };
    match &record.status {
        AttemptStatus::Withheld { reason } => {
            score.reason = Some(reason.clone());
            score.steps = record
                .steps
                .iter()
                .map(|s| StepScore {
                    step: s.step.clone(),
                    expect: s.expect,
                    status: CheckStatus::Withheld {
                        reason: reason.clone(),
                    },
                    turns: Vec::new(),
                    answer: None,
                    interruption: None,
                    lost_opening_ms: None,
                })
                .collect();
            return score;
        }
        AttemptStatus::Aborted { reason } => {
            score.outcome = Outcome::Invalid;
            score.reason = Some(reason.clone());
        }
        AttemptStatus::Completed => {}
    }
    let simulated = matches!(record.stratum, Stratum::FakeTiming | Stratum::FakeSignal);
    let same_clock = record.clock.as_ref().is_some_and(|c| {
        c.from == c.to && c.uncertainty_us == 0 && matches!(record.attribution, Attribution::Timed)
    });
    let mut cx = Context {
        record,
        turns: timeline(&record.events),
        attributed: BTreeMap::new(),
        unattributed: Vec::new(),
        windows: Vec::new(),
        findings: Vec::new(),
        latencies: Vec::new(),
        simulated,
        same_clock,
        planned_yields: BTreeSet::new(),
        false_interrupted: BTreeMap::new(),
        interrupters: BTreeSet::new(),
        fully_played: BTreeSet::new(),
        undeclared: BTreeSet::new(),
        background: BTreeSet::new(),
    };
    let trace_present = record
        .events
        .iter()
        .any(|e| matches!(e.kind, EventKind::RunStart { .. }))
        && record
            .events
            .iter()
            .any(|e| matches!(e.kind, EventKind::RunEnd { .. }));
    let unscorable = if !trace_present {
        Some("no complete runtime trace (run_start and run_end required)".to_owned())
    } else {
        attribute(&mut cx)
    };
    score.admissions = cx.turns.values().filter(|t| t.admitted.is_some()).count();
    score.playback_ms = cx
        .turns
        .values()
        .filter_map(|t| Some(ms(i128::from(t.terminal()?) - i128::from(t.first_write?))))
        .filter(|v| *v > 0.0)
        .sum();
    if let Some(reason) = unscorable {
        cx.finding(
            FindingKind::EvidenceMissing,
            Severity::Withheld,
            None,
            None,
            reason.clone(),
        );
        score.steps = record
            .steps
            .iter()
            .map(|s| StepScore {
                step: s.step.clone(),
                expect: s.expect,
                status: CheckStatus::Unscored {
                    reason: reason.clone(),
                },
                turns: Vec::new(),
                answer: None,
                interruption: None,
                lost_opening_ms: None,
            })
            .collect();
        score.findings = cx.findings;
        if score.outcome != Outcome::Invalid {
            score.outcome = Outcome::Incomplete;
            score.reason = Some(reason);
        }
        return score;
    }
    classify_background(&mut cx);
    analyse_cancellations(&mut cx);
    score.ring_checked = consistency(&mut cx);
    let expects_failure = record.steps.iter().any(|s| {
        matches!(
            s.expect,
            Expectation::HonestFailure | Expectation::RecoverAndAnswer
        )
    });
    let run_failed = record
        .events
        .iter()
        .any(|e| matches!(&e.kind, EventKind::RunEnd { status: Some(s), .. } if s == "failed"));
    let mut steps = Vec::new();
    for (index, step) in record.steps.iter().enumerate() {
        let turns = cx.attributed.get(&index).cloned().unwrap_or_default();
        let mut step_score = StepScore {
            step: step.step.clone(),
            expect: step.expect,
            status: CheckStatus::Pass,
            turns: turns.clone(),
            answer: None,
            interruption: None,
            lost_opening_ms: None,
        };
        // A session that ended after an injected failure, before the recovery
        // request could be asked, did not recover: that is a failure.
        if step.expect == Expectation::RecoverAndAnswer
            && matches!(
                step.status,
                StepStatus::SessionEnded | StepStatus::TriggerMissed
            )
            && run_failed
        {
            cx.finding(FindingKind::NoRecovery, Severity::Failure, Some(&step.step), None,
                "the session ended after the provider failure; the follow-up request could not be asked".into());
            step_score.status = CheckStatus::Fail;
            step_score.answer = Some(AnswerOutcome::Missing {
                cause: "no recovery after provider failure".into(),
            });
            steps.push(step_score);
            continue;
        }
        if let Some((kind, detail)) = withheld_status(step) {
            cx.finding(
                kind,
                Severity::Withheld,
                Some(&step.step),
                None,
                detail.clone(),
            );
            step_score.status = CheckStatus::Withheld { reason: detail };
            steps.push(step_score);
            continue;
        }
        if cx.undeclared.contains(&index) {
            step_score.status = CheckStatus::Unscored {
                reason: "no declared turn for this stimulus".into(),
            };
            steps.push(step_score);
            continue;
        }
        let onset = cx
            .windows
            .iter()
            .find(|w| w.step == index)
            .and_then(|w| w.clips.iter().map(|c| c.0).min());
        let bound_end = step
            .bound_turn
            .and_then(|b| cx.turns.get(&b))
            .and_then(Turn::terminal);
        // Overlap steps test speech during playback; if playback had ended
        // before the stimulus reached the runtime, the condition was not met.
        let overlap_missed = matches!(
            step.expect,
            Expectation::NoInterrupt | Expectation::InterruptAndAnswer
        ) && onset
            .zip(bound_end)
            .is_some_and(|(onset, end)| end <= onset);
        let request = matches!(
            step.expect,
            Expectation::Answer
                | Expectation::InterruptAndAnswer
                | Expectation::HonestFailure
                | Expectation::RecoverAndAnswer
        );
        let fail = |status: &mut CheckStatus| *status = CheckStatus::Fail;
        // In a multi-talker scene only admissions during addressed speech belong
        // to the request; the rest are responses to background talk.
        let (background, turns): (Vec<u64>, Vec<u64>) = turns
            .into_iter()
            .partition(|turn| cx.background.contains(turn));
        if let Some(&first) = turns.first().filter(|_| request) {
            step_score.lost_opening_ms = lost_opening(&mut cx, step, index, first);
            transcript_check(&mut cx, step, first);
        }
        for &turn in &background {
            // Counted once: as the false interruption it caused, if any.
            if cx.interrupters.contains(&turn) {
                continue;
            }
            let audible = cx.turns.get(&turn).is_some_and(|t| t.first_write.is_some());
            cx.finding(
                FindingKind::UnexpectedResponse,
                Severity::Failure,
                Some(&step.step),
                Some(turn),
                format!(
                    "background speech admitted as turn {turn}{}",
                    if audible { " and answered aloud" } else { "" }
                ),
            );
        }
        if !background.is_empty() && request {
            fail(&mut step_score.status);
        }
        // One request answered aloud more than once (for example after a split).
        let answered: Vec<u64> = turns
            .iter()
            .copied()
            .filter(|t| cx.turns.get(t).is_some_and(|t| t.first_write.is_some()))
            .collect();
        if request && answered.len() > 1 {
            cx.finding(
                FindingKind::DuplicateAnswer,
                Severity::Failure,
                Some(&step.step),
                answered.last().copied(),
                format!(
                    "one request was answered aloud {} times (turns {answered:?})",
                    answered.len()
                ),
            );
            fail(&mut step_score.status);
        }
        match step.expect {
            Expectation::Fragment => {
                step_score.status = CheckStatus::Unscored {
                    reason: "unfinished utterance; judged by the step that continues it".into(),
                };
            }
            Expectation::Answer | Expectation::HonestFailure | Expectation::RecoverAndAnswer
                if turns.is_empty() =>
            {
                cx.finding(
                    FindingKind::MissedInput,
                    Severity::Failure,
                    Some(&step.step),
                    None,
                    "the stimulus was never admitted as a turn".into(),
                );
                step_score.answer = Some(AnswerOutcome::NotAdmitted);
                fail(&mut step_score.status);
            }
            Expectation::Answer | Expectation::RecoverAndAnswer => {
                if turns.len() > 1 {
                    cx.finding(FindingKind::TurnSplit, Severity::Failure, Some(&step.step), turns.last().copied(),
                        format!("one stimulus became {} turns ({turns:?}); earlier parts were cancelled", turns.len()));
                    fail(&mut step_score.status);
                }
                let (answer, ok) = score_answer(
                    &mut cx,
                    step,
                    *turns.last().expect("nonempty"),
                    answer_deadline_ms,
                );
                if !ok {
                    fail(&mut step_score.status);
                } else if let AnswerOutcome::Unsupported { reason } = &answer
                    && step_score.status == CheckStatus::Pass
                {
                    step_score.status = CheckStatus::Unscored {
                        reason: reason.clone(),
                    };
                }
                step_score.answer = Some(answer);
            }
            Expectation::InterruptAndAnswer if overlap_missed => {
                cx.finding(
                    FindingKind::OverlapNotAchieved,
                    Severity::Withheld,
                    Some(&step.step),
                    step.bound_turn,
                    "the answer had already ended when the interrupting speech started".into(),
                );
                step_score.status = CheckStatus::Unscored {
                    reason: "overlap not achieved".into(),
                };
            }
            Expectation::InterruptAndAnswer => {
                let bound = step.bound_turn;
                match turns.first() {
                    None => {
                        step_score.interruption = Some(false);
                        cx.finding(FindingKind::MissedInterruption, Severity::Failure, Some(&step.step), bound,
                            "the interrupting speech was never admitted; the old answer kept playing".into());
                        fail(&mut step_score.status);
                    }
                    Some(&first) => {
                        let b = bound
                            .and_then(|b| cx.turns.get(&b).cloned())
                            .unwrap_or_default();
                        let admitted = cx.turns.get(&first).and_then(Turn::decided).unwrap_or(0);
                        match &b.cancelled {
                            Some((at, _))
                                if bound.is_some_and(|b| cx.planned_yields.contains(&b)) =>
                            {
                                step_score.interruption = Some(true);
                                let kind = cx.kind();
                                cx.latency(
                                    Metric::AdmissionToCancel,
                                    kind,
                                    i128::from(*at) - i128::from(admitted),
                                    &step.step,
                                );
                                if let Some(tail) = b.tail_retired {
                                    cx.latency(
                                        Metric::CancelToTailRetired,
                                        kind,
                                        i128::from(tail) - i128::from(*at),
                                        &step.step,
                                    );
                                }
                            }
                            _ if b.terminal().is_some_and(|end| end < admitted) => {
                                cx.finding(FindingKind::OverlapNotAchieved, Severity::Withheld, Some(&step.step), bound,
                                    "the answer had already ended when the interruption was admitted".into());
                                step_score.status = CheckStatus::Unscored {
                                    reason: "overlap not achieved".into(),
                                };
                            }
                            _ => {
                                step_score.interruption = Some(false);
                                cx.finding(FindingKind::MissedInterruption, Severity::Failure, Some(&step.step), bound,
                                    "the interruption was admitted but did not revoke the playing answer".into());
                                fail(&mut step_score.status);
                            }
                        }
                        if turns.len() > 1 {
                            cx.finding(
                                FindingKind::TurnSplit,
                                Severity::Failure,
                                Some(&step.step),
                                turns.last().copied(),
                                format!("the interrupting request became {} turns", turns.len()),
                            );
                            fail(&mut step_score.status);
                        }
                        let (answer, ok) = score_answer(
                            &mut cx,
                            step,
                            *turns.last().expect("nonempty"),
                            answer_deadline_ms,
                        );
                        if !ok {
                            fail(&mut step_score.status);
                        } else if let AnswerOutcome::Unsupported { reason } = &answer
                            && step_score.status == CheckStatus::Pass
                        {
                            // The yield is scored in `interruption`; the answer is not.
                            step_score.status = CheckStatus::Unscored {
                                reason: format!("yield scored; {reason}"),
                            };
                        }
                        step_score.answer = Some(answer);
                    }
                }
            }
            Expectation::NoInterrupt if overlap_missed => {
                cx.finding(
                    FindingKind::OverlapNotAchieved,
                    Severity::Withheld,
                    Some(&step.step),
                    step.bound_turn,
                    "the answer had already ended when the stimulus started".into(),
                );
                step_score.status = CheckStatus::Unscored {
                    reason: "overlap not achieved".into(),
                };
            }
            Expectation::NoInterrupt => {
                if let Some(&first) = turns.first()
                    && !cx.interrupters.contains(&first)
                {
                    let audible = cx
                        .turns
                        .get(&first)
                        .is_some_and(|t| t.first_write.is_some());
                    cx.finding(
                        FindingKind::UnexpectedResponse,
                        Severity::Failure,
                        Some(&step.step),
                        Some(first),
                        format!(
                            "a non-request was admitted as turn {first}{}",
                            if audible { " and answered aloud" } else { "" }
                        ),
                    );
                }
                if !turns.is_empty() {
                    fail(&mut step_score.status);
                }
            }
            Expectation::Silence => {
                for &turn in &turns {
                    let audible = cx.turns.get(&turn).is_some_and(|t| t.first_write.is_some());
                    cx.finding(
                        FindingKind::UnexpectedResponse,
                        Severity::Failure,
                        Some(&step.step),
                        Some(turn),
                        format!(
                            "unaddressed sound admitted as turn {turn}{}",
                            if audible { " and answered aloud" } else { "" }
                        ),
                    );
                }
                if !turns.is_empty() {
                    fail(&mut step_score.status);
                }
            }
            Expectation::Observe => {
                let (from, to) = match (step.started_us, &record.clock) {
                    (Some(started), Some(clock)) => {
                        let end = step
                            .bound_turn
                            .and_then(|b| cx.turns.get(&b))
                            .and_then(Turn::terminal)
                            .unwrap_or(u64::MAX);
                        (clock.to_event(started), end)
                    }
                    _ => (0, u64::MAX),
                };
                let echoes: Vec<u64> = cx
                    .unattributed
                    .iter()
                    .copied()
                    .filter(|t| {
                        cx.turns
                            .get(t)
                            .and_then(|t| t.admitted)
                            .is_some_and(|a| (from..=to).contains(&a))
                    })
                    .collect();
                if !echoes.is_empty() {
                    step_score.turns = echoes;
                    fail(&mut step_score.status);
                }
            }
            Expectation::HonestFailure => {
                let turn = *turns.last().expect("nonempty");
                let t = cx.turns.get(&turn).cloned().unwrap_or_default();
                let failed_run = record.events.iter().any(|e| {
                    matches!(&e.kind, EventKind::RunEnd { status: Some(s), .. } if s == "failed")
                });
                let honest = matches!(&t.cancelled, Some((_, Some(r))) if r == "runtime_failed")
                    || (failed_run && t.completed.is_none() && t.cancelled.is_none());
                // Another cancellation can end the turn before the injected fault
                // fires; then no failure happened and honesty cannot be judged.
                let pre_empted = match &t.cancelled {
                    Some((_, Some(reason))) if reason != "runtime_failed" => Some(reason.clone()),
                    _ => None,
                };
                if t.completed
                    .as_ref()
                    .is_some_and(|(_, o)| o.starts_with("audio_written"))
                {
                    cx.finding(
                        FindingKind::FabricatedCompletion,
                        Severity::Failure,
                        Some(&step.step),
                        Some(turn),
                        "a faulted turn was reported as a completed answer".into(),
                    );
                    fail(&mut step_score.status);
                } else if let Some(reason) = pre_empted {
                    step_score.status = CheckStatus::Unscored {
                        reason: format!(
                            "the injected fault was pre-empted: the turn ended as {reason}"
                        ),
                    };
                } else if !honest {
                    cx.finding(
                        FindingKind::RuntimeFailure,
                        Severity::Failure,
                        Some(&step.step),
                        Some(turn),
                        "the injected provider fault produced no explicit failure outcome".into(),
                    );
                    fail(&mut step_score.status);
                } else {
                    match record.spoken_failure_notice {
                        Some(true) => {}
                        Some(false) => {
                            cx.finding(FindingKind::UnannouncedFailure, Severity::Failure, Some(&step.step), Some(turn),
                                "the runtime has no spoken failure notice; the person hears the answer stop or never start".into());
                            fail(&mut step_score.status);
                        }
                        None => {
                            step_score.status = CheckStatus::Unscored {
                                reason: "spoken failure notice requires listening".into(),
                            };
                        }
                    }
                }
                // A deliberately faulted answer is scored for honesty, not completion.
                step_score.answer = None;
            }
        }
        steps.push(step_score);
    }
    // Admissions that match no stimulus and cancelled nothing are still responses.
    for turn in cx.unattributed.clone() {
        let cancelled_something = cx.turns.iter().any(|(other, t)| {
            *other != turn
                && matches!(&t.cancelled, Some((at, Some(r))) if r == "user_interrupted" && interrupter(&cx, *other, *at) == Some(turn))
        });
        if !cancelled_something {
            let audible = cx.turns.get(&turn).is_some_and(|t| t.first_write.is_some());
            let severity = if cx.undeclared.is_empty() {
                Severity::Failure
            } else {
                Severity::Hypothesis
            };
            cx.finding(
                FindingKind::UnexpectedResponse,
                severity,
                None,
                Some(turn),
                format!(
                    "turn {turn} matches no stimulus{}",
                    if audible {
                        " and was answered aloud"
                    } else {
                        ""
                    }
                ),
            );
        }
    }
    let mut runtime_faults = BTreeSet::new();
    for event in &record.events {
        match &event.kind {
            EventKind::RuntimeFault { reason } => {
                runtime_faults.insert(reason.clone());
            }
            EventKind::PlaybackDiscarded { expected: false } => {
                runtime_faults.insert("unexpected playback discard".into());
            }
            EventKind::RunEnd {
                status: Some(status),
                error,
            } if status == "failed" && !expects_failure => {
                runtime_faults.insert(format!("run failed: {}", error.clone().unwrap_or_default()));
            }
            _ => {}
        }
    }
    for reason in runtime_faults {
        cx.finding(
            FindingKind::RuntimeFailure,
            Severity::Failure,
            None,
            None,
            reason,
        );
    }
    for step in &record.steps {
        if let Some(lateness) = step
            .started_us
            .zip(step.target_us)
            .map(|(s, t)| i128::from(s) - i128::from(t))
        {
            cx.latency(
                Metric::TriggerLateness,
                MetricKind::Runner,
                lateness.max(0),
                &step.step,
            );
            if lateness > i128::from(LATE_STIMULUS_US) {
                cx.finding(
                    FindingKind::LateStimulus,
                    Severity::Withheld,
                    Some(&step.step),
                    None,
                    format!(
                        "stimulus started {} ms after its planned time; the tested phase differs",
                        lateness / 1000
                    ),
                );
            }
        }
    }
    if let Some(annotations) = annotations {
        score.acoustic = annotations.score(record);
        review_findings(&mut cx, &mut steps, annotations);
    } else if record.stratum.is_physical() {
        score.acoustic = record
            .steps
            .iter()
            .filter(|s| s.status == StepStatus::Injected && s.scene.is_some())
            .map(|s| AcousticScore::unscored(&s.step, "no reviewed room-audio annotation"))
            .collect();
    }
    for acoustic in &score.acoustic {
        if let Some((metric, value)) = acoustic.measurement() {
            cx.latencies.push(Latency {
                metric,
                kind: MetricKind::Acoustic,
                value_ms: value,
                step: acoustic.step.clone(),
            });
        }
    }
    score.unattributed_admissions = cx.unattributed.len();
    let failed = cx
        .findings
        .iter()
        .any(|f| matches!(f.severity, Severity::Failure | Severity::Defect))
        || steps.iter().any(|s| s.status == CheckStatus::Fail);
    let unscored = steps.iter().any(|s| {
        matches!(
            s.status,
            CheckStatus::Unscored { .. } | CheckStatus::Withheld { .. }
        )
    });
    if score.outcome != Outcome::Invalid {
        score.outcome = if failed {
            Outcome::Failed
        } else if unscored || !steps.iter().any(|s| s.status == CheckStatus::Pass) {
            Outcome::Incomplete
        } else {
            Outcome::Passed
        };
    }
    score.steps = steps;
    score.findings = cx.findings;
    score.latencies = cx.latencies;
    score
}

/// Reviewer judgments from a listened annotation of the matching recording.
fn review_findings(cx: &mut Context, steps: &mut [StepScore], annotations: &Annotations) {
    let record = cx.record;
    for step in steps.iter_mut() {
        let Some(review) = annotations.reviewed_step(record, &step.step) else {
            continue;
        };
        let mut push = |kind, detail: &str| {
            cx.findings.push(Finding {
                kind,
                severity: Severity::Failure,
                step: Some(step.step.clone()),
                turn: None,
                evidence: Evidence::RoomAnnotation,
                detail: detail.into(),
            });
        };
        if review.answer_complete == Some(false) {
            push(
                FindingKind::IncompleteAnswer,
                "the reviewer heard an incomplete answer",
            );
            step.status = CheckStatus::Fail;
        }
        if review.answer_relevant == Some(false) {
            push(
                FindingKind::IrrelevantAnswer,
                "the reviewer judged the answer irrelevant",
            );
            step.status = CheckStatus::Fail;
        }
        if step.expect == Expectation::HonestFailure && record.spoken_failure_notice.is_none() {
            match review.spoken_failure_notice {
                Some(false) => {
                    push(
                        FindingKind::UnannouncedFailure,
                        "the reviewer heard no spoken failure notice",
                    );
                    step.status = CheckStatus::Fail;
                }
                Some(true) if matches!(&step.status, CheckStatus::Unscored { reason } if reason.contains("requires listening")) =>
                {
                    step.status = CheckStatus::Pass;
                }
                _ => {}
            }
        }
    }
}

/// Ordering tolerance between voice events and ring requests on one clock.
const STATE_TOLERANCE_US: u64 = 50_000;

/// Voice and ring state through cancellation: no output for a revoked turn,
/// one speaking reply at a time, every turn terminated, and ring phases that
/// match voice state. Returns whether ring requests were present to check.
fn consistency(cx: &mut Context) -> bool {
    let record = cx.record;
    let tol = STATE_TOLERANCE_US;
    let mut found: Vec<(FindingKind, Option<u64>, String)> = Vec::new();
    let run_failed = record
        .events
        .iter()
        .any(|e| matches!(&e.kind, EventKind::RunEnd { status: Some(s), .. } if s == "failed"));
    for (turn, t) in &cx.turns {
        if t.admitted.is_some() && t.completed.is_none() && t.cancelled.is_none() && !run_failed {
            found.push((
                FindingKind::UnterminatedTurn,
                Some(*turn),
                format!("turn {turn} has no completion or cancellation"),
            ));
        }
    }
    let mut ring = false;
    for event in &record.events {
        let (Some(turn), Some(at)) = (event.turn, event.at_us) else {
            continue;
        };
        let Some(t) = cx.turns.get(&turn) else {
            continue;
        };
        let revoked = t.cancelled.as_ref().map(|(c, _)| *c);
        let output = matches!(
            event.kind,
            EventKind::SpeakerFirstWrite | EventKind::SpeechRetired | EventKind::PlaybackGap { .. }
        );
        if output && revoked.is_some_and(|c| at > c + tol) {
            found.push((
                FindingKind::StaleOutput,
                Some(turn),
                format!(
                    "{} for turn {turn} {} ms after it was revoked",
                    event.name(),
                    (at - revoked.unwrap_or(at)) / 1000
                ),
            ));
        }
        let EventKind::RingRequested { phase } = &event.kind else {
            continue;
        };
        ring = true;
        let ended = t.completed.as_ref().map(|c| c.0).or(revoked);
        if ended.is_some_and(|end| at > end + tol) {
            found.push((
                FindingKind::StaleOutput,
                Some(turn),
                format!("{phase} ring cue for turn {turn} after the turn ended"),
            ));
            continue;
        }
        let playing_from = t.first_write;
        let playing_to = t.retired.or(revoked).or(ended);
        let within = |from: Option<u64>, to: Option<u64>| {
            from.is_some_and(|f| at + tol >= f) && to.is_none_or(|e| at <= e + tol)
        };
        let consistent = match phase.as_str() {
            "listening" => within(t.decided().or(t.admitted), t.endpoint.or(ended)),
            "speaking" => within(playing_from, playing_to),
            "waiting" => {
                t.endpoint.is_some_and(|e| at + tol >= e)
                    && !(playing_from.is_some_and(|f| at > f + tol)
                        && playing_to.is_none_or(|e| at + tol < e))
            }
            _ => false,
        };
        if !consistent {
            found.push((
                FindingKind::RingMismatch,
                Some(turn),
                format!("ring requested {phase} for turn {turn} when its voice state disagreed"),
            ));
        }
    }
    // One speaking reply at a time: a new first write must follow the
    // previous reply's retirement or revocation.
    let mut intervals: Vec<(u64, u64, u64)> = cx
        .turns
        .iter()
        .filter_map(|(turn, t)| {
            let start = t.first_write?;
            let end = t
                .retired
                .or(t.cancelled.as_ref().map(|c| c.0))
                .unwrap_or(u64::MAX);
            Some((start, end, *turn))
        })
        .collect();
    intervals.sort();
    for pair in intervals.windows(2) {
        let ((_, end, first), (start, _, second)) = (pair[0], pair[1]);
        if start + tol < end {
            found.push((
                FindingKind::OverlappingOutput,
                Some(second),
                format!("turn {second} started playing while turn {first} was still playing"),
            ));
        }
    }
    found.sort();
    found.dedup();
    for (kind, turn, detail) in found {
        cx.finding(kind, Severity::Failure, None, turn, detail);
    }
    ring
}

/// Mark admissions in multi-talker scenes whose captured input never overlaps
/// an utterance addressed to Lamp: they are responses to background talk.
fn classify_background(cx: &mut Context) {
    let record = cx.record;
    let mut background = BTreeSet::new();
    for (index, turns) in &cx.attributed {
        let step = &record.steps[*index];
        if step.addressed.is_empty() {
            continue;
        }
        let Some(window) = cx.windows.iter().find(|w| w.step == *index) else {
            continue;
        };
        let addressed: Vec<(u64, u64)> = window
            .clips
            .iter()
            .zip(&window.utterances)
            .filter(|(_, id)| step.addressed.contains(id))
            .map(|(clip, _)| *clip)
            .collect();
        for turn in turns {
            let Some(t) = cx.turns.get(turn) else {
                continue;
            };
            let from = t.prefix.or(t.decided()).unwrap_or(0);
            let to = t.endpoint.or(t.terminal()).unwrap_or(u64::MAX);
            let hears_request = addressed
                .iter()
                .any(|(start, end)| from <= end + window.slack && to + 20_000 >= *start);
            if !hears_request {
                background.insert(*turn);
            }
        }
    }
    cx.background = background;
}
