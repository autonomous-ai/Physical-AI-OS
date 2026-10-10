//! Reporting regressions use synthetic traces and a real, silent fixture WAV.
//! They test evidence accounting, not listening quality or acoustic latency.
mod common;

use lamp_voice_eval::{
    annotations::{Annotations, AttemptAnnotation, StepAnnotation},
    evaluate::{self, AnswerOutcome, AttemptScore, CheckStatus, FindingKind, Outcome},
    import::{ImportOptions, import_trace},
    plan::{LoadedPlan, Targets},
    record::AttemptRecord,
    report,
    stimulus::StimulusCatalog,
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, fs};

fn finding(kind: &str, severity: &str, step: &str, evidence: &str) -> Value {
    json!({"kind":kind,"severity":severity,"step":step,"turn":null,
        "evidence":evidence,"detail":"synthetic scoring fixture"})
}

/// Deserialize so this regression also runs against the old score schema,
/// which ignores the new optional review field and still counts bad answers.
fn scored(answer: Value, review: Value, findings: Vec<Value>) -> AttemptScore {
    serde_json::from_value(json!({
        "attempt_id":"test","scenario":"quick-chat","cohort":"quick_chat",
        "capability":"conversation","stratum":"imported_trace","repetition":0,
        "outcome":if findings.is_empty() { "passed" } else { "failed" },"reason":null,
        "steps":[{"step":"ask","expect":"answer","status":{
            "check":if findings.is_empty() { "pass" } else { "fail" }
        },"turns":[1],"answer":answer,"answer_review":review,
            "interruption":null,"lost_opening_ms":null}],
        "findings":findings,"latencies":[],"acoustic":[],"admissions":1,
        "unattributed_admissions":0,"playback_ms":2000.0,"ring_checked":false,
        "voices":1,"reproduce":"synthetic fixture only"
    }))
    .unwrap()
}

fn summary(scores: &[&AttemptScore]) -> Value {
    serde_json::to_value(report::summarize(scores)).unwrap()
}

fn ratio(summary: &Value, field: &str, count: usize, of: usize) {
    assert_eq!(summary[field], json!({"count":count,"of":of}), "{field}");
}

#[test]
fn reviewed_irrelevant_or_incomplete_answer_keeps_transport_but_not_completion_credit() {
    for (review, kind) in [
        (
            json!({"complete":true,"relevant":false}),
            "irrelevant_answer",
        ),
        (
            json!({"complete":false,"relevant":true}),
            "incomplete_answer",
        ),
    ] {
        let score = scored(
            json!("complete"),
            review,
            vec![finding(kind, "failure", "ask", "room_annotation")],
        );
        assert_eq!(score.steps[0].answer, Some(AnswerOutcome::Complete));
        let s = summary(&[&score]);
        ratio(&s, "complete_answers", 0, 1);
        ratio(&s, "complete_answers_without_gaps", 0, 1);
        ratio(&s, "complete_playbacks", 1, 1);
        assert_eq!(s["complete_playbacks_with_answer_failure"], 1);
    }
}

#[test]
fn complete_positive_review_is_separate_from_unreviewed_or_partial_review() {
    for (review, reviewed, unreviewed) in [
        (json!({"complete":true,"relevant":true}), 1, 0),
        (Value::Null, 0, 1),
        (json!({"complete":true,"relevant":null}), 0, 1),
        (json!({"complete":null,"relevant":true}), 0, 1),
    ] {
        let score = scored(json!("complete"), review, vec![]);
        let s = summary(&[&score]);
        ratio(&s, "complete_answers", 1, 1);
        ratio(&s, "complete_answers_without_gaps", 1, 1);
        ratio(&s, "complete_playbacks", 1, 1);
        assert_eq!(s["complete_answers_reviewed"], reviewed);
        assert_eq!(s["complete_answers_unreviewed"], unreviewed);
    }
}

