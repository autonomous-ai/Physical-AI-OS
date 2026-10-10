//! Fake-runtime scenarios: the runner's event triggering and the evaluator's
//! findings against lamp-live's directed turn policy. No audio is produced.
mod common;

use lamp_voice_eval::{
    evaluate::{self, AnswerOutcome, CheckStatus, FindingKind, Metric, Outcome},
    events::EventKind,
    fake::FakeBackend,
    ledger::read_rows,
    plan::{LoadedPlan, TriggerEvent},
    record::{AttemptRecord, StepStatus, Stratum},
    runner::{SuiteOptions, run_suite},
    stimulus::{AssetIndex, StimulusCatalog},
};

struct Setup {
    catalog: StimulusCatalog,
    plan: LoadedPlan,
}
fn setup() -> Setup {
    let catalog = StimulusCatalog::load().unwrap();
    let plan = LoadedPlan::default_plan(&catalog).unwrap();
    Setup { catalog, plan }
}

fn run(
    setup: &Setup,
    profile: &str,
    scenarios: &[&str],
    assets: Option<&AssetIndex>,
) -> Vec<AttemptRecord> {
    let dir = common::Private::new("fake");
    let run = dir.join("run");
    lamp_voice_eval::ledger::create_run_directory(&run).unwrap();
    let mut backend = FakeBackend::new(
        profile,
        setup.plan.profile(profile).unwrap().clone(),
        setup.plan.plan.fixed_reply.clone(),
        assets,
    );
    let options = SuiteOptions {
        run_id: "t".into(),
        scenarios: scenarios.iter().map(|s| s.to_string()).collect(),
        repetitions: 1,
        seed: 7,
        shuffle: false,
        attempt_seed: None,
    };
    let records = run_suite(&setup.plan, &setup.catalog, &mut backend, &options, &run).unwrap();
    // Every attempt is retained: planned and finished rows for each.
    let (rows, truncated) = read_rows(&run).unwrap();
    assert!(!truncated);
    let planned = rows
        .iter()
        .filter(|r| r["row"] == "attempt_planned")
        .count();
    let finished = rows
        .iter()
        .filter(|r| r["row"] == "attempt_finished")
        .count();
    assert_eq!((planned, finished), (scenarios.len(), scenarios.len()));
    records
}

fn score(setup: &Setup, record: &AttemptRecord) -> evaluate::AttemptScore {
    evaluate::score(record, setup.plan.plan.answer_deadline_ms, None)
}

fn kinds(score: &evaluate::AttemptScore) -> Vec<FindingKind> {
    score.findings.iter().map(|f| f.kind).collect()
}

#[test]
fn quick_chat_is_one_complete_answer_with_simulated_latency_only() {
    let s = setup();
    let records = run(&s, "v2-directed-current", &["quick-chat"], None);
    let score = score(&s, &records[0]);
    assert_eq!(score.outcome, Outcome::Passed, "{:?}", score.findings);
    assert_eq!(score.stratum, Stratum::FakeTiming);
    assert_eq!(score.steps[0].answer, Some(AnswerOutcome::Complete));
    assert!(
        score
            .latencies
            .iter()
            .all(|l| l.kind != evaluate::MetricKind::Acoustic)
    );
    assert!(
        score.acoustic.is_empty(),
        "fake attempts have no room audio to score"
    );
}

#[test]
fn follow_ups_are_triggered_by_the_end_of_each_answer() {
    let s = setup();
    let records = run(&s, "v2-directed-current", &["follow-up-chain"], None);
    let record = &records[0];
    let retired: Vec<u64> = record
        .events
        .iter()
        .filter(|e| matches!(e.kind, EventKind::SpeechRetired))
        .filter_map(|e| e.at_us)
        .collect();
    assert_eq!(retired.len(), 3);
    for (index, step) in record.steps.iter().enumerate().skip(1) {
        let trigger = step.trigger.as_ref().unwrap();
        assert_eq!(trigger.event, TriggerEvent::SpeechRetired);
        assert_eq!(trigger.event_at_us, Some(retired[index - 1]));
        let delay = s.plan.scenario("follow-up-chain").unwrap().steps[index]
            .trigger
            .delay_ms;
        assert_eq!(
            step.started_us,
            Some(retired[index - 1] + u64::from(delay) * 1000)
        );
    }
    assert_eq!(score(&s, record).outcome, Outcome::Passed);
}

