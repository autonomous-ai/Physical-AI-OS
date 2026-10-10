//! Synthetic trace regression: playback retirement is not whole-turn completion.
mod common;
use lamp_voice_eval::{
    evaluate::{self, AnswerOutcome, Outcome},
    import::{ImportOptions, import_trace},
    plan::LoadedPlan,
    record::StimulusSource,
    stimulus::StimulusCatalog,
};
use serde_json::{Value, json};
use std::fs;

fn score(body: Vec<Value>) -> evaluate::AttemptScore {
    score_with_identity(body, true)
}
fn score_with_identity(body: Vec<Value>, identified: bool) -> evaluate::AttemptScore {
    let catalog = StimulusCatalog::load().unwrap();
    let plan = LoadedPlan::default_plan(&catalog).unwrap();
    let dir = common::Private::new("occ");
    let path = dir.join("events.jsonl");
    let mut trace = vec![
        json!({"kind":"run_start","at_us":0,"provider_kind":"gemini"}),
        json!({"kind":"input_admitted","turn":1,"owner":{"generation":2},"at_us":1000000}),
        json!({"kind":"local_endpoint","turn":1,"at_us":2000000}),
        playback("speaker_first_write", 1, 3000000),
        playback("speech_final_sample_retired", 1, 4000000),
    ];
    if !identified {
        for record in &mut trace {
            record.as_object_mut().unwrap().remove("playback_sequence");
        }
    }
    trace.extend(body);
    trace.push(json!({"kind":"run_end","at_us":12000000,"status":"completed_unscored"}));
    fs::write(
        &path,
        trace
            .iter()
            .map(Value::to_string)
            .collect::<Vec<_>>()
            .join("\n"),
    )
    .unwrap();
    let run = dir.join("run");
    lamp_voice_eval::ledger::create_run_directory(&run).unwrap();
    let record = import_trace(
        &plan,
        &catalog,
        &ImportOptions {
            run_id: "occ".into(),
            events_path: &path,
            scenario: "quick-chat".into(),
            turn_map: vec![("ask".into(), Some(1))],
            room_metadata: None,
            room_independent: false,
            source: StimulusSource::Unknown,
        },
        &run,
    )
    .unwrap();
    evaluate::score(&record, 15000, None)
}
fn playback(kind: &str, sequence: u64, at: u64) -> Value {
    json!({"kind":kind,"turn":1,"playback_sequence":sequence,"at_us":at})
}
fn cancelled(at: u64) -> Value {
    json!({"kind":"turn_finished","turn":1,"owner":{"generation":2},"at_us":at,"outcome":"provider_interrupted","playback_gaps":0})
}
#[test]
fn two_complete_occurrences_sum_exposure_without_counting_the_gap() {
    let result = score(vec![
        playback("speaker_first_write", 2, 6000000),
        playback("speech_final_sample_retired", 2, 8000000),
        json!({"kind":"turn_finished","turn":1,"generation":2,"at_us":8100000,"outcome":"audio_written_unscored","playback_gaps":0}),
    ]);
    assert_eq!(
        result.steps[0].answer,
        Some(AnswerOutcome::Complete),
        "{:?}",
        result.findings
    );
    assert_eq!(result.playback_ms, 3000.0);
}
#[test]
fn cancellation_during_second_generation_is_not_a_complete_answer() {
    let result = score(vec![
        playback("speaker_first_write", 2, 6000000),
        cancelled(7000000),
    ]);
    assert!(
        matches!(
            result.steps[0].answer,
            Some(AnswerOutcome::Truncated { .. })
        ),
        "{:?}",
        result.steps
    );
    assert_ne!(result.outcome, Outcome::Passed);
    assert_eq!(result.playback_ms, 2000.0);
}
#[test]
fn cancellation_between_generations_is_not_whole_turn_completion() {
    let result = score(vec![cancelled(5000000)]);
    assert!(
        matches!(
            result.steps[0].answer,
            Some(AnswerOutcome::Truncated { .. })
        ),
        "{:?}",
        result.steps
    );
    assert_ne!(result.outcome, Outcome::Passed);
    assert_eq!(result.playback_ms, 1000.0);
}