#[test]
fn legacy_review_findings_still_exclude_completion_without_new_review_field() {
    for kind in ["irrelevant_answer", "incomplete_answer"] {
        let score = scored(
            json!("complete"),
            Value::Null,
            vec![finding(kind, "failure", "ask", "room_annotation")],
        );
        let mut legacy = serde_json::to_value(score).unwrap();
        legacy["steps"][0]
            .as_object_mut()
            .unwrap()
            .remove("answer_review");
        let score: AttemptScore = serde_json::from_value(legacy).unwrap();
        ratio(&summary(&[&score]), "complete_answers", 0, 1);
    }
}

#[test]
fn gap_accounting_and_review_failures_are_independent() {
    for (relevant, expected) in [(true, 1), (false, 0)] {
        let mut findings = vec![finding("playback_gaps", "defect", "ask", "software_trace")];
        if !relevant {
            findings.push(finding(
                "irrelevant_answer",
                "failure",
                "ask",
                "room_annotation",
            ));
        }
        let score = scored(
            json!("complete_with_gaps"),
            json!({"complete":true,"relevant":relevant}),
            findings,
        );
        let s = summary(&[&score]);
        ratio(&s, "complete_answers", expected, 1);
        ratio(&s, "complete_answers_without_gaps", 0, 1);
        ratio(&s, "complete_playbacks", 1, 1);
        assert_eq!(s["playback_gap_answers"], 1);
    }
}

#[test]
fn positive_review_cannot_repair_missing_truncated_or_late_transport() {
    let scores: Vec<_> = [
        json!({"missing":{"cause":"no audio"}}),
        json!({"truncated":{"cause":"cancelled"}}),
        json!("late"),
        json!("not_admitted"),
    ]
    .into_iter()
    .map(|answer| scored(answer, json!({"complete":true,"relevant":true}), vec![]))
    .collect();
    let s = summary(&scores.iter().collect::<Vec<_>>());
    ratio(&s, "complete_answers", 0, 4);
    ratio(&s, "complete_answers_without_gaps", 0, 4);
    ratio(&s, "complete_playbacks", 0, 4);
    assert_eq!(s["complete_answers_reviewed"], 0);
}

#[test]
fn confirmed_duplicate_and_split_answers_are_not_successful_completion() {
    for kind in ["duplicate_answer", "turn_split"] {
        let score = scored(
            json!("complete"),
            json!({"complete":true,"relevant":true}),
            vec![finding(kind, "failure", "ask", "declared_attribution")],
        );
        let s = summary(&[&score]);
        ratio(&s, "complete_answers", 0, 1);
        ratio(&s, "complete_answers_without_gaps", 0, 1);
        ratio(&s, "complete_playbacks", 1, 1);
        assert_eq!(s["complete_playbacks_with_answer_failure"], 1);
    }
}

#[test]
fn unrelated_ring_fault_and_unconfirmed_or_other_step_findings_do_not_change_answer_credit() {
    let score = scored(
        json!("complete"),
        json!({"complete":true,"relevant":true}),
        vec![
            finding("ring_mismatch", "failure", "ask", "software_trace"),
            finding("duplicate_answer", "hypothesis", "ask", "software_trace"),
            finding(
                "irrelevant_answer",
                "failure",
                "another-step",
                "room_annotation",
            ),
            finding(
                "incomplete_answer",
                "hypothesis",
                "ask",
                "provider_transcript",
            ),
            finding(
                "turn_split",
                "failure",
                "another-step",
                "declared_attribution",
            ),
        ],
    );
    assert_eq!(score.outcome, Outcome::Failed);
    assert_eq!(score.steps[0].status, CheckStatus::Fail);
    let s = summary(&[&score]);
    ratio(&s, "complete_answers", 1, 1);
    ratio(&s, "complete_answers_without_gaps", 1, 1);
    assert_eq!(s["complete_answers_reviewed"], 1);
    assert_eq!(s["complete_playbacks_with_answer_failure"], 0);
}

