//! End-to-end physical orchestration without hardware: the runner starts the
//! real `lamp-session` relay, which starts a fake lamp-live that emits cues via
//! lamp-live's own `CueSink`; "playback" goes to a fake room socket. This runs
//! in real time and plays no audio.
mod common;

use lamp_voice_eval::{
    evaluate::{self, FindingKind, Outcome},
    events::{EventKind, EventSource},
    physical::mac::{FakeRoomPlayer, LampMode, PhysicalBackend, PhysicalConfig},
    plan::LoadedPlan,
    record::{AttemptStatus, StepStatus, Stratum},
    runner::{Backend, SuiteOptions, run_suite},
    stimulus::{AssetIndex, StimulusCatalog},
};
use std::fs;

const BIN: &str = env!("CARGO_BIN_EXE_lamp-voice-eval");

fn config(dir: &common::Private, scenario: &str, mode: LampMode) -> PhysicalConfig {
    config_with_fault(dir, scenario, mode, 12, None)
}

fn config_with_fault(
    dir: &common::Private,
    scenario: &str,
    mode: LampMode,
    seconds: u16,
    fault: Option<&str>,
) -> PhysicalConfig {
    let lamp_root = dir.join("lamp");
    fs::create_dir(&lamp_root).unwrap();
    let mut command = vec![
        BIN.to_owned(),
        "lamp-session".into(),
        "--runtime".into(),
        BIN.into(),
    ];
    for arg in [
        "fake-lamp-live",
        "--fake-scenario",
        scenario,
        "--fake-profile",
        "v2-directed-current",
        "--fake-room",
        &dir.join("room.sock").display().to_string(),
        "--fake-session-seconds",
        &seconds.to_string(),
    ] {
        command.extend(["--runtime-prefix-arg".to_owned(), arg.to_owned()]);
    }
    if let Some(fault) = fault {
        let option = if fault == "two" {
            "--fake-generations"
        } else {
            "--fake-cue-fault"
        };
        for arg in [option, fault] {
            command.extend(["--runtime-prefix-arg".to_owned(), arg.to_owned()]);
        }
    }
    PhysicalConfig {
        lamp_command: command,
        lamp_work_root: lamp_root.display().to_string(),
        mode,
        noise_suppression: "on".into(),
        diagnostics: false,
        room_recorder: None,
        room_independent: false,
        allowance_seconds: 10,
        ring_channel_ceiling: None,
        stimulus_source: lamp_voice_eval::record::StimulusSource::LoudspeakerSynthetic,
    }
}

fn fixture() -> LampMode {
    LampMode::Fixture {
        reply: "/nonexistent/reply.wav".into(),
        sha256: "0".repeat(64),
    }
}

fn run(
    scenario: &str,
    scenes: &[&str],
) -> (
    lamp_voice_eval::record::AttemptRecord,
    evaluate::AttemptScore,
) {
    run_mode(scenario, scenes, fixture(), 12, None)
}

fn directed() -> LampMode {
    LampMode::Directed {
        provider_config: "/private/unused-fake-provider.json".into(),
    }
}

fn run_mode(
    scenario: &str,
    scenes: &[&str],
    mode: LampMode,
    seconds: u16,
    fault: Option<&str>,
) -> (
    lamp_voice_eval::record::AttemptRecord,
    evaluate::AttemptScore,
) {
    let catalog = StimulusCatalog::load().unwrap();
    let plan = LoadedPlan::default_plan(&catalog).unwrap();
    let dir = common::Private::new("loop");
    let tones: Vec<_> = scenes
        .iter()
        .map(|s| (*s, common::tone_scene(&catalog, s, 0.3)))
        .collect();
    let manifest = common::synthetic_cache(&dir.path, &catalog, &tones);
    let assets = AssetIndex::load(&dir.join("cache"), &[manifest], &catalog).unwrap();
    let player = FakeRoomPlayer {
        room: dir.join("room.sock"),
    };
    let mut backend = PhysicalBackend::new(
        config_with_fault(&dir, scenario, mode, seconds, fault),
        &assets,
        Box::new(player),
    );
    let run = dir.join("run");
    lamp_voice_eval::ledger::create_run_directory(&run).unwrap();
    let options = SuiteOptions {
        run_id: "loop".into(),
        scenarios: vec![scenario.into()],
        repetitions: 1,
        seed: 3,
        shuffle: false,
        attempt_seed: None,
        self_test: serde_json::Value::Null,
    };
    let mut records = run_suite(&plan, &catalog, &mut backend, &options, &run).unwrap();
    let record = records.remove(0);
    let score = evaluate::score(&record, plan.plan.answer_deadline_ms, None);
    (record, score)
}

