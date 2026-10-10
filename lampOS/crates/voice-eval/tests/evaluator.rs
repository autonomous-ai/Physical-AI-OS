//! Evaluator rules on imported lamp-live traces, acoustic annotations and plan
//! validation. Traces below follow the documented shape of retained physical
//! trials; they are synthetic reconstructions, not the private recordings.
mod common;

use lamp_voice_eval::{
    annotations::{Annotations, AttemptAnnotation, StepAnnotation, template},
    evaluate::{self, Evidence, FindingKind, Metric, MetricKind, Outcome},
    import::{ImportOptions, import_trace},
    plan::{DEFAULT_PLAN, LoadedPlan},
    record::AttemptRecord,
    stimulus::StimulusCatalog,
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, fs, path::Path};

const T0: u64 = 50_000_000;

fn owner(turn: u64) -> Value {
    let boot = [3_u8; 16];
    json!({"boot": boot, "turn": turn, "generation": turn + 10})
}

fn trace(provider: &str, body: &[Value]) -> String {
    let mut lines = vec![
        json!({"kind":"run_start","at_us":T0,"provider_kind":provider,"acoustic_score":null}),
        json!({"kind":"listening_ready","at_us":T0 + 2_000_000}),
    ];
    lines.extend_from_slice(body);
    lines.push(json!({"kind":"run_end","at_us":T0 + 40_000_000,"status":"completed_unscored","error":null}));
    lines
        .iter()
        .map(Value::to_string)
        .collect::<Vec<_>>()
        .join("\n")
}

fn admit(turn: u64, at: u64) -> Value {
    json!({"kind":"input_admitted","owner":owner(turn),"turn":turn,"at_us":at,"prefix_first_host_read_us":at - 360_000})
}
fn cancel(turn: u64, at: u64, reason: &str) -> Value {
    json!({"kind":"turn_finished","owner":owner(turn),"turn":turn,"at_us":at,"outcome":reason,"provider_audio_seen":true,"playback_gaps":0})
}

fn import(
    dir: &Path,
    name: &str,
    scenario: &str,
    text: &str,
    room: Option<Value>,
) -> AttemptRecord {
    let catalog = StimulusCatalog::load().unwrap();
    let plan = LoadedPlan::default_plan(&catalog).unwrap();
    let events = dir.join(format!("{name}.jsonl"));
    fs::write(&events, text).unwrap();
    let room_path = room.map(|value| {
        let path = dir.join(format!("{name}-room.json"));
        fs::write(&path, value.to_string()).unwrap();
        path
    });
    let out = dir.join(name);
    lamp_voice_eval::ledger::create_run_directory(&out).unwrap();
    import_trace(
        &plan,
        &catalog,
        &ImportOptions {
            run_id: name.into(),
            events_path: &events,
            scenario: scenario.into(),
            turn_map: Vec::new(),
            room_metadata: room_path.as_deref(),
            room_independent: false,
            source: lamp_voice_eval::record::StimulusSource::Unknown,
        },
        &out,
    )
    .unwrap()
}

fn h4_trace() -> String {
    let first_write = T0 + 6_000_000;
    trace(
        "one_cached_reply",
        &[
            admit(1, T0 + 3_100_000),
            json!({"kind":"local_endpoint","turn":1,"at_us":T0 + 5_600_000,"last_block_host_read_us":T0 + 5_599_000,"includes_silence_wait_ms":600}),
            json!({"kind":"provider_first_audio","turn":1,"at_us":T0 + 5_990_000}),
            json!({"kind":"speaker_first_write","turn":1,"owner":owner(1),"at_us":first_write}),
            // Second admission 846.505 ms after the first accepted reply write.
            admit(2, first_write + 846_505),
            cancel(1, first_write + 846_520, "user_interrupted"),
            json!({"kind":"cancelled_tail_retired","owner":owner(1),"turn":1,"at_us":first_write + 860_694}),
            json!({"kind":"local_endpoint","turn":2,"at_us":first_write + 2_000_000}),
            json!({"kind":"provider_turn_complete","turn":2,"idle":true,"at_us":first_write + 2_010_000}),
            json!({"kind":"turn_finished","turn":2,"at_us":first_write + 2_020_000,"outcome":"no_audio_answer","playback_gaps":0}),
        ],
    )
}