fn completed(at: u64) -> Value {
    json!({"kind":"turn_finished","turn":1,"generation":2,"at_us":at,"outcome":"audio_written_unscored","playback_gaps":0})
}
#[test]
fn stale_first_retirement_during_second_is_invalid_and_does_not_shorten_exposure() {
    let result = score(vec![
        playback("speaker_first_write", 2, 6000000),
        playback("speech_final_sample_retired", 1, 6500000),
        cancelled(7000000),
    ]);
    assert_eq!(result.outcome, Outcome::Invalid);
    assert_eq!(result.playback_ms, 2000.0);
    assert!(!matches!(
        result.steps[0].answer,
        Some(AnswerOutcome::Complete)
    ));
}
#[test]
fn missing_second_identity_duplicate_retirement_and_early_completion_cannot_pass() {
    for body in [
        vec![
            json!({"kind":"speaker_first_write","turn":1,"at_us":6000000}),
            json!({"kind":"speech_final_sample_retired","turn":1,"at_us":8000000}),
            completed(8100000),
        ],
        vec![
            playback("speech_final_sample_retired", 1, 4500000),
            completed(5100000),
        ],
        vec![
            playback("speaker_first_write", 2, 6000000),
            completed(7000000),
        ],
    ] {
        let result = score(body);
        assert_eq!(result.outcome, Outcome::Invalid, "{:?}", result.findings);
        assert!(!matches!(
            result.steps[0].answer,
            Some(AnswerOutcome::Complete)
        ));
    }
}
#[test]
fn every_occurrence_is_used_for_ring_state_and_latency_uses_the_first_word() {
    let body = vec![
        json!({"kind":"ring_requested","turn":1,"phase":"waiting","at_us":5000000}),
        playback("speaker_first_write", 2, 6000000),
        json!({"kind":"ring_requested","turn":1,"phase":"speaking","at_us":7000000}),
        playback("speech_final_sample_retired", 2, 8000000),
        completed(8100000),
    ];
    let result = score(body);
    assert!(
        !result
            .findings
            .iter()
            .any(|f| f.kind == evaluate::FindingKind::RingMismatch),
        "{:?}",
        result.findings
    );
    let values: Vec<_> = result
        .latencies
        .iter()
        .filter(|l| l.metric == evaluate::Metric::EndpointToFirstWrite)
        .map(|l| l.value_ms)
        .collect();
    assert_eq!(values, vec![1000.0]);
    let result = score(vec![
        json!({"kind":"ring_requested","turn":1,"phase":"speaking","at_us":5000000}),
        playback("speaker_first_write", 2, 6000000),
        playback("speech_final_sample_retired", 2, 8000000),
        completed(8100000),
    ]);
    assert!(
        result
            .findings
            .iter()
            .any(|f| f.kind == evaluate::FindingKind::RingMismatch)
    );
}
#[test]
fn completion_missing_gap_count_is_retained_invalid_not_normalized_to_success() {
    for field in ["playback_gaps", "outcome"] {
        let mut completion = completed(5000000);
        completion.as_object_mut().unwrap().remove(field);
        let result = score(vec![completion]);
        assert_eq!(result.outcome, Outcome::Invalid);
    }
}
#[test]
fn historical_terminal_without_generation_is_readable() {
    let mut completion = completed(5000000);
    completion.as_object_mut().unwrap().remove("generation");
    let result = score_with_identity(vec![completion], false);
    assert_eq!(result.steps[0].answer, Some(AnswerOutcome::Complete));
}

#[test]
fn completion_relabelled_to_another_generation_or_with_contradictory_gaps_is_invalid() {
    for (field, value) in [
        ("generation", json!(3)),
        ("generation", json!(0)),
        ("playback_gaps", json!(1)),
    ] {
        let mut completion = completed(5000000);
        completion[field] = value;
        let result = score(vec![completion]);
        assert_eq!(result.outcome, Outcome::Invalid, "{field}");
    }
}

#[test]
fn recognized_lifecycle_records_cannot_disappear_when_identity_or_time_is_missing() {
    let samples = [
        playback("speaker_first_write", 2, 6_000_000),
        playback("speech_final_sample_retired", 2, 8_000_000),
        completed(8_100_000),
        cancelled(8_100_000),
    ];
    for sample in samples {
        for field in ["at_us", "turn"] {
            let mut broken = sample.clone();
            broken.as_object_mut().unwrap().remove(field);
            // A malformed ownerless record must not be assigned to the known turn.
            if field == "turn" {
                broken.as_object_mut().unwrap().remove("owner");
            }
            let result = score(vec![broken.clone(), completed(9_000_000)]);
            assert_eq!(result.outcome, Outcome::Invalid, "silently lost {broken}");
            let summary = lamp_voice_eval::report::summarize(&[&result]);
            assert_eq!(summary.invalid_excluded, 1);
            assert_eq!(summary.complete_answers.count, 0);
            assert_eq!(summary.complete_playbacks.count, 0);
            assert!(summary.latencies.is_empty());
        }
    }
}
