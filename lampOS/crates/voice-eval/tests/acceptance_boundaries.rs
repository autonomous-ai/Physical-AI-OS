//! Synthetic records exercise evidence boundaries, not physical speech quality.
mod common;

use lamp_voice_eval::{
    evaluate::{self, CheckStatus, FindingKind, Outcome},
    events::EventKind,
    fake::FakeBackend,
    plan::LoadedPlan,
    record::{AttemptRecord, Attribution, StimulusSource, Stratum},
    runner::{SuiteOptions, run_suite},
    stimulus::StimulusCatalog,
};

fn simulated(scenario: &str) -> AttemptRecord {
    let dir = common::Private::new("acceptance-boundary");
    let output = dir.join("run");
    lamp_voice_eval::ledger::create_run_directory(&output).unwrap();
    let catalog = StimulusCatalog::load().unwrap();
    let plan = LoadedPlan::default_plan(&catalog).unwrap();
    let mut backend = FakeBackend::new(
        "v2-directed-current",
        plan.profile("v2-directed-current").unwrap().clone(),
        plan.plan.fixed_reply.clone(),
        None,
    );
    let options = SuiteOptions {
        run_id: "boundary".into(),
        scenarios: vec![scenario.into()],
        repetitions: 1,
        seed: 1,
        shuffle: false,
        attempt_seed: None,
        self_test: serde_json::Value::Null,
    };
    run_suite(&plan, &catalog, &mut backend, &options, &output)
        .unwrap()
        .remove(0)
}

fn prompted(mut record: AttemptRecord) -> AttemptRecord {
    record.source = StimulusSource::DirectHuman;
    record.stratum = Stratum::PhysicalGemini;
    record.clock.as_mut().unwrap().path_allowance_us = 2_500_000;
    record
}

fn assert_unscored_prompt(record: &AttemptRecord) {
    let score = evaluate::score(record, 15_000, None);
    assert_eq!(score.outcome, Outcome::Incomplete, "{score:?}");
    assert!(
        score
            .steps
            .iter()
            .all(|step| matches!(step.status, CheckStatus::Unscored { .. }))
    );
    assert!(score.steps.iter().all(|step| step.answer.is_none()));
    assert!(score.latencies.is_empty());
    assert!(score.reason.unwrap().contains("human speech"));
}

#[test]
fn unanswered_human_prompt_cannot_pass_a_silence_test() {
    let mut record = prompted(simulated("two-colleagues"));
    // The prompt was displayed but nobody spoke. Retain only the run envelope.
    record.events.retain(|event| event.turn.is_none());
    assert_unscored_prompt(&record);
}

#[test]
fn delayed_human_response_has_no_synthetic_timing_claim() {
    let mut record = prompted(simulated("quick-chat"));
    for event in &mut record.events {
        if event.turn.is_some() {
            event.at_us = event.at_us.map(|at| at + 5_000_000);
        }
    }
    assert_unscored_prompt(&record);
}

#[test]
fn prompt_window_does_not_prove_an_echo_was_a_human_interruption() {
    // The same runtime events could come from speech or self-echo. A prompt
    // receipt cannot disambiguate them or prove the speaker overlap happened.
    let record = prompted(simulated("topic-change"));
    assert_unscored_prompt(&record);
}

#[test]
fn human_import_with_explicit_turn_attribution_remains_separate_and_usable() {
    let mut record = prompted(simulated("quick-chat"));
    record.stratum = Stratum::ImportedTrace;
    record.attribution = Attribution::Declared {
        turns: vec![("ask".into(), 1)],
        absent: vec![],
    };
    let score = evaluate::score(&record, 15_000, None);
    assert_eq!(score.source, StimulusSource::DirectHuman);
    assert_eq!(score.outcome, Outcome::Passed, "{score:?}");
    assert!(score.acoustic.iter().all(|a| a.unscored_reason.is_some()));
}

#[test]
fn unfinished_background_admission_is_unknown_without_timestamp_overflow() {
    let mut record = simulated("overlapping-speakers");
    record.events.retain(|event| {
        event.turn.is_none() || matches!(event.kind, EventKind::InputAdmitted { .. })
    });
    let score = evaluate::score(&record, 15_000, None);
    assert!(
        score
            .findings
            .iter()
            .any(|f| f.kind == FindingKind::UnterminatedTurn)
    );
    assert!(
        score
            .findings
            .iter()
            .any(|f| { f.kind == FindingKind::EvidenceMissing && f.detail.contains("background") })
    );
    assert!(
        !score.findings.iter().any(|f| {
            f.kind == FindingKind::UnexpectedResponse && f.detail.contains("background")
        })
    );
}
