//! End-to-end physical orchestration without hardware: the runner starts the
//! real `lamp-session` relay, which starts a fake lamp-live that emits cues via
//! lamp-live's own `CueSink`; "playback" goes to a fake room socket. This runs
//! in real time and plays no audio.
mod common;

use lamp_voice_eval::{
    evaluate::{self, FindingKind, Outcome},
    events::EventSource,
    physical::mac::{FakeRoomPlayer, LampMode, PhysicalBackend, PhysicalConfig},
    plan::LoadedPlan,
    record::{AttemptStatus, StepStatus, Stratum},
    runner::{Backend, SuiteOptions, run_suite},
    stimulus::{AssetIndex, StimulusCatalog},
};
use std::fs;

const BIN: &str = env!("CARGO_BIN_EXE_lamp-voice-eval");

fn config(dir: &common::Private, scenario: &str, mode: LampMode) -> PhysicalConfig {
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
        "12",
    ] {
        command.extend(["--runtime-prefix-arg".to_owned(), arg.to_owned()]);
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
    let mut backend =
        PhysicalBackend::new(config(&dir, scenario, fixture()), &assets, Box::new(player));
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
fn directed_mode_withholds_event_triggered_scenarios_instead_of_guessing_delays() {
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
            .contains("proposal P1")
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