#[test]
fn failed_answers_stay_in_the_denominator_and_existing_exclusions_stay_explicit() {
    let mut scores = vec![
        scored(json!("complete"), Value::Null, vec![]),
        scored(
            json!("complete"),
            Value::Null,
            vec![finding(
                "irrelevant_answer",
                "failure",
                "ask",
                "room_annotation",
            )],
        ),
        scored(json!("complete_with_gaps"), Value::Null, vec![]),
        scored(json!({"missing":{"cause":"timeout"}}), Value::Null, vec![]),
        scored(
            json!({"truncated":{"cause":"cancelled"}}),
            Value::Null,
            vec![],
        ),
        scored(json!("yielded_as_planned"), Value::Null, vec![]),
        scored(
            json!({"unsupported":{"reason":"fixture"}}),
            Value::Null,
            vec![],
        ),
        scored(json!("complete"), Value::Null, vec![]),
        scored(Value::Null, Value::Null, vec![]),
    ];
    scores[7].outcome = Outcome::Invalid;
    scores[8].steps[0].status = CheckStatus::Withheld {
        reason: "not run".into(),
    };
    let s = summary(&scores.iter().collect::<Vec<_>>());
    ratio(&s, "complete_answers", 2, 5);
    ratio(&s, "complete_answers_without_gaps", 1, 5);
    ratio(&s, "complete_playbacks", 3, 5);
    assert_eq!(s["attempts"], 9);
    assert_eq!(s["invalid_excluded"], 1);
    assert_eq!(s["answers_yielded_as_planned"], 1);
    assert_eq!(s["answers_unsupported"], 1);
    assert_eq!(s["withheld_steps"], 1);
    assert_eq!(s["complete_answers_unreviewed"], 2);
}

#[test]
fn readable_report_does_not_describe_unreviewed_playback_as_semantic_success() {
    let scores = [scored(json!("complete"), Value::Null, vec![])];
    let report = report::build("test", "plan", "hash", &scores, vec![]);
    let text = report::markdown(
        &report,
        &Targets {
            answer_p50_ms: 2000,
            answer_p95_ms: 4000,
            yield_p95_ms: 150,
        },
    );
    assert!(text.contains("Complete playback, no known answer failure"));
    assert!(text.contains("without a complete content review"));
    assert!(text.contains("Unreviewed playback is not evidence of semantic success"));
}

fn imported(dir: &common::Private, two_replies: bool) -> AttemptRecord {
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate: 16_000,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let wav = dir.join("room.wav");
    let mut writer = hound::WavWriter::create(&wav, spec).unwrap();
    for _ in 0..160_000 {
        writer.write_sample(0_i16).unwrap();
    }
    writer.finalize().unwrap();
    let metadata = dir.join("metadata.json");
    fs::write(
        &metadata,
        json!({"valid":true,"status":"captured_unscored",
        "room_wav_sha256":lamp_acoustic::hash(&fs::read(wav).unwrap())})
        .to_string(),
    )
    .unwrap();
    const START: u64 = 50_000_000;
    let mut lines = vec![
        json!({"kind":"run_start","at_us":START,"provider_kind":"gemini"}),
        json!({"kind":"listening_ready","at_us":START + 1_000_000}),
    ];
    for turn in 1..=if two_replies { 2 } else { 1 } {
        let at = START + turn * 3_000_000;
        let boot = [3_u8; 16];
        let owner = json!({"boot":boot,"turn":turn,"generation":turn + 10});
        lines.extend([
            json!({"kind":"input_admitted","owner":owner,"turn":turn,"at_us":at,
                "prefix_first_host_read_us":at-360_000}),
            json!({"kind":"local_endpoint","turn":turn,"at_us":at+500_000}),
            json!({"kind":"speaker_first_write","owner":owner,"turn":turn,"at_us":at+1_000_000}),
            json!({"kind":"speech_final_sample_retired","owner":owner,"turn":turn,"at_us":at+2_000_000}),
            json!({"kind":"turn_finished","turn":turn,"at_us":at+2_000_100,
                "outcome":"audio_written_unscored","playback_gaps":0}),
        ]);
    }
    lines.push(json!({"kind":"run_end","at_us":START+10_000_000,
        "status":"completed_unscored","error":null}));
    let events = dir.join("events.jsonl");
    fs::write(
        &events,
        lines
            .iter()
            .map(Value::to_string)
            .collect::<Vec<_>>()
            .join("\n"),
    )
    .unwrap();
    let catalog = StimulusCatalog::load().unwrap();
    let plan = LoadedPlan::default_plan(&catalog).unwrap();
    let output = dir.join("import");
    lamp_voice_eval::ledger::create_run_directory(&output).unwrap();
    import_trace(
        &plan,
        &catalog,
        &ImportOptions {
            run_id: "answer-review".into(),
            events_path: &events,
            scenario: "quick-chat".into(),
            turn_map: if two_replies {
                vec![("ask".into(), Some(1)), ("ask".into(), Some(2))]
            } else {
                vec![]
            },
            room_metadata: Some(&metadata),
            room_independent: true,
        },
        &output,
    )
    .unwrap()
}