#[test]
fn cue_triggered_acknowledgment_is_injected_during_playback_and_scored() {
    let (record, score) = run(
        "fixed-reply-acknowledgment",
        &["quick-chat", "listener-acknowledgment"],
    );
    assert_eq!(
        record.status,
        AttemptStatus::Completed,
        "{:?}",
        record.evidence
    );
    assert_eq!(record.stratum, Stratum::PhysicalFixture);
    assert!(
        record
            .live_events
            .iter()
            .any(|e| e.source == EventSource::Cue)
    );
    let clock = record.clock.as_ref().expect("ping/pong mapping");
    assert!(clock.uncertainty_us < 50_000, "{clock:?}");
    let ack = &record.steps[1];
    assert_eq!(ack.status, StepStatus::Injected);
    let trigger = ack.trigger.as_ref().unwrap();
    assert_eq!(
        trigger.event_domain,
        lamp_voice_eval::events::ClockDomain::LampMonotonic
    );
    assert!(
        trigger.runner_time_method.contains("mapped"),
        "{}",
        trigger.runner_time_method
    );
    // Started 1.5 s after the mapped cue time, within a loose real-time bound.
    let late_ms = (ack.started_us.unwrap() as i64 - (trigger.runner_us + 1_500_000) as i64) / 1000;
    assert!((0..30).contains(&late_ms), "{late_ms}");
    assert!(!record.events.is_empty(), "the final trace was relayed");
    assert!(
        score
            .findings
            .iter()
            .any(|f| f.kind == FindingKind::FalseInterruption),
        "{:?}",
        score.findings
    );
    assert_eq!(score.outcome, Outcome::Failed);
    assert!(
        score
            .acoustic
            .iter()
            .all(|a| a.unscored_reason.as_deref() == Some("no reviewed room-audio annotation"))
    );
}

#[test]
fn echo_only_window_passes_through_the_relay_without_extra_admissions() {
    let (record, score) = run("fixed-reply-echo-only", &["quick-chat"]);
    assert_eq!(
        record.status,
        AttemptStatus::Completed,
        "{:?}",
        record.evidence
    );
    assert_eq!(record.steps[1].status, StepStatus::Injected);
    assert_eq!(score.admissions, 1);
    assert_eq!(score.outcome, Outcome::Passed, "{:?}", score.findings);
    assert_eq!(record.evidence["session_end"]["exit_code"], 0);
}

#[test]
fn directed_mode_keeps_provider_fault_and_asset_restrictions() {
    let catalog = StimulusCatalog::load().unwrap();
    let plan = LoadedPlan::default_plan(&catalog).unwrap();
    let dir = common::Private::new("dir");
    let manifest = common::synthetic_cache(
        &dir.path,
        &catalog,
        &[(
            "quick-chat",
            common::tone_scene(&catalog, "quick-chat", 0.3),
        )],
    );
    let assets = AssetIndex::load(&dir.join("cache"), &[manifest], &catalog).unwrap();
    let backend = PhysicalBackend::new(
        config(
            &dir,
            "quick-chat",
            LampMode::Directed {
                provider_config: "/private/provider.json".into(),
            },
        ),
        &assets,
        Box::new(FakeRoomPlayer {
            room: dir.join("room.sock"),
        }),
    );
    assert!(
        backend
            .unsupported(plan.scenario("follow-up-chain").unwrap())
            .unwrap()
            .contains("no verified cached asset")
    );
    assert!(
        backend
            .unsupported(plan.scenario("fixed-reply-echo-only").unwrap())
            .is_some()
    );
    assert!(
        backend
            .unsupported(plan.scenario("provider-late-answer").unwrap())
            .is_some()
    );
    assert!(
        backend
            .unsupported(plan.scenario("quick-fact").unwrap())
            .unwrap()
            .contains("no verified cached asset")
    );
    assert!(
        backend
            .unsupported(plan.scenario("quick-chat").unwrap())
            .is_none()
    );
}