#[test]
fn imported_h4_trace_is_a_false_interruption_by_unattributed_input() {
    let dir = common::Private::new("h4");
    let record = import(&dir.path, "h4", "fixed-reply-echo-only", &h4_trace(), None);
    let score = evaluate::score(&record, 15_000, None);
    assert_eq!(score.outcome, Outcome::Failed);
    let false_interruptions: Vec<_> = score
        .findings
        .iter()
        .filter(|f| f.kind == FindingKind::FalseInterruption)
        .collect();
    assert_eq!(false_interruptions.len(), 1);
    assert_eq!(
        false_interruptions[0].evidence,
        Evidence::DeclaredAttribution
    );
    assert!(
        false_interruptions[0]
            .detail
            .contains("matches no stimulus")
    );
    assert_eq!(score.unattributed_admissions, 1);
    assert_eq!(
        score.steps[1].status,
        evaluate::CheckStatus::Fail,
        "observation window"
    );
    // Software boundaries only; no acoustic number from a trace.
    assert!(
        score
            .latencies
            .iter()
            .all(|l| l.kind != MetricKind::Acoustic)
    );
    assert!(score.acoustic.iter().all(|a| a.unscored_reason.is_some()));
    assert!(
        score
            .latencies
            .iter()
            .any(|l| l.metric == Metric::EndpointToFirstWrite && l.kind == MetricKind::Software)
    );
}

#[test]
fn imported_zejfwxq7_like_chain_counts_every_cancelled_answer() {
    let dir = common::Private::new("zej");
    let mut body = vec![
        admit(1, T0 + 3_000_000),
        json!({"kind":"local_endpoint","turn":1,"at_us":T0 + 5_000_000}),
    ];
    let mut at = T0 + 6_300_000;
    for turn in 1..=3 {
        body.push(json!({"kind":"speaker_first_write","turn":turn,"owner":owner(turn),"at_us":at}));
        body.push(admit(turn + 1, at + 600_000));
        body.push(cancel(turn, at + 600_010, "user_interrupted"));
        body.push(json!({"kind":"local_endpoint","turn":turn + 1,"at_us":at + 1_200_000}));
        at += 2_600_000;
    }
    body.push(json!({"kind":"speaker_first_write","turn":4,"owner":owner(4),"at_us":at}));
    body.push(json!({"kind":"speech_final_sample_retired","turn":4,"owner":owner(4),"at_us":at + 900_000}));
    body.push(json!({"kind":"turn_finished","turn":4,"at_us":at + 900_100,"outcome":"audio_written_unscored","playback_gaps":0}));
    let record = import(
        &dir.path,
        "zej",
        "quick-chat",
        &trace("gemini", &body),
        None,
    );
    let score = evaluate::score(&record, 15_000, None);
    assert_eq!(
        score
            .findings
            .iter()
            .filter(|f| f.kind == FindingKind::FalseInterruption)
            .count(),
        3
    );
    assert_eq!(score.admissions, 4);
    assert!(matches!(
        score.steps[0].answer,
        Some(evaluate::AnswerOutcome::Truncated { .. })
    ));
}

#[test]
fn a_trace_without_run_records_is_unscored_not_passed() {
    let dir = common::Private::new("empty");
    let catalog = StimulusCatalog::load().unwrap();
    let plan = LoadedPlan::default_plan(&catalog).unwrap();
    let text = trace("gemini", &[]);
    let mut record = import(&dir.path, "e", "quick-chat", &text, None);
    record.events.retain(|e| {
        !matches!(
            e.kind,
            lamp_voice_eval::events::EventKind::RunStart { .. }
                | lamp_voice_eval::events::EventKind::RunEnd { .. }
        )
    });
    let score = evaluate::score(&record, plan.plan.answer_deadline_ms, None);
    assert_eq!(score.outcome, Outcome::Incomplete);
    assert!(
        score
            .findings
            .iter()
            .any(|f| f.kind == FindingKind::EvidenceMissing)
    );
}