#[test]
fn acknowledgments_and_asides_falsely_interrupt_the_current_policy() {
    let s = setup();
    for scenario in [
        "listener-acknowledgment",
        "ack-yeah",
        "ack-right",
        "colleague-aside",
        "fixed-reply-acknowledgment",
    ] {
        let records = run(&s, "v2-directed-current", &[scenario], None);
        let score = score(&s, &records[0]);
        assert_eq!(score.outcome, Outcome::Failed, "{scenario}");
        assert!(
            kinds(&score).contains(&FindingKind::FalseInterruption),
            "{scenario}"
        );
        assert_eq!(score.steps[1].status, CheckStatus::Fail, "{scenario}");
        assert!(
            matches!(
                score.steps[0].answer,
                Some(AnswerOutcome::Incomplete { .. })
            ),
            "{scenario}"
        );
    }
}

#[test]
fn topic_change_yields_as_planned_and_measures_software_yield() {
    let s = setup();
    let records = run(&s, "v2-directed-current", &["topic-change"], None);
    let score = score(&s, &records[0]);
    assert_eq!(score.outcome, Outcome::Passed, "{:?}", score.findings);
    assert_eq!(score.steps[0].answer, Some(AnswerOutcome::YieldedAsPlanned));
    assert_eq!(score.steps[1].answer, Some(AnswerOutcome::Complete));
    assert!(
        score
            .latencies
            .iter()
            .any(|l| l.metric == Metric::AdmissionToCancel)
    );
    assert!(
        score
            .latencies
            .iter()
            .any(|l| l.metric == Metric::CancelToTailRetired)
    );
    // The interruption started 1.2 s after the first write, on the event.
    let step = &records[0].steps[1];
    let trigger = step.trigger.as_ref().unwrap();
    assert_eq!(step.started_us, Some(trigger.runner_us + 1_200_000));
    assert_eq!(step.bound_turn, Some(1));
}

#[test]
fn hesitation_longer_than_the_endpoint_splits_the_turn() {
    let s = setup();
    let records = run(&s, "v2-directed-current", &["hesitant-sharing"], None);
    let score = score(&s, &records[0]);
    assert!(kinds(&score).contains(&FindingKind::TurnSplit));
    assert_eq!(score.steps[0].turns.len(), 2);
}

#[test]
fn unaddressed_speech_gets_admitted_and_answered_without_an_addressee_gate() {
    let s = setup();
    for scenario in [
        "two-colleagues",
        "other-device",
        "computer-call",
        "background-media",
        "ambiguous-not-invited",
    ] {
        let records = run(&s, "v2-directed-current", &[scenario], None);
        let score = score(&s, &records[0]);
        assert!(
            kinds(&score).contains(&FindingKind::UnexpectedResponse),
            "{scenario}"
        );
        assert_eq!(score.steps[0].status, CheckStatus::Fail, "{scenario}");
        if scenario == "two-colleagues" {
            assert!(
                score.voices > 1,
                "multi-voice scenes are flagged for the spatial limitation"
            );
        }
    }
    let quiet = run(&s, "v2-directed-current", &["noise-only"], None);
    assert_eq!(score(&s, &quiet[0]).outcome, Outcome::Passed);
}

#[test]
fn provider_faults_are_reported_honestly() {
    let s = setup();
    let late = score(
        &s,
        &run(&s, "v2-directed-current", &["provider-late-answer"], None)[0],
    );
    assert!(kinds(&late).contains(&FindingKind::LateAnswer));
    assert_eq!(late.steps[0].answer, Some(AnswerOutcome::Late));
    let slow = score(
        &s,
        &run(
            &s,
            "v2-directed-current",
            &["provider-slow-first-audio"],
            None,
        )[0],
    );
    assert_eq!(slow.outcome, Outcome::Passed);
    let gap = score(
        &s,
        &run(&s, "v2-directed-current", &["provider-supply-gap"], None)[0],
    );
    assert!(kinds(&gap).contains(&FindingKind::PlaybackGaps));
    assert_eq!(gap.steps[0].answer, Some(AnswerOutcome::CompleteWithGaps));
    for scenario in [
        "provider-failure-before-audio",
        "provider-disconnect-mid-reply",
    ] {
        let records = run(&s, "v2-directed-current", &[scenario], None);
        let failed = score(&s, &records[0]);
        let kinds = kinds(&failed);
        assert!(
            kinds.contains(&FindingKind::UnannouncedFailure),
            "{scenario}"
        );
        assert!(
            !kinds.contains(&FindingKind::FabricatedCompletion),
            "{scenario}"
        );
        assert!(
            !kinds.contains(&FindingKind::RuntimeFailure),
            "an expected fault is not double counted"
        );
        assert!(records[0].events.iter().any(
            |e| matches!(&e.kind, EventKind::RunEnd { status: Some(s), .. } if s == "failed")
        ));
    }
    let spurious = score(
        &s,
        &run(
            &s,
            "v2-directed-current",
            &["provider-spurious-interrupt"],
            None,
        )[0],
    );
    assert!(kinds(&spurious).contains(&FindingKind::FalseInterruption));
}

