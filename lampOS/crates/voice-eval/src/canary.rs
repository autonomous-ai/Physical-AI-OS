//! Evaluator self-test. Before any report is trusted, deliberately injected
//! failures must each be detected and clean controls must pass. Failures are
//! injected two ways: through the fake runtime's own fault modes, and by
//! mutating a clean attempt's events in a known way.
use crate::{
    Result,
    evaluate::{self, AttemptScore, FindingKind, Outcome},
    events::{ClockDomain, EventKind, EventSource, RuntimeEvent},
    fake::FakeBackend,
    plan::LoadedPlan,
    record::{AttemptRecord, Stratum},
    runner::{AttemptContext, run_attempt},
    stimulus::StimulusCatalog,
};
use serde::Serialize;
use serde_json::json;
use std::path::PathBuf;

/// Version of the self-test contract, separate from the run-plan schema.
pub const SUITE_VERSION: u32 = 1;

/// Exact embedded inputs used by the self-test, never the caller's run plan.
#[derive(Clone, Debug, Serialize)]
pub struct CanonicalPlan {
    pub suite_version: u32,
    pub plan_id: String,
    pub plan_version: u32,
    pub plan_sha256: String,
    pub desk_catalog_sha256: String,
    pub extension_catalog_sha256: String,
    pub merged_catalog_sha256: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct CanaryResult {
    pub canonical_plan: CanonicalPlan,
    pub name: &'static str,
    /// What must be observed: a finding kind, or a passing control.
    pub expects: String,
    pub detected: bool,
    pub outcome: Outcome,
    pub findings: Vec<FindingKind>,
}

type Mutation = fn(&mut AttemptRecord);

#[derive(Clone, Copy, Debug)]
enum Expect {
    /// A clean control: passes with no failure or defect finding.
    Pass,
    /// The injected failure must be reported, and the attempt must not pass.
    Finding(FindingKind),
    /// No acoustic number may appear without a reviewed annotation.
    AcousticUnmeasured,
}

struct Canary {
    name: &'static str,
    profile: &'static str,
    scenario: &'static str,
    mutate: Option<Mutation>,
    expect: Expect,
}

fn event(kind: EventKind, turn: u64, at: u64) -> RuntimeEvent {
    RuntimeEvent::new(
        kind,
        Some(turn),
        at,
        ClockDomain::Virtual,
        EventSource::Fake,
    )
}

fn at_of(record: &AttemptRecord, turn: u64, pick: fn(&EventKind) -> bool) -> u64 {
    record
        .events
        .iter()
        .find(|e| e.turn == Some(turn) && pick(&e.kind))
        .and_then(|e| e.at_us)
        .unwrap_or(0)
}

fn drop_turn_events(record: &mut AttemptRecord, turn: u64, pick: fn(&EventKind) -> bool) {
    record
        .events
        .retain(|e| !(e.turn == Some(turn) && pick(&e.kind)));
}

const CANARIES: &[Canary] = &[
    Canary {
        name: "clean conversation passes",
        profile: "v2-directed-current",
        scenario: "quick-chat",
        mutate: None,
        expect: Expect::Pass,
    },
    Canary {
        name: "kept silence passes",
        profile: "v2-directed-current",
        scenario: "noise-only",
        mutate: None,
        expect: Expect::Pass,
    },
    Canary {
        name: "planned interruption passes",
        profile: "v2-directed-current",
        scenario: "topic-change",
        mutate: None,
        expect: Expect::Pass,
    },
    Canary {
        name: "playback echo interrupts Lamp",
        profile: "v2-echo-leak-h4",
        scenario: "fixed-reply-echo-only",
        mutate: None,
        expect: Expect::Finding(FindingKind::FalseInterruption),
    },
    Canary {
        name: "acknowledgment interrupts Lamp",
        profile: "v2-directed-current",
        scenario: "listener-acknowledgment",
        mutate: None,
        expect: Expect::Finding(FindingKind::FalseInterruption),
    },
    Canary {
        name: "unaddressed talk is answered",
        profile: "v2-directed-current",
        scenario: "two-colleagues",
        mutate: None,
        expect: Expect::Finding(FindingKind::UnexpectedResponse),
    },
    Canary {
        name: "hesitation splits the turn",
        profile: "v2-directed-current",
        scenario: "hesitant-sharing",
        mutate: None,
        expect: Expect::Finding(FindingKind::TurnSplit),
    },
    Canary {
        name: "supply gap during the answer",
        profile: "v2-directed-current",
        scenario: "provider-supply-gap",
        mutate: None,
        expect: Expect::Finding(FindingKind::PlaybackGaps),
    },
    Canary {
        name: "answer after the deadline",
        profile: "v2-directed-current",
        scenario: "provider-late-answer",
        mutate: None,
        expect: Expect::Finding(FindingKind::LateAnswer),
    },
    Canary {
        name: "failure without a spoken notice",
        profile: "v2-directed-current",
        scenario: "provider-failure-before-audio",
        mutate: None,
        expect: Expect::Finding(FindingKind::UnannouncedFailure),
    },
    Canary {
        name: "session does not recover",
        profile: "v2-directed-current",
        scenario: "disconnect-between-turns",
        mutate: None,
        expect: Expect::Finding(FindingKind::NoRecovery),
    },
    Canary {
        name: "interruption is ignored",
        profile: "v2-directed-current",
        scenario: "topic-change",
        mutate: Some(|r| {
            // Remove the interrupting turn: the story keeps playing.
            r.events.retain(|e| e.turn != Some(2));
            r.events.retain(|e| {
                !matches!(
                    e.kind,
                    EventKind::TurnCancelled { .. }
                        | EventKind::CancelledTailRetired
                        | EventKind::InputCandidate { .. }
                )
            });
        }),
        expect: Expect::Finding(FindingKind::MissedInterruption),
    },
    Canary {
        name: "answer never plays",
        profile: "v2-directed-current",
        scenario: "quick-chat",
        mutate: Some(|r| {
            let done = at_of(r, 1, |k| matches!(k, EventKind::TurnCompleted { .. }));
            drop_turn_events(r, 1, |k| {
                matches!(
                    k,
                    EventKind::SpeakerFirstWrite
                        | EventKind::SpeechRetired
                        | EventKind::TurnCompleted { .. }
                        | EventKind::RingRequested { .. }
                )
            });
            r.events.push(event(
                EventKind::TurnCompleted {
                    outcome: "no_audio_answer".into(),
                    playback_gaps: 0,
                },
                1,
                done,
            ));
        }),
        expect: Expect::Finding(FindingKind::MissingAnswer),
    },
    Canary {
        name: "answer is cut short",
        profile: "v2-directed-current",
        scenario: "long-answer",
        mutate: Some(|r| {
            let write = at_of(r, 1, |k| matches!(k, EventKind::SpeakerFirstWrite));
            drop_turn_events(r, 1, |k| {
                matches!(
                    k,
                    EventKind::SpeechRetired | EventKind::TurnCompleted { .. }
                )
            });
            r.events.push(event(
                EventKind::TurnCancelled {
                    reason: Some("runtime_failed".into()),
                    provider_audio_seen: Some(true),
                },
                1,
                write + 2_000_000,
            ));
        }),
        expect: Expect::Finding(FindingKind::TruncatedAnswer),
    },
    Canary {
        name: "one request answered twice",
        profile: "v2-directed-current",
        scenario: "quick-chat",
        mutate: Some(|r| {
            let admitted = at_of(r, 1, |k| matches!(k, EventKind::InputAdmitted { .. }));
            let write = at_of(r, 1, |k| matches!(k, EventKind::SpeakerFirstWrite));
            r.events.push(event(
                EventKind::InputAdmitted {
                    prefix_first_read_us: Some(admitted + 300_000),
                    candidate: None,
                    basis: None,
                },
                2,
                admitted + 600_000,
            ));
            r.events
                .push(event(EventKind::SpeakerFirstWrite, 2, write + 9_000_000));
            r.events
                .push(event(EventKind::SpeechRetired, 2, write + 10_000_000));
            r.events.push(event(
                EventKind::TurnCompleted {
                    outcome: "audio_written_unscored".into(),
                    playback_gaps: 0,
                },
                2,
                write + 10_000_000,
            ));
        }),
        expect: Expect::Finding(FindingKind::DuplicateAnswer),
    },
    Canary {
        name: "opening words are dropped",
        profile: "v2-directed-current",
        scenario: "quick-chat",
        mutate: Some(|r| {
            for e in &mut r.events {
                if let EventKind::InputAdmitted {
                    prefix_first_read_us: Some(prefix),
                    ..
                } = &mut e.kind
                {
                    *prefix += 400_000;
                }
            }
        }),
        expect: Expect::Finding(FindingKind::LostOpeningWords),
    },
    Canary {
        name: "revoked reply keeps playing",
        profile: "v2-directed-current",
        scenario: "topic-change",
        mutate: Some(|r| {
            let cancel = at_of(r, 1, |k| matches!(k, EventKind::TurnCancelled { .. }));
            r.events
                .push(event(EventKind::SpeechRetired, 1, cancel + 700_000));
        }),
        expect: Expect::Finding(FindingKind::StaleOutput),
    },
    Canary {
        name: "ring speaks before the voice",
        profile: "v2-directed-current",
        scenario: "topic-change",
        mutate: Some(|r| {
            let admitted = at_of(r, 2, |k| matches!(k, EventKind::InputAdmitted { .. }));
            r.events.push(event(
                EventKind::RingRequested {
                    phase: "speaking".into(),
                },
                2,
                admitted + 100_000,
            ));
        }),
        expect: Expect::Finding(FindingKind::RingMismatch),
    },
    Canary {
        name: "two replies play at once",
        profile: "v2-directed-current",
        scenario: "topic-change",
        mutate: Some(|r| drop_turn_events(r, 1, |k| matches!(k, EventKind::TurnCancelled { .. }))),
        expect: Expect::Finding(FindingKind::OverlappingOutput),
    },
    Canary {
        name: "partial trace is not scored",
        profile: "v2-directed-current",
        scenario: "quick-chat",
        mutate: Some(|r| {
            r.events
                .retain(|e| !matches!(e.kind, EventKind::RunEnd { .. }))
        }),
        expect: Expect::Finding(FindingKind::EvidenceMissing),
    },
    Canary {
        name: "acoustic latency is never invented",
        profile: "v2-directed-current",
        scenario: "quick-chat",
        mutate: Some(|r| {
            // A physical-looking attempt with a valid room recording but no annotation.
            r.stratum = Stratum::PhysicalFixture;
            r.evidence = json!({"room_audio": {"sha256": "0".repeat(64), "valid": true}});
        }),
        expect: Expect::AcousticUnmeasured,
    },
];

fn detected(score: &AttemptScore, expect: Expect) -> bool {
    match expect {
        Expect::Pass => {
            score.outcome == Outcome::Passed
                && !score.findings.iter().any(|f| {
                    matches!(
                        f.severity,
                        evaluate::Severity::Failure | evaluate::Severity::Defect
                    )
                })
        }
        Expect::AcousticUnmeasured => {
            !score.acoustic.is_empty()
                && score.acoustic.iter().all(|a| a.unscored_reason.is_some())
                && score
                    .latencies
                    .iter()
                    .all(|l| l.kind != evaluate::MetricKind::Acoustic)
        }
        Expect::Finding(kind) => {
            score.outcome != Outcome::Passed && score.findings.iter().any(|f| f.kind == kind)
        }
    }
}

/// Run every canary against the current evaluator's embedded plan/catalog.
/// The caller's plan and catalog remain in the signature for compatibility,
/// but cannot supply or change self-test profiles, stimuli, or expectations.
pub fn run(_plan: &LoadedPlan, _catalog: &StimulusCatalog) -> Result<Vec<CanaryResult>> {
    let catalog = StimulusCatalog::load()?;
    let plan = LoadedPlan::default_plan(&catalog)?;
    let canonical_plan = CanonicalPlan {
        suite_version: SUITE_VERSION,
        plan_id: plan.plan.id.clone(),
        plan_version: plan.plan.version,
        plan_sha256: plan.sha256.clone(),
        desk_catalog_sha256: catalog.desk_file_sha256.clone(),
        extension_catalog_sha256: catalog.extension_file_sha256.clone(),
        merged_catalog_sha256: catalog.merged_sha256.clone(),
    };
    let mut results = Vec::with_capacity(CANARIES.len());
    for canary in CANARIES {
        let profile = plan.profile(canary.profile)?.clone();
        let scenario = plan.scenario(canary.scenario)?;
        let mut backend =
            FakeBackend::new(canary.profile, profile, plan.plan.fixed_reply.clone(), None);
        let context = AttemptContext {
            attempt_id: format!("canary-{}", canary.scenario),
            run_id: "canary".into(),
            scenario,
            repetition: 0,
            order: 0,
            seed: 1,
            run_directory: PathBuf::new(),
        };
        let mut record = run_attempt(&mut backend, &catalog, &context)?;
        if let Some(mutate) = canary.mutate {
            mutate(&mut record);
        }
        let score = evaluate::score(&record, plan.plan.answer_deadline_ms, None);
        results.push(CanaryResult {
            canonical_plan: canonical_plan.clone(),
            name: canary.name,
            expects: match canary.expect {
                Expect::Pass => "clean pass".into(),
                Expect::Finding(kind) => format!("{kind:?}"),
                Expect::AcousticUnmeasured => "acoustic boundaries unmeasured".into(),
            },
            detected: detected(&score, canary.expect),
            outcome: score.outcome,
            findings: score.findings.iter().map(|f| f.kind).collect(),
        });
    }
    Ok(results)
}

pub fn all_detected(results: &[CanaryResult]) -> bool {
    results.iter().all(|r| r.detected)
}