#[test]
fn provider_mismatch_and_unknown_turns_are_rejected_on_import() {
    let dir = common::Private::new("bad");
    let catalog = StimulusCatalog::load().unwrap();
    let plan = LoadedPlan::default_plan(&catalog).unwrap();
    let events = dir.join("x.jsonl");
    fs::write(&events, h4_trace()).unwrap();
    for (scenario, turn_map) in [
        ("quick-chat", vec![]),
        ("fixed-reply-echo-only", vec![("greet".to_owned(), Some(9))]),
    ] {
        let out = dir.join(&format!("o-{scenario}"));
        lamp_voice_eval::ledger::create_run_directory(&out).unwrap();
        assert!(
            import_trace(
                &plan,
                &catalog,
                &ImportOptions {
                    run_id: "x".into(),
                    events_path: &events,
                    scenario: scenario.into(),
                    turn_map,
                    room_metadata: None,
                    room_independent: false,
                    source: lamp_voice_eval::record::StimulusSource::Unknown,
                },
                &out
            )
            .is_err(),
            "{scenario}"
        );
    }
}

fn room(sha: &str) -> Value {
    json!({"valid": true, "status": "captured_unscored", "room_wav_sha256": sha, "start_requested_host_ns": 1_000_000_000_u64})
}

fn annotation(listened: bool, sha: &str, step: StepAnnotation) -> Annotations {
    let mut steps = BTreeMap::new();
    steps.insert("greet".to_owned(), step);
    let mut attempts = BTreeMap::new();
    attempts.insert(
        "ac-import-fixed-reply-echo-only".to_owned(),
        AttemptAnnotation {
            room_recording_sha256: Some(sha.into()),
            listened,
            steps,
            notes: None,
        },
    );
    Annotations {
        schema: 1,
        run_id: "ac".into(),
        annotator: "test".into(),
        method: "test".into(),
        attempts,
    }
}

#[test]
fn acoustic_latency_requires_a_listened_annotation_of_the_same_recording() {
    let dir = common::Private::new("ac");
    let sha = "a".repeat(64);
    let record = import(
        &dir.path,
        "ac",
        "fixed-reply-echo-only",
        &h4_trace(),
        Some(room(&sha)),
    );
    let boundaries = StepAnnotation {
        user_speech_end_s: Some(5.53),
        user_speech_end_uncertainty_ms: Some(30.0),
        first_substantive_answer_word_s: Some(7.51),
        answer_onset_uncertainty_ms: Some(20.0),
        ..StepAnnotation::default()
    };
    let scored = evaluate::score(
        &record,
        15_000,
        Some(&annotation(true, &sha, boundaries.clone())),
    );
    let acoustic: Vec<_> = scored
        .latencies
        .iter()
        .filter(|l| l.kind == MetricKind::Acoustic)
        .collect();
    assert_eq!(acoustic.len(), 1);
    assert_eq!(acoustic[0].metric, Metric::AcousticSpeechEndToAnswer);
    assert!((acoustic[0].value_ms - 1980.0).abs() < 1e-6);
    assert_eq!(scored.acoustic[0].uncertainty_ms, Some(50.0));
    for (annotations, reason) in [
        (annotation(false, &sha, boundaries.clone()), "not listened"),
        (
            annotation(true, &"b".repeat(64), boundaries.clone()),
            "different room recording",
        ),
        (
            annotation(
                true,
                &sha,
                StepAnnotation {
                    first_substantive_answer_word_s: None,
                    ..boundaries.clone()
                },
            ),
            "boundary is missing",
        ),
        (
            Annotations {
                schema: 1,
                ..Annotations::default()
            },
            "no annotation",
        ),
    ] {
        let score = evaluate::score(&record, 15_000, Some(&annotations));
        assert!(
            score
                .latencies
                .iter()
                .all(|l| l.kind != MetricKind::Acoustic),
            "{reason}"
        );
        assert!(
            score.acoustic[0]
                .unscored_reason
                .as_deref()
                .unwrap()
                .contains(reason),
            "{reason}"
        );
    }
    let invalid_room = import(
        &dir.path,
        "ac2",
        "fixed-reply-echo-only",
        &h4_trace(),
        Some(json!({"valid": false, "room_wav_sha256": sha})),
    );
    let score = evaluate::score(
        &invalid_room,
        15_000,
        Some(&annotation(true, &sha, boundaries)),
    );
    assert!(
        score.acoustic[0]
            .unscored_reason
            .as_deref()
            .unwrap()
            .contains("integrity")
    );
    // The template gives a software locating hint but no boundary values.
    let t = template("ac", std::slice::from_ref(&record));
    let step = &t.attempts["ac-import-fixed-reply-echo-only"].steps["greet"];
    assert!(step.user_speech_end_s.is_none() && step.first_substantive_answer_word_s.is_none());
    assert!(!t.attempts["ac-import-fixed-reply-echo-only"].listened);
}