fn cue_time(record: &lamp_voice_eval::record::AttemptRecord, name: &str, turn: u64) -> u64 {
    record.evidence["cue_receipts"].as_array().unwrap().iter()
        .find(|r| r["accepted"] == true && r["cue"]["kind"] == name && r["cue"]["turn"].as_u64() == Some(turn))
        .unwrap_or_else(|| panic!("missing {name} cue for {turn}: {}", record.evidence))
        ["cue"]["event_us"].as_u64().unwrap()
}

fn assert_directed_receipts(record: &lamp_voice_eval::record::AttemptRecord) {
    assert_eq!(
        record.status,
        AttemptStatus::Completed,
        "{:?}",
        record.evidence
    );
    assert_eq!(record.evidence["session_start"]["mode"], "directed");
    assert_eq!(record.evidence["session_start"]["cue_socket"], true);
    let argv = record.evidence["session_start"]["runtime_argv"]
        .as_array()
        .unwrap();
    assert!(argv.iter().any(|a| a == "--cue-socket"));
    assert!(
        !argv.iter().any(|a| a == "--diagnostics"),
        "cues must not opt in PCM diagnostics"
    );
    assert_eq!(record.evidence["protocol_errors"], serde_json::json!([]));
    let receipts = record.evidence["cue_receipts"].as_array().unwrap();
    assert!(!receipts.is_empty());
    for (i, receipt) in receipts.iter().enumerate() {
        assert_eq!(receipt["accepted"], true, "{receipt}");
        assert_eq!(receipt["cue"]["sequence"].as_u64(), Some(i as u64 + 1));
        assert_eq!(receipt["cue"]["boot"], serde_json::json!(vec![7; 16]));
        if let Some(turn) = receipt["cue"]["turn"].as_u64() {
            assert_eq!(receipt["cue"]["generation"].as_u64(), Some(turn + 1));
        }
        assert!(
            receipt["lamp_received_us"].as_u64().unwrap()
                < receipt["cue"]["expires_us"].as_u64().unwrap()
        );
    }
    // Exact source events and their local receipt times remain distinguishable.
    for cue in &record.live_events {
        if cue.source == EventSource::Cue {
            assert!(
                record
                    .events
                    .iter()
                    .any(|e| e.name() == cue.name() && e.turn == cue.turn && e.at_us == cue.at_us),
                "{cue:?}"
            );
            assert!(cue.received_us.is_some());
        }
    }
    println!(
        "{} software step records: {}",
        record.scenario,
        serde_json::to_string(&record.steps).unwrap()
    );
    println!(
        "{} software cue receipts: {}",
        record.scenario, record.evidence["cue_receipts"]
    );
}

#[test]
fn directed_followup_waits_for_whole_reply_completion_then_admits_a_new_turn() {
    let (record, score) = run_mode(
        "rapid-follow-up",
        &["mid-sentence-address", "rapid-followup"],
        directed(),
        22,
        None,
    );
    assert_directed_receipts(&record);
    assert!(
        record
            .steps
            .iter()
            .all(|s| s.status == StepStatus::Injected)
    );
    let followup = &record.steps[1];
    let retired = cue_time(&record, "turn_completed", 1);
    assert_eq!(followup.bound_turn, Some(1));
    assert_eq!(
        followup.trigger.as_ref().unwrap().event_at_us,
        Some(retired)
    );
    assert!(cue_time(&record, "input_admitted", 1) < cue_time(&record, "local_endpoint", 1));
    assert!(cue_time(&record, "local_endpoint", 1) < cue_time(&record, "speaker_first_write", 1));
    assert!(cue_time(&record, "input_admitted", 2) > retired);
    assert!(cue_time(&record, "speech_retired", 2) > cue_time(&record, "speaker_first_write", 2));
    let late = followup
        .started_us
        .unwrap()
        .saturating_sub(followup.target_us.unwrap());
    assert!(late < 50_000, "local scheduling late by {late} us");
    assert_eq!(score.admissions, 2);
    assert!(score.acoustic.iter().all(|a| a.unscored_reason.is_some()));
}