#[test]
fn echo_leak_profile_reproduces_the_h4_false_admission() {
    let s = setup();
    let records = run(&s, "v2-echo-leak-h4", &["fixed-reply-echo-only"], None);
    let record = &records[0];
    let first_write = record
        .events
        .iter()
        .find(|e| matches!(e.kind, EventKind::SpeakerFirstWrite))
        .unwrap()
        .at_us
        .unwrap();
    let second = record
        .events
        .iter()
        .filter(|e| matches!(e.kind, EventKind::InputAdmitted { .. }))
        .nth(1)
        .unwrap()
        .at_us
        .unwrap();
    // Burst starts 846 ms after the first write; six 10 ms blocks admit it.
    let delay_ms = (second - first_write) / 1000;
    assert!((900..=920).contains(&delay_ms), "{delay_ms}");
    let score = score(&s, record);
    assert!(kinds(&score).contains(&FindingKind::FalseInterruption));
    assert_eq!(
        score.steps[1].status,
        CheckStatus::Fail,
        "the observation window fails"
    );
    assert_eq!(score.unattributed_admissions, 1);
    let clean = score_of(&s, "v2-directed-current", "fixed-reply-echo-only");
    assert_eq!(clean.outcome, Outcome::Passed);
}

#[test]
fn echo_loop_profile_reproduces_a_self_sustaining_admission_chain() {
    let s = setup();
    let score = score_of(&s, "v2-echo-loop", "quick-chat");
    assert!(
        score.unattributed_admissions >= 3,
        "{}",
        score.unattributed_admissions
    );
    assert!(
        kinds(&score)
            .iter()
            .filter(|k| **k == FindingKind::FalseInterruption)
            .count()
            >= 3
    );
}

fn score_of(s: &Setup, profile: &str, scenario: &str) -> evaluate::AttemptScore {
    score(s, &run(s, profile, &[scenario], None)[0])
}

#[test]
fn fixed_reply_second_turn_is_unsupported_not_failed() {
    let s = setup();
    // The yield is scored; the second answer cannot be, so the attempt is
    // incomplete rather than passed.
    let score = score_of(&s, "v2-directed-current", "fixed-reply-topic-change");
    assert_eq!(score.outcome, Outcome::Incomplete, "{:?}", score.findings);
    assert_eq!(score.steps[1].interruption, Some(true));
    assert!(
        matches!(&score.steps[1].status, CheckStatus::Unscored { reason } if reason.starts_with("yield scored"))
    );
    assert_eq!(score.steps[0].answer, Some(AnswerOutcome::YieldedAsPlanned));
    assert!(matches!(
        score.steps[1].answer,
        Some(AnswerOutcome::Unsupported { .. })
    ));
}

#[test]
fn same_seed_reproduces_identical_events() {
    let s = setup();
    let a = run(&s, "v2-directed-current", &["topic-change"], None);
    let b = run(&s, "v2-directed-current", &["topic-change"], None);
    assert_eq!(
        serde_json::to_value(&a[0].events).unwrap(),
        serde_json::to_value(&b[0].events).unwrap()
    );
}

