//! Score an existing lamp-live trace (for example a retained physical trial)
//! against a declared scenario. Traces carry no stimulus times, so turns are
//! attributed to steps by explicit declaration and labeled as such.
use crate::{
    Result,
    events::{ClockDomain, EventKind, parse_trace},
    invalid,
    ledger::{Ledger, unix_ms},
    plan::{LoadedPlan, ProviderKind},
    record::{
        AttemptRecord, AttemptStatus, Attribution, StepRecord, StepStatus, StimulusSource, Stratum,
    },
    stimulus::StimulusCatalog,
};
use lamp_acoustic::hash;
use serde_json::{Value, json};
use std::{fs, path::Path};

pub struct ImportOptions<'a> {
    pub run_id: String,
    pub events_path: &'a Path,
    pub scenario: String,
    /// Declared `step -> admitted turn` pairs; `None` declares that the step
    /// produced no admission. Every stimulus step must be declared, except in a
    /// single-stimulus scenario, where the default gives that step the first
    /// admission (or none) and leaves later admissions unattributed.
    pub turn_map: Vec<(String, Option<u64>)>,
    /// Observer `metadata.json` of a continuous room recording, when one exists.
    pub room_metadata: Option<&'a Path>,
    /// Whether that recorder is independent of Lamp's own audio hardware.
    pub room_independent: bool,
    /// How the trial's speech was produced, when known.
    pub source: StimulusSource,
}

/// Room-audio evidence from a `lamp-observer record` final report.
pub fn room_evidence(metadata: &Value, independent: bool, source: &str) -> Value {
    json!({
        "sha256": metadata["room_wav_sha256"],
        "valid": metadata["valid"] == true,
        "status": metadata["status"],
        "capture_requested_runner_us": metadata["start_requested_host_ns"].as_u64().map(|ns| ns / 1000),
        "independent_of_lamp": independent,
        "source": source,
    })
}

pub fn import_trace(
    plan: &LoadedPlan,
    catalog: &StimulusCatalog,
    options: &ImportOptions,
    run_directory: &Path,
) -> Result<AttemptRecord> {
    let scenario = plan.scenario(&options.scenario)?;
    let text = fs::read_to_string(options.events_path)?;
    if text.len() > 64 * 1024 * 1024 {
        return Err(invalid("trace exceeds 64 MiB"));
    }
    let (events, unmapped) = parse_trace(&text)?;
    let provider = events.iter().find_map(|e| match &e.kind {
        EventKind::RunStart { provider_kind } => provider_kind.clone(),
        _ => None,
    });
    let traced = match provider.as_deref() {
        Some("one_cached_reply") => ProviderKind::FixedReply,
        Some("gemini") => ProviderKind::Gemini,
        _ => return Err(invalid("trace has no recognizable run_start provider_kind")),
    };
    if traced != scenario.provider {
        return Err(invalid(
            "trace provider differs from the declared scenario provider",
        ));
    }
    let admitted: Vec<u64> = events
        .iter()
        .filter(|e| matches!(e.kind, EventKind::InputAdmitted { .. }))
        .filter_map(|e| e.turn)
        .collect();
    let stimuli: Vec<&str> = scenario
        .steps
        .iter()
        .filter(|s| s.scene.is_some())
        .map(|s| s.id.as_str())
        .collect();
    let mut turn_map = options.turn_map.clone();
    let mut assumption = "declared by the operator";
    if turn_map.is_empty() && stimuli.len() == 1 {
        assumption = "default: the only stimulus step owns the first admission; later admissions are unattributed";
        turn_map.push((stimuli[0].to_owned(), admitted.first().copied()));
    }
    for (step, turn) in &turn_map {
        let known_step = scenario.steps.iter().any(|s| &s.id == step);
        if !known_step || turn.is_some_and(|turn| !admitted.contains(&turn)) {
            return Err(invalid(&format!(
                "turn map entry {step}={turn:?} matches no step or admitted turn"
            )));
        }
    }
    // Undeclared stimuli would make every unattributed admission ambiguous.
    if let Some(missing) = stimuli
        .iter()
        .find(|id| !turn_map.iter().any(|(step, _)| step == *id))
    {
        return Err(invalid(&format!(
            "declare every stimulus step with --turn STEP=TURN or STEP=none; {missing} is missing"
        )));
    }
    let absent: Vec<String> = turn_map
        .iter()
        .filter(|(_, turn)| turn.is_none())
        .map(|(step, _)| step.clone())
        .collect();
    let turn_map: Vec<(String, u64)> = turn_map
        .into_iter()
        .filter_map(|(step, turn)| Some((step, turn?)))
        .collect();
    let mut steps = Vec::new();
    let mut previous_turn = None;
    for step in &scenario.steps {
        let mut record = StepRecord::planned(step, StepStatus::Injected);
        record.timing = step
            .scene
            .as_deref()
            .map(|scene| catalog.estimate(scene))
            .transpose()?;
        record.bound_turn = previous_turn;
        record.detail = Some("imported: stimulus timing not in the trace".into());
        if let Some((_, turn)) = turn_map.iter().find(|(id, _)| id == &step.id) {
            previous_turn = Some(*turn);
        }
        steps.push(record);
    }
    let room = match options.room_metadata {
        Some(path) => {
            let metadata: Value = serde_json::from_slice(&fs::read(path)?)?;
            room_evidence(
                &metadata,
                options.room_independent,
                &path.display().to_string(),
            )
        }
        None => Value::Null,
    };
    let record = AttemptRecord {
        attempt_id: format!("{}-import-{}", options.run_id, scenario.id),
        run_id: options.run_id.clone(),
        scenario: scenario.id.clone(),
        cohort: scenario.cohort,
        capability: scenario.capability,
        provider: scenario.provider,
        stratum: Stratum::ImportedTrace,
        source: options.source,
        repetition: 0,
        order: 0,
        profile: None,
        seed: None,
        step_domain: ClockDomain::LampMonotonic,
        steps,
        events,
        live_events: Vec::new(),
        clock: None,
        attribution: Attribution::Declared {
            turns: turn_map,
            absent,
        },
        status: AttemptStatus::Completed,
        spoken_failure_notice: None,
        evidence: json!({
            "trace_path": options.events_path.display().to_string(),
            "trace_sha256": hash(text.as_bytes()),
            "attribution_assumption": assumption,
            "room_audio": room,
        }),
        unmapped_trace_records: unmapped,
        reproduce: format!(
            "cargo run -p lamp-voice-eval -- import-trace --events {} --scenario {} --out NEW_DIR",
            options.events_path.display(),
            scenario.id
        ),
    };
    let mut ledger = Ledger::create(run_directory)?;
    ledger.append(
        "run_start",
        json!({"run_id": options.run_id, "plan_id": plan.plan.id, "plan_sha256": plan.sha256,
            "merged_catalog_sha256": catalog.merged_sha256, "backend": {"kind": "imported_trace"},
            "started_unix_ms": unix_ms()}),
        true,
    )?;
    ledger.append("attempt_planned", json!({"attempt_id": record.attempt_id, "scenario": scenario.id,
        "stratum": Stratum::ImportedTrace, "note": "imported after the fact; no stimulus was played by this tool"}), true)?;
    ledger.append(
        "attempt_finished",
        json!({"attempt_id": record.attempt_id, "record": record}),
        true,
    )?;
    ledger.append(
        "run_end",
        json!({"run_id": options.run_id, "attempts": 1, "finished_unix_ms": unix_ms()}),
        true,
    )?;
    Ok(record)
}