#[test]
fn directed_natural_interruption_runs_during_reply_with_original_cancel_reason() {
    let (record, score) = run_mode(
        "topic-change",
        &["story-primer", "topic-change"],
        directed(),
        24,
        None,
    );
    assert_directed_receipts(&record);
    assert!(
        record
            .steps
            .iter()
            .all(|s| s.status == StepStatus::Injected)
    );
    let change = &record.steps[1];
    assert_eq!(change.bound_turn, Some(1));
    assert_eq!(
        change.trigger.as_ref().unwrap().event_at_us,
        Some(cue_time(&record, "speaker_first_write", 1))
    );
    let cancel = record.live_events.iter().find(|e| e.turn == Some(1) && matches!(&e.kind, EventKind::TurnCancelled { reason: Some(reason), .. } if reason == "user_interrupted")).unwrap();
    assert_eq!(cancel.source, EventSource::Cue);
    assert!(cancel.at_us.unwrap() > cue_time(&record, "speaker_first_write", 1));
    assert!(cancel.at_us.unwrap() <= cue_time(&record, "input_admitted", 2));
    assert!(
        !record
            .events
            .iter()
            .any(|e| e.turn == Some(1) && matches!(e.kind, EventKind::SpeechRetired))
    );
    assert!(cue_time(&record, "speech_retired", 2) > cue_time(&record, "local_endpoint", 2));
    assert_eq!(score.admissions, 2);
    assert!(score.acoustic.iter().all(|a| a.unscored_reason.is_some()));
}

#[test]
fn directed_missing_or_stale_retirement_never_falls_back_to_a_timer_or_final_trace() {
    for fault in ["drop-retirement", "stale-retirement"] {
        let (record, score) = run_mode(
            "rapid-follow-up",
            &["mid-sentence-address", "rapid-followup"],
            directed(),
            15,
            Some(fault),
        );
        assert_eq!(
            record.steps[0].status,
            StepStatus::Injected,
            "{fault}: {:?}",
            record.evidence
        );
        assert_ne!(record.steps[1].status, StepStatus::Injected, "{fault}");
        assert!(record.steps[1].started_us.is_none());
        assert_eq!(record.evidence["playbacks"].as_array().unwrap().len(), 1);
        // The final trace contains the real retirement, but it arrives too late
        // to authorize a live stimulus and never becomes a replacement cue.
        assert!(
            record
                .events
                .iter()
                .any(|e| e.turn == Some(1) && matches!(e.kind, EventKind::SpeechRetired))
        );
        assert!(
            !record
                .live_events
                .iter()
                .any(|e| matches!(e.kind, EventKind::SpeechRetired))
        );
        assert!(matches!(record.status, AttemptStatus::Aborted { .. }));
        assert!(
            !record.evidence["protocol_errors"]
                .as_array()
                .unwrap()
                .is_empty()
        );
        assert_ne!(score.outcome, Outcome::Passed);
        println!(
            "{fault}: status={:?}, second={:?}, errors={}",
            record.status, record.steps[1].status, record.evidence["protocol_errors"]
        );
    }
}