#[test]
fn a_lost_overlap_precondition_withholds_the_stimulus() {
    let s = setup();
    // Ask for an acknowledgment after a reply that is shorter than the delay.
    let text = lamp_voice_eval::plan::DEFAULT_PLAN.replace(
        r#""fake_replies": [{"text": "Once there was a small robot named Pip who loved fixing lamps for the people in its building. One night a storm knocked the power out, and Pip carried its little light from door to door.", "speech_ms": 12000}]
    },
    {
      "id": "ack-yeah""#,
        r#""fake_replies": [{"text": "Short.", "speech_ms": 1000}]
    },
    {
      "id": "ack-yeah""#,
    );
    assert_ne!(text, lamp_voice_eval::plan::DEFAULT_PLAN);
    let plan = LoadedPlan::parse(&text, &s.catalog).unwrap();
    let custom = Setup {
        catalog: s.catalog.clone(),
        plan,
    };
    let records = run(
        &custom,
        "v2-directed-current",
        &["listener-acknowledgment"],
        None,
    );
    let step = &records[0].steps[1];
    assert_eq!(step.status, StepStatus::PreconditionLost);
    assert_eq!(step.started_us, None, "nothing was played");
    let score = score(&custom, &records[0]);
    assert!(matches!(
        score.steps[1].status,
        CheckStatus::Withheld { .. }
    ));
    assert_eq!(score.outcome, Outcome::Incomplete);
}

#[test]
fn a_missing_trigger_is_recorded_not_replaced_by_a_fixed_delay() {
    let s = setup();
    // The story's audio never arrives within the step deadline, so the
    // interruption's speaker_first_write trigger never comes.
    let mut plan = s.plan.clone();
    let scenario = plan
        .plan
        .scenarios
        .iter_mut()
        .find(|s| s.id == "topic-change")
        .unwrap();
    scenario.physical = false;
    scenario.fake_faults = vec![lamp_voice_eval::plan::ProviderFault::FirstAudioDelay {
        turn: 1,
        delay_ms: 50_000,
    }];
    scenario.steps[1].trigger.deadline_ms = 5_000;
    let custom = Setup {
        catalog: s.catalog.clone(),
        plan,
    };
    let records = run(&custom, "v2-directed-current", &["topic-change"], None);
    assert_eq!(records[0].steps[1].status, StepStatus::TriggerMissed);
    assert_eq!(
        records[0].steps[1].started_us, None,
        "nothing was played on a guess"
    );
    let score = score(&custom, &records[0]);
    assert!(
        score
            .findings
            .iter()
            .any(|f| f.kind == FindingKind::TriggerMissed)
    );
    assert!(matches!(
        score.steps[1].status,
        CheckStatus::Withheld { .. }
    ));
    assert_ne!(score.outcome, Outcome::Passed);
}

#[test]
fn signal_mode_uses_lamp_live_vad_on_the_exact_cached_mix() {
    let s = setup();
    let dir = common::Private::new("sig");
    let manifest = common::synthetic_cache(
        &dir.path,
        &s.catalog,
        &[
            (
                "quick-fact",
                common::tone_scene(&s.catalog, "quick-fact", 0.3),
            ),
            ("noise-only", {
                let mut state = 99_u32;
                (0..20 * 16_000)
                    .map(|_| {
                        state ^= state << 13;
                        state ^= state >> 17;
                        state ^= state << 5;
                        0.05 * (state as f32 / u32::MAX as f32 * 2.0 - 1.0)
                    })
                    .collect()
            }),
        ],
    );
    let assets = AssetIndex::load(&dir.join("cache"), &[manifest], &s.catalog).unwrap();
    let records = run(
        &s,
        "v2-directed-current",
        &["quick-fact", "noise-only", "quick-chat"],
        Some(&assets),
    );
    let scores: Vec<_> = records.iter().map(|r| score(&s, r)).collect();
    assert!(
        scores
            .iter()
            .all(|sc| sc.stratum == Stratum::FakeSignal || sc.outcome == Outcome::Withheld)
    );
    // A steady tone is scored as speech by the VAD: it is admitted and answered.
    assert_eq!(
        scores[0].steps[0].answer,
        Some(AnswerOutcome::Complete),
        "{:?}",
        scores[0].findings
    );
    assert_eq!(
        scores[1].outcome,
        Outcome::Passed,
        "white noise stays below the start threshold"
    );
    assert_eq!(
        scores[2].outcome,
        Outcome::Withheld,
        "no verified mix, so nothing is invented"
    );
    let timing = records[0].steps[0].timing.as_ref().unwrap();
    assert_eq!(
        timing.source,
        lamp_voice_eval::stimulus::TimingSource::MeasuredMix
    );
}