#[test]
fn annotation_files_reject_negative_or_nonfinite_times() {
    let bad = json!({"schema":1,"run_id":"x","annotator":"a","method":"m","attempts":{"a":{"room_recording_sha256":null,"listened":true,
        "steps":{"s":{"user_speech_end_s":-1.0,"user_speech_end_uncertainty_ms":null,"first_substantive_answer_word_s":null,
        "answer_onset_uncertainty_ms":null,"interruption_onset_s":null,"lamp_silent_s":null,"silence_uncertainty_ms":null,
        "answer_relevant":null,"answer_complete":null,"spoken_failure_notice":null}}}}});
    assert!(Annotations::parse(bad.to_string().as_bytes()).is_err());
}

#[test]
fn the_shipped_plan_is_valid_and_mutations_are_rejected() {
    let catalog = StimulusCatalog::load().unwrap();
    let plan = LoadedPlan::default_plan(&catalog).unwrap();
    assert!(plan.plan.scenarios.len() >= 30);
    for cohort in [
        "quick_chat",
        "follow_up",
        "short_answer",
        "long_answer",
        "topic_change",
        "hesitation",
        "correction",
        "quiet_speech",
        "rapid_speech",
        "acknowledgment",
        "two_people",
        "computer_audio",
        "noise",
        "echo_only",
        "provider_delay",
        "provider_failure",
        "provider_disconnect",
    ] {
        assert!(
            DEFAULT_PLAN.contains(&format!("\"cohort\": \"{cohort}\"")),
            "{cohort} is covered"
        );
    }
    let mutations = [
        // An overlap expectation without the speaking precondition.
        (
            r#""delay_ms": 1500, "deadline_ms": 15000, "require_lamp_speaking": true}, "expect": "no_interrupt""#,
            r#""delay_ms": 1500, "deadline_ms": 15000}, "expect": "no_interrupt""#,
        ),
        // A trigger that disagrees with the scene's declared trigger delay.
        (
            r#""scene": "topic-change", "trigger": {"after": "speaker_first_write", "delay_ms": 1200"#,
            r#""scene": "topic-change", "trigger": {"after": "speaker_first_write", "delay_ms": 1300"#,
        ),
        // Fault injection cannot be claimed as a physical scenario.
        (
            r#""provider": "gemini", "physical": false, "session_seconds": 45, "observe_ms": 5000,
      "steps": [
        {"id": "ask", "scene": "quick-chat", "trigger": {"after": "listening_ready", "delay_ms": 500, "deadline_ms": 30000}, "expect": "answer", "reference": "A reply that arrives late but complete."}"#,
            r#""provider": "gemini", "physical": true, "session_seconds": 45, "observe_ms": 5000,
      "steps": [
        {"id": "ask", "scene": "quick-chat", "trigger": {"after": "listening_ready", "delay_ms": 500, "deadline_ms": 30000}, "expect": "answer", "reference": "A reply that arrives late but complete."}"#,
        ),
        // Unbounded deadline.
        (
            r#""deadline_ms": 30000}, "expect": "answer", "reference": "Four."}"#,
            r#""deadline_ms": 300000}, "expect": "answer", "reference": "Four."}"#,
        ),
        // An unknown scene.
        (r#""scene": "quick-fact""#, r#""scene": "no-such-scene""#),
    ];
    for (from, to) in mutations {
        assert!(
            DEFAULT_PLAN.contains(from),
            "mutation anchor missing: {from}"
        );
        let mutated = DEFAULT_PLAN.replacen(from, to, 1);
        assert!(
            LoadedPlan::parse(&mutated, &catalog).is_err(),
            "accepted: {to}"
        );
    }
}

fn import_with(
    dir: &Path,
    name: &str,
    scenario: &str,
    text: &str,
    turns: Vec<(String, Option<u64>)>,
) -> lamp_voice_eval::Result<AttemptRecord> {
    let catalog = StimulusCatalog::load().unwrap();
    let plan = LoadedPlan::default_plan(&catalog).unwrap();
    let events = dir.join(format!("{name}.jsonl"));
    fs::write(&events, text).unwrap();
    let out = dir.join(name);
    lamp_voice_eval::ledger::create_run_directory(&out).unwrap();
    import_trace(
        &plan,
        &catalog,
        &ImportOptions {
            run_id: name.into(),
            events_path: &events,
            scenario: scenario.into(),
            turn_map: turns,
            room_metadata: None,
            room_independent: false,
            source: lamp_voice_eval::record::StimulusSource::Unknown,
        },
        &out,
    )
}

fn short_yes_trace() -> String {
    let retired = T0 + 8_000_000;
    trace(
        "gemini",
        &[
            admit(1, T0 + 3_000_000),
            json!({"kind":"local_endpoint","turn":1,"at_us":T0 + 5_000_000}),
            json!({"kind":"speaker_first_write","turn":1,"owner":owner(1),"at_us":T0 + 6_000_000}),
            json!({"kind":"speech_final_sample_retired","turn":1,"owner":owner(1),"at_us":retired}),
            // The provider was not idle yet, so the next input revokes a fully played reply.
            admit(2, retired + 900_000),
            cancel(2 - 1, retired + 900_010, "user_interrupted"),
            json!({"kind":"local_endpoint","turn":2,"at_us":retired + 1_800_000}),
            json!({"kind":"speaker_first_write","turn":2,"owner":owner(2),"at_us":retired + 3_000_000}),
            json!({"kind":"speech_final_sample_retired","turn":2,"owner":owner(2),"at_us":retired + 5_000_000}),
            json!({"kind":"turn_finished","turn":2,"at_us":retired + 5_000_100,"outcome":"audio_written_unscored","playback_gaps":0}),
        ],
    )
}

#[test]
fn a_reply_retired_before_its_revocation_is_complete_not_falsely_interrupted() {
    let dir = common::Private::new("ret");
    let record = import_with(
        &dir.path,
        "r",
        "short-yes",
        &short_yes_trace(),
        vec![("invite".into(), Some(1)), ("yes".into(), Some(2))],
    )
    .unwrap();
    let score = evaluate::score(&record, 15_000, None);
    assert!(
        !score
            .findings
            .iter()
            .any(|f| f.kind == FindingKind::FalseInterruption),
        "{:?}",
        score.findings
    );
    assert_eq!(
        score.steps[0].answer,
        Some(evaluate::AnswerOutcome::Complete)
    );
    assert_eq!(score.outcome, Outcome::Passed, "{:?}", score.findings);
}

#[test]
fn imports_must_declare_every_stimulus_and_undeclared_steps_are_unscored() {
    let dir = common::Private::new("decl");
    assert!(import_with(&dir.path, "a", "short-yes", &short_yes_trace(), vec![]).is_err());
    assert!(
        import_with(
            &dir.path,
            "b",
            "short-yes",
            &short_yes_trace(),
            vec![("invite".into(), Some(1))]
        )
        .is_err()
    );
    let record = import_with(
        &dir.path,
        "c",
        "short-yes",
        &short_yes_trace(),
        vec![("invite".into(), Some(1)), ("yes".into(), None)],
    )
    .unwrap();
    let score = evaluate::score(&record, 15_000, None);
    assert!(
        score
            .findings
            .iter()
            .any(|f| f.kind == FindingKind::MissedInput && f.step.as_deref() == Some("yes"))
    );
    // A record whose declaration is incomplete scores that step as unknown and
    // demotes unattributed findings to hypotheses.
    let mut partial = record.clone();
    partial.attribution = lamp_voice_eval::record::Attribution::Declared {
        turns: vec![("invite".into(), 1)],
        absent: vec![],
    };
    let score = evaluate::score(&partial, 15_000, None);
    assert!(
        matches!(&score.steps[1].status, evaluate::CheckStatus::Unscored { reason } if reason.contains("declared"))
    );
    assert!(
        score
            .findings
            .iter()
            .filter(|f| f.kind == FindingKind::UnexpectedResponse)
            .all(|f| f.severity == evaluate::Severity::Hypothesis)
    );
}

#[test]
fn a_trace_with_run_start_but_no_run_end_is_not_scored() {
    let dir = common::Private::new("part");
    let mut record = import(&dir.path, "p", "fixed-reply-echo-only", &h4_trace(), None);
    record
        .events
        .retain(|e| !matches!(e.kind, lamp_voice_eval::events::EventKind::RunEnd { .. }));
    let score = evaluate::score(&record, 15_000, None);
    assert_eq!(score.outcome, Outcome::Incomplete);
    assert!(
        score
            .steps
            .iter()
            .all(|s| matches!(s.status, evaluate::CheckStatus::Unscored { .. }))
    );
}

#[test]
fn reviewer_judgments_fail_and_negative_intervals_stay_unscored() {
    let dir = common::Private::new("rev");
    let sha = "c".repeat(64);
    let record = import(
        &dir.path,
        "rv",
        "fixed-reply-echo-only",
        &h4_trace(),
        Some(room(&sha)),
    );
    let mut annotations = annotation(
        true,
        &sha,
        StepAnnotation {
            user_speech_end_s: Some(7.0),
            first_substantive_answer_word_s: Some(6.5),
            answer_relevant: Some(false),
            ..StepAnnotation::default()
        },
    );
    let entry = annotations
        .attempts
        .remove("ac-import-fixed-reply-echo-only")
        .unwrap();
    annotations
        .attempts
        .insert(record.attempt_id.clone(), entry);
    let score = evaluate::score(&record, 15_000, Some(&annotations));
    assert!(
        score
            .findings
            .iter()
            .any(|f| f.kind == FindingKind::IrrelevantAnswer
                && f.evidence == Evidence::RoomAnnotation)
    );
    assert!(
        score
            .latencies
            .iter()
            .all(|l| l.kind != MetricKind::Acoustic)
    );
    assert!(
        score.acoustic[0]
            .unscored_reason
            .as_deref()
            .unwrap()
            .contains("negative")
    );
}

#[test]
fn rates_exclude_invalid_attempts_and_unscored_opportunities() {
    let dir = common::Private::new("rate");
    let record = import(&dir.path, "x", "fixed-reply-echo-only", &h4_trace(), None);
    let failed = evaluate::score(&record, 15_000, None);
    let mut invalid = failed.clone();
    invalid.outcome = Outcome::Invalid;
    let mut unscored = failed.clone();
    for step in &mut unscored.steps {
        step.status = evaluate::CheckStatus::Unscored {
            reason: "test".into(),
        };
    }
    let summary = lamp_voice_eval::report::summarize(&[&failed, &invalid, &unscored]);
    assert_eq!(summary.invalid_excluded, 1);
    assert_eq!(
        summary.false_interruption_opportunities.of, 1,
        "only the scored observe window"
    );
    assert_eq!(summary.false_interruption_opportunities.count, 1);
}

#[test]
fn the_evaluator_detects_every_injected_failure_and_passes_clean_controls() {
    let catalog = StimulusCatalog::load().unwrap();
    let plan = LoadedPlan::default_plan(&catalog).unwrap();
    let results = lamp_voice_eval::canary::run(&plan, &catalog).unwrap();
    assert!(results.len() >= 20);
    let missed: Vec<_> = results.iter().filter(|r| !r.detected).collect();
    assert!(missed.is_empty(), "{missed:#?}");
    assert!(results.iter().filter(|r| r.expects == "clean pass").count() >= 3);
}

#[test]
fn loudspeaker_and_direct_human_attempts_are_reported_separately() {
    use lamp_voice_eval::record::StimulusSource;
    let dir = common::Private::new("src");
    let loudspeaker = import(&dir.path, "ls", "fixed-reply-echo-only", &h4_trace(), None);
    let mut human = loudspeaker.clone();
    human.attempt_id.push_str("-human");
    let mut loudspeaker = loudspeaker;
    loudspeaker.source = StimulusSource::LoudspeakerSynthetic;
    human.source = StimulusSource::DirectHuman;
    let scores = vec![
        evaluate::score(&loudspeaker, 15_000, None),
        evaluate::score(&human, 15_000, None),
    ];
    let report = lamp_voice_eval::report::build("r", "p", "s", &scores, Vec::new(), Vec::new());
    let sources: Vec<_> = report.strata.iter().map(|g| g.source).collect();
    assert_eq!(
        sources,
        vec![
            StimulusSource::LoudspeakerSynthetic,
            StimulusSource::DirectHuman
        ]
    );
    assert!(
        report.strata.iter().all(|g| g.summary.attempts == 1),
        "never pooled"
    );
    assert!(report.limitations.iter().any(|l| l.contains("Jieli")));
    let markdown = lamp_voice_eval::report::markdown(
        &report,
        &LoadedPlan::default_plan(&StimulusCatalog::load().unwrap())
            .unwrap()
            .plan
            .targets,
    );
    assert!(
        markdown.contains("UNTRUSTED"),
        "a report without a self-test is marked untrusted"
    );
}