fn annotations(record: &AttemptRecord, complete: bool, relevant: bool) -> Annotations {
    Annotations {
        schema: 1,
        run_id: record.run_id.clone(),
        annotator: "synthetic-test".into(),
        method: "synthetic evidence accounting; no listening claim".into(),
        attempts: BTreeMap::from([(
            record.attempt_id.clone(),
            AttemptAnnotation {
                room_recording_sha256: record.evidence["room_audio"]["sha256"]
                    .as_str()
                    .map(str::to_owned),
                listened: true,
                steps: BTreeMap::from([(
                    "ask".into(),
                    StepAnnotation {
                        answer_complete: Some(complete),
                        answer_relevant: Some(relevant),
                        ..StepAnnotation::default()
                    },
                )]),
                notes: Some("Synthetic test declaration only".into()),
            },
        )]),
    }
}

#[test]
fn evaluator_retains_validated_review_and_transport_as_separate_facts() {
    let dir = common::Private::new("answer-review");
    let record = imported(&dir, false);
    for (complete, relevant, count) in [(true, false, 0), (false, true, 0), (true, true, 1)] {
        let review = annotations(&record, complete, relevant);
        let score = evaluate::score(&record, 15_000, Some(&review));
        let step = score.steps.iter().find(|s| s.step == "ask").unwrap();
        assert_eq!(step.answer, Some(AnswerOutcome::Complete));
        assert_eq!(
            serde_json::to_value(step).unwrap()["answer_review"],
            json!({"complete":complete,"relevant":relevant})
        );
        ratio(&summary(&[&score]), "complete_answers", count, 1);
        ratio(&summary(&[&score]), "complete_playbacks", 1, 1);
    }
}

#[test]
fn evaluator_duplicate_attributed_replies_preserve_transport_and_fail_completion_rate() {
    let dir = common::Private::new("answer-duplicate");
    let record = imported(&dir, true);
    let score = evaluate::score(&record, 15_000, None);
    let step = score.steps.iter().find(|s| s.step == "ask").unwrap();
    assert_eq!(step.turns, [1, 2]);
    assert_eq!(step.answer, Some(AnswerOutcome::Complete));
    assert_eq!(step.status, CheckStatus::Fail);
    for kind in [FindingKind::DuplicateAnswer, FindingKind::TurnSplit] {
        assert!(score.findings.iter().any(|f| f.kind == kind));
    }
    ratio(&summary(&[&score]), "complete_answers", 0, 1);
    ratio(&summary(&[&score]), "complete_playbacks", 1, 1);
}

#[test]
fn unlistened_or_mismatched_recording_review_cannot_disqualify_playback() {
    let dir = common::Private::new("answer-unreviewed");
    let record = imported(&dir, false);
    for not_listened in [true, false] {
        let mut review = annotations(&record, false, false);
        let attempt = review.attempts.get_mut(&record.attempt_id).unwrap();
        if not_listened {
            attempt.listened = false;
        } else {
            attempt.room_recording_sha256 = Some("0".repeat(64));
        }
        let score = evaluate::score(&record, 15_000, Some(&review));
        let s = summary(&[&score]);
        ratio(&s, "complete_answers", 1, 1);
        assert_eq!(s["complete_answers_unreviewed"], 1);
        assert!(!score.findings.iter().any(|f| matches!(
            f.kind,
            FindingKind::IrrelevantAnswer | FindingKind::IncompleteAnswer
        )));
    }
}