#[test]
fn an_admission_during_a_noise_only_scene_fails_that_silence_step() {
    use lamp_voice_eval::events::{ClockDomain, EventSource, RuntimeEvent};
    let s = setup();
    let mut record = run(&s, "v2-directed-current", &["noise-only"], None).remove(0);
    let started = record.steps[0].started_us.unwrap();
    for (kind, offset) in [
        (
            EventKind::InputAdmitted {
                prefix_first_read_us: Some(started + 1_700_000),
                candidate: None,
                basis: None,
            },
            2_000_000,
        ),
        (EventKind::SpeakerFirstWrite, 5_000_000),
    ] {
        record.events.push(RuntimeEvent::new(
            kind,
            Some(1),
            started + offset,
            ClockDomain::Virtual,
            EventSource::Fake,
        ));
    }
    let score = score(&s, &record);
    assert_eq!(
        score.steps[0].turns,
        vec![1],
        "the scene's whole duration is its window"
    );
    assert_eq!(score.steps[0].status, CheckStatus::Fail);
    assert_eq!(score.unattributed_admissions, 0);
}

/// Places every reply-start event 2 s earlier on the runner clock, as if its
/// cue had been relayed 2 s late.
struct LateCues<'a>(FakeBackend<'a>);
impl lamp_voice_eval::runner::Backend for LateCues<'_> {
    fn describe(&self) -> serde_json::Value {
        self.0.describe()
    }
    fn stratum(&self, s: &lamp_voice_eval::plan::Scenario) -> Stratum {
        self.0.stratum(s)
    }
    fn domain(&self) -> lamp_voice_eval::events::ClockDomain {
        self.0.domain()
    }
    fn unsupported(&self, s: &lamp_voice_eval::plan::Scenario) -> Option<String> {
        self.0.unsupported(s)
    }
    fn begin(
        &mut self,
        c: &lamp_voice_eval::runner::AttemptContext,
    ) -> lamp_voice_eval::Result<lamp_voice_eval::runner::Begin> {
        self.0.begin(c)
    }
    fn now_us(&mut self) -> u64 {
        self.0.now_us()
    }
    fn next_event(
        &mut self,
        until: u64,
    ) -> lamp_voice_eval::Result<Option<lamp_voice_eval::events::RuntimeEvent>> {
        self.0.next_event(until)
    }
    fn runner_time(&self, event: &lamp_voice_eval::events::RuntimeEvent) -> (u64, String) {
        let (at, method) = self.0.runner_time(event);
        match event.kind {
            EventKind::SpeakerFirstWrite => (at.saturating_sub(2_000_000), method),
            _ => (at, method),
        }
    }
    fn inject(
        &mut self,
        step: &lamp_voice_eval::plan::Step,
        scene: &str,
        timing: &lamp_voice_eval::stimulus::SceneTiming,
        at: u64,
    ) -> lamp_voice_eval::Result<lamp_voice_eval::runner::Injection> {
        self.0.inject(step, scene, timing, at)
    }
    fn finish(
        &mut self,
        c: &lamp_voice_eval::runner::AttemptContext,
    ) -> lamp_voice_eval::Result<lamp_voice_eval::runner::Finished> {
        self.0.finish(c)
    }
    fn reproduce(&self, c: &lamp_voice_eval::runner::AttemptContext) -> String {
        self.0.reproduce(c)
    }
}

#[test]
fn a_stale_trigger_withholds_the_stimulus_instead_of_playing_it_late() {
    let s = setup();
    let dir = common::Private::new("stale");
    let run_dir = dir.join("run");
    lamp_voice_eval::ledger::create_run_directory(&run_dir).unwrap();
    let inner = FakeBackend::new(
        "v2-directed-current",
        s.plan.profile("v2-directed-current").unwrap().clone(),
        s.plan.plan.fixed_reply.clone(),
        None,
    );
    let mut backend = LateCues(inner);
    let options = SuiteOptions {
        run_id: "t".into(),
        scenarios: vec!["listener-acknowledgment".into()],
        repetitions: 1,
        seed: 7,
        shuffle: false,
        attempt_seed: None,
    };
    let records = run_suite(&s.plan, &s.catalog, &mut backend, &options, &run_dir).unwrap();
    let step = &records[0].steps[1];
    assert_eq!(step.status, StepStatus::TriggerStale, "{:?}", step.detail);
    assert_eq!(step.started_us, None);
    let score = score(&s, &records[0]);
    assert!(
        score
            .findings
            .iter()
            .any(|f| f.kind == FindingKind::TriggerStale)
    );
    assert_ne!(score.outcome, Outcome::Passed);
}