/// A player whose output device fails: nothing reaches the room.
struct BrokenPlayer;
impl lamp_voice_eval::physical::mac::Player for BrokenPlayer {
    fn start(
        &mut self,
        _: &std::path::Path,
        _: &lamp_voice_eval::stimulus::SceneTiming,
        _: &std::path::Path,
        _: &std::path::Path,
    ) -> lamp_voice_eval::Result<std::thread::JoinHandle<serde_json::Value>> {
        Ok(std::thread::spawn(
            || serde_json::json!({"valid": false, "status": "failed", "error": "output device unavailable"}),
        ))
    }
    fn describe(&self) -> serde_json::Value {
        serde_json::json!({"kind": "broken test player"})
    }
}

#[test]
fn undelivered_stimuli_are_withheld_not_scored_as_silence() {
    let catalog = StimulusCatalog::load().unwrap();
    let plan = LoadedPlan::default_plan(&catalog).unwrap();
    let dir = common::Private::new("brk");
    let manifest = common::synthetic_cache(
        &dir.path,
        &catalog,
        &[(
            "quick-chat",
            common::tone_scene(&catalog, "quick-chat", 0.3),
        )],
    );
    let assets = AssetIndex::load(&dir.join("cache"), &[manifest], &catalog).unwrap();
    let mut backend = PhysicalBackend::new(
        config(&dir, "fixed-reply-echo-only", fixture()),
        &assets,
        Box::new(BrokenPlayer),
    );
    let run = dir.join("run");
    lamp_voice_eval::ledger::create_run_directory(&run).unwrap();
    let options = SuiteOptions {
        run_id: "brk".into(),
        scenarios: vec!["fixed-reply-echo-only".into()],
        repetitions: 1,
        seed: 3,
        shuffle: false,
        attempt_seed: None,
        self_test: serde_json::Value::Null,
    };
    let record = run_suite(&plan, &catalog, &mut backend, &options, &run)
        .unwrap()
        .remove(0);
    assert_eq!(record.steps[0].status, StepStatus::DeliveryFailed);
    let score = evaluate::score(&record, plan.plan.answer_deadline_ms, None);
    assert!(
        score
            .findings
            .iter()
            .any(|f| f.kind == FindingKind::StimulusNotDelivered)
    );
    assert_ne!(score.outcome, Outcome::Passed);
}

#[test]
fn a_transport_that_closes_mid_trace_invalidates_the_attempt() {
    let catalog = StimulusCatalog::load().unwrap();
    let plan = LoadedPlan::default_plan(&catalog).unwrap();
    let dir = common::Private::new("cut");
    let manifest = common::synthetic_cache(
        &dir.path,
        &catalog,
        &[(
            "quick-chat",
            common::tone_scene(&catalog, "quick-chat", 0.3),
        )],
    );
    let assets = AssetIndex::load(&dir.join("cache"), &[manifest], &catalog).unwrap();
    let mut cfg = config(&dir, "quick-chat", fixture());
    // Relays a session start and the first trace record, then disconnects.
    cfg.lamp_command = vec![
        "/bin/sh".into(),
        "-c".into(),
        r#"printf '%s\n' '{"type":"session_start","schema":1,"lamp_us":1,"mode":"fixture","cue_socket":true,"runtime_argv":[]}' '{"type":"trace","record":{"kind":"run_start","at_us":2,"provider_kind":"one_cached_reply"}}'"#.into(),
        "sh".into(),
    ];
    let mut backend = PhysicalBackend::new(
        cfg,
        &assets,
        Box::new(FakeRoomPlayer {
            room: dir.join("room.sock"),
        }),
    );
    let run = dir.join("run");
    lamp_voice_eval::ledger::create_run_directory(&run).unwrap();
    let options = SuiteOptions {
        run_id: "cut".into(),
        scenarios: vec!["fixed-reply-echo-only".into()],
        repetitions: 1,
        seed: 3,
        shuffle: false,
        attempt_seed: None,
        self_test: serde_json::Value::Null,
    };
    let record = run_suite(&plan, &catalog, &mut backend, &options, &run)
        .unwrap()
        .remove(0);
    assert!(
        matches!(&record.status, AttemptStatus::Aborted { reason } if reason.contains("partial")),
        "{:?}",
        record.status
    );
    let score = evaluate::score(&record, plan.plan.answer_deadline_ms, None);
    assert_eq!(score.outcome, Outcome::Invalid);
}

#[test]
fn directed_two_occurrences_wait_for_true_completion_before_followup() {
    let (record, score) = run_mode(
        "rapid-follow-up",
        &["mid-sentence-address", "rapid-followup"],
        directed(),
        23,
        Some("two"),
    );
    assert_directed_receipts(&record);
    assert!(
        record
            .steps
            .iter()
            .all(|s| s.status == StepStatus::Injected)
    );
    let starts: Vec<_> = record
        .live_events
        .iter()
        .filter(|e| e.turn == Some(1) && matches!(e.kind, EventKind::SpeakerFirstWrite))
        .collect();
    let retires: Vec<_> = record
        .live_events
        .iter()
        .filter(|e| e.turn == Some(1) && matches!(e.kind, EventKind::SpeechRetired))
        .collect();
    assert_eq!(starts.len(), 2);
    assert_eq!(retires.len(), 2);
    assert_eq!(starts[0].playback_sequence, Some(1));
    assert_eq!(starts[1].playback_sequence, Some(2));
    assert_eq!(retires[1].playback_sequence, Some(2));
    let complete = cue_time(&record, "turn_completed", 1);
    assert!(complete >= retires[1].at_us.unwrap());
    assert_eq!(
        record.steps[1].trigger.as_ref().unwrap().event_at_us,
        Some(complete)
    );
    assert!(cue_time(&record, "input_admitted", 2) > complete);
    assert_eq!(
        score.steps[0].answer,
        Some(evaluate::AnswerOutcome::Complete)
    );
}
#[test]
fn directed_interruption_during_second_occurrence_preserves_owner_and_yields() {
    let (record, score) = run_mode(
        "topic-change",
        &["story-primer", "topic-change"],
        directed(),
        24,
        Some("two"),
    );
    assert_directed_receipts(&record);
    assert_eq!(record.steps[1].status, StepStatus::Injected);
    let starts: Vec<_> = record
        .live_events
        .iter()
        .filter(|e| e.turn == Some(1) && matches!(e.kind, EventKind::SpeakerFirstWrite))
        .collect();
    assert_eq!(starts.len(), 2);
    assert!(cue_time(&record, "cancelled", 1) > starts[1].at_us.unwrap());
    assert!(
        !record
            .live_events
            .iter()
            .any(|e| e.turn == Some(1) && matches!(e.kind, EventKind::TurnCompleted { .. }))
    );
    assert!(!record.live_events.iter().any(|e| e.turn == Some(1)
        && e.playback_sequence == Some(2)
        && matches!(e.kind, EventKind::SpeechRetired)));
    assert_eq!(
        score.steps[0].answer,
        Some(evaluate::AnswerOutcome::YieldedAsPlanned)
    );
    assert_eq!(score.steps[1].interruption, Some(true));
}
#[test]
fn missing_or_stale_whole_completion_withholds_followup_after_valid_retirement() {
    for fault in ["drop-completion", "stale-completion"] {
        let (record, score) = run_mode(
            "rapid-follow-up",
            &["mid-sentence-address", "rapid-followup"],
            directed(),
            15,
            Some(fault),
        );
        assert_eq!(record.steps[0].status, StepStatus::Injected);
        assert_ne!(record.steps[1].status, StepStatus::Injected);
        assert!(
            record
                .live_events
                .iter()
                .any(|e| e.turn == Some(1) && matches!(e.kind, EventKind::SpeechRetired))
        );
        assert!(
            !record
                .live_events
                .iter()
                .any(|e| matches!(e.kind, EventKind::TurnCompleted { .. }))
        );
        assert!(
            record
                .events
                .iter()
                .any(|e| matches!(e.kind, EventKind::TurnCompleted { .. }))
        );
        assert_eq!(record.evidence["playbacks"].as_array().unwrap().len(), 1);
        assert_ne!(score.outcome, Outcome::Passed);
    }
}
