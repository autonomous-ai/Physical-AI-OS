//! Event-triggered scenario execution shared by the fake and physical backends.
//!
//! Each step waits for its triggering runtime event within a bounded deadline,
//! then starts its stimulus at `event + delay`. A missing trigger, a lost overlap
//! precondition or an ended session withholds the remaining steps; the runner
//! never falls back to a fixed delay and never drops an attempt from the ledger.
use crate::{
    Result, Rng, derive_seed,
    events::{ClockDomain, EventKind, RuntimeEvent},
    ledger::{Ledger, unix_ms},
    plan::{LoadedPlan, Scenario, Step, TriggerEvent},
    record::{
        AttemptRecord, AttemptStatus, Attribution, ClockMapping, StepRecord, StepStatus, Stratum,
        TriggerObservation,
    },
    stimulus::{SceneTiming, StimulusCatalog},
};
use serde_json::{Value, json};
use std::{collections::BTreeSet, path::PathBuf};

pub struct AttemptContext<'a> {
    pub attempt_id: String,
    pub run_id: String,
    pub scenario: &'a Scenario,
    pub repetition: u32,
    pub order: u32,
    pub seed: u64,
    pub run_directory: PathBuf,
}

pub enum Begin {
    Ready,
    Withheld(String),
}

pub struct Injection {
    /// Best evidence of the actual start in the runner domain.
    pub started_us: u64,
    pub receipt: Value,
}

pub struct Finished {
    pub events: Vec<RuntimeEvent>,
    pub clock: Option<ClockMapping>,
    pub evidence: Value,
    /// Backend failure that invalidates the attempt's evidence.
    pub aborted: Option<String>,
    pub unmapped: usize,
    pub spoken_failure_notice: Option<bool>,
    /// Later, better start evidence per step (for example the player's own
    /// start request), replacing the runner's provisional start time.
    pub corrected_starts: Vec<(String, u64)>,
    /// Steps whose stimulus the player reports as not delivered, with reason.
    pub failed_deliveries: Vec<(String, String)>,
}

/// A trigger later than this cannot start its stimulus at the planned phase;
/// the step is withheld instead of being played late.
pub const MAX_TRIGGER_LATENESS_US: u64 = 100_000;

pub trait Backend {
    fn describe(&self) -> Value;
    fn stratum(&self, scenario: &Scenario) -> Stratum;
    fn domain(&self) -> ClockDomain;
    fn profile(&self) -> Option<String> {
        None
    }
    /// Why the scenario cannot run on this backend, checked before any session.
    fn unsupported(&self, scenario: &Scenario) -> Option<String>;
    fn begin(&mut self, context: &AttemptContext) -> Result<Begin>;
    fn now_us(&mut self) -> u64;
    /// The next live event, waiting no later than `until_us` (runner domain).
    fn next_event(&mut self, until_us: u64) -> Result<Option<RuntimeEvent>>;
    /// Place a live event on the runner clock, naming the method used.
    fn runner_time(&self, event: &RuntimeEvent) -> (u64, String);
    fn timing(&mut self, catalog: &StimulusCatalog, scene: &str) -> Result<SceneTiming> {
        catalog.estimate(scene)
    }
    fn inject(
        &mut self,
        step: &Step,
        scene: &str,
        timing: &SceneTiming,
        at_us: u64,
    ) -> Result<Injection>;
    /// Close the session within its bound and return the authoritative evidence.
    fn finish(&mut self, context: &AttemptContext) -> Result<Finished>;
    fn reproduce(&self, context: &AttemptContext) -> String;
}

#[derive(Clone, Debug)]
pub struct SuiteOptions {
    pub run_id: String,
    pub scenarios: Vec<String>,
    pub repetitions: u32,
    pub seed: u64,
    pub shuffle: bool,
    /// Reproduce one attempt exactly: use this attempt seed instead of deriving it.
    pub attempt_seed: Option<u64>,
    /// Evaluator self-test results recorded in the run's first ledger row.
    pub self_test: Value,
}

/// Runtime events seen live, in receipt order.
#[derive(Default)]
struct LiveView {
    events: Vec<(RuntimeEvent, u64, String)>,
    max_turn: u64,
    ended: bool,
}

impl LiveView {
    fn observe(&mut self, event: RuntimeEvent, runner_us: u64, method: String) {
        if let Some(turn) = event.turn {
            self.max_turn = self.max_turn.max(turn);
        }
        if matches!(event.kind, EventKind::RunEnd { .. }) {
            self.ended = true;
        }
        self.events.push((event, runner_us, method));
    }

    /// The first matching event received after `from_index` that belongs to a
    /// turn newer than every turn known when the previous step started.
    fn find(
        &self,
        after: TriggerEvent,
        known_turn: u64,
        from_index: usize,
    ) -> Option<TriggerObservation> {
        self.events[from_index.min(self.events.len())..]
            .iter()
            .find(|(event, _, _)| {
                let kind = matches!(
                    (&event.kind, after),
                    (EventKind::ListeningReady, TriggerEvent::ListeningReady)
                        | (
                            EventKind::SpeakerFirstWrite,
                            TriggerEvent::SpeakerFirstWrite
                        )
                        | (EventKind::SpeechRetired, TriggerEvent::SpeechRetired)
                        | (EventKind::TurnCancelled { .. }, TriggerEvent::TurnCancelled)
                );
                kind && (after == TriggerEvent::ListeningReady
                    || event.turn.is_none_or(|turn| turn > known_turn))
            })
            .map(|(event, runner_us, method)| TriggerObservation {
                event: after,
                turn: event.turn,
                event_at_us: event.at_us,
                event_domain: event.domain,
                runner_us: *runner_us,
                runner_time_method: method.clone(),
            })
    }

    /// Whether the reply has started and has no terminal event yet.
    fn speaking(&self, turn: Option<u64>) -> bool {
        let Some(turn) = turn else { return false };
        let mut started = false;
        for (event, _, _) in &self.events {
            if event.turn != Some(turn) {
                continue;
            }
            match event.kind {
                EventKind::SpeakerFirstWrite => started = true,
                EventKind::SpeechRetired
                | EventKind::TurnCancelled { .. }
                | EventKind::TurnCompleted { .. } => return false,
                _ => {}
            }
        }
        started && !self.ended
    }
}

fn observe_next(backend: &mut dyn Backend, view: &mut LiveView, until_us: u64) -> Result<bool> {
    match backend.next_event(until_us)? {
        Some(mut event) => {
            if event.received_us.is_none() {
                event.received_us = Some(backend.now_us());
            }
            let (runner_us, method) = backend.runner_time(&event);
            view.observe(event, runner_us, method);
            Ok(true)
        }
        None => Ok(false),
    }
}

/// Execute the steps of one attempt. Returns step records, live events and an
/// abort reason when the runner itself could not continue.
fn execute(
    backend: &mut dyn Backend,
    scenario: &Scenario,
    timings: &[Option<SceneTiming>],
    steps: &mut [StepRecord],
    view: &mut LiveView,
) -> Result<()> {
    let mut anchor_us = backend.now_us();
    let mut anchor_index = 0;
    let mut known_turn = 0;
    let mut index = 0;
    while index < scenario.steps.len() {
        let step = &scenario.steps[index];
        let record = &mut steps[index];
        let deadline_us = anchor_us + u64::from(step.trigger.deadline_ms) * 1000;
        let observation = loop {
            if let Some(found) = view.find(step.trigger.after, known_turn, anchor_index) {
                break Some(found);
            }
            if view.ended || !observe_next(backend, view, deadline_us)? {
                break None;
            }
        };
        let Some(observation) = observation else {
            record.status = if view.ended {
                StepStatus::SessionEnded
            } else {
                StepStatus::TriggerMissed
            };
            record.detail = Some(format!(
                "no {:?} within {} ms",
                step.trigger.after, step.trigger.deadline_ms
            ));
            return Ok(());
        };
        let target_us = observation.runner_us + u64::from(step.trigger.delay_ms) * 1000;
        record.bound_turn = observation.turn;
        record.trigger = Some(observation);
        record.target_us = Some(target_us);
        while !view.ended && backend.now_us() < target_us && observe_next(backend, view, target_us)?
        {
        }
        if view.ended {
            record.status = StepStatus::SessionEnded;
            return Ok(());
        }
        let now_us = backend.now_us();
        if now_us > target_us + MAX_TRIGGER_LATENESS_US {
            record.status = StepStatus::TriggerStale;
            record.detail = Some(format!(
                "planned start passed {} ms before the trigger could act",
                (now_us - target_us) / 1000
            ));
            return Ok(());
        }
        if step.trigger.require_lamp_speaking && !view.speaking(record.bound_turn) {
            // The overlap condition no longer holds; playing now would test
            // something else, so nothing is played and the gap is recorded.
            record.status = StepStatus::PreconditionLost;
            record.detail = Some("the triggering reply was no longer playing".into());
            return Ok(());
        }
        let started_us = match (&step.scene, &timings[index]) {
            (Some(scene), Some(timing)) => {
                let injection = backend.inject(step, scene, timing, target_us)?;
                record.timing = Some(timing.clone());
                record.receipt = injection.receipt;
                injection.started_us
            }
            _ => backend.now_us(),
        };
        record.started_us = Some(started_us);
        record.status = StepStatus::Injected;
        anchor_us = started_us.max(target_us);
        anchor_index = view.events.len();
        known_turn = view.max_turn;
        index += 1;
    }
    let until = anchor_us + u64::from(scenario.observe_ms) * 1000;
    while !view.ended && observe_next(backend, view, until)? {}
    Ok(())
}

pub fn run_attempt(
    backend: &mut dyn Backend,
    catalog: &StimulusCatalog,
    context: &AttemptContext,
) -> Result<AttemptRecord> {
    let scenario = context.scenario;
    let mut steps: Vec<StepRecord> = scenario
        .steps
        .iter()
        .map(|step| StepRecord::planned(step, StepStatus::Withheld))
        .collect();
    let mut record = AttemptRecord {
        attempt_id: context.attempt_id.clone(),
        run_id: context.run_id.clone(),
        scenario: scenario.id.clone(),
        cohort: scenario.cohort,
        capability: scenario.capability,
        provider: scenario.provider,
        stratum: backend.stratum(scenario),
        repetition: context.repetition,
        order: context.order,
        profile: backend.profile(),
        seed: Some(context.seed),
        step_domain: backend.domain(),
        steps: Vec::new(),
        events: Vec::new(),
        live_events: Vec::new(),
        clock: None,
        attribution: Attribution::Timed,
        status: AttemptStatus::Completed,
        spoken_failure_notice: None,
        evidence: Value::Null,
        unmapped_trace_records: 0,
        reproduce: backend.reproduce(context),
    };
    if let Some(reason) = backend.unsupported(scenario) {
        for step in &mut steps {
            step.status = StepStatus::TriggerUnavailable;
        }
        record.steps = steps;
        record.status = AttemptStatus::Withheld { reason };
        return Ok(record);
    }
    // Stimulus timing reads and verifies cached audio; do it before the session
    // so that work never sits between a trigger and its stimulus.
    let mut timings = Vec::with_capacity(scenario.steps.len());
    for step in &scenario.steps {
        match step
            .scene
            .as_deref()
            .map(|scene| backend.timing(catalog, scene))
            .transpose()
        {
            Ok(timing) => timings.push(timing),
            Err(error) => {
                record.steps = steps;
                record.status = AttemptStatus::Withheld {
                    reason: format!("stimulus timing unavailable: {error}"),
                };
                return Ok(record);
            }
        }
    }
    match backend.begin(context) {
        Ok(Begin::Ready) => {}
        Ok(Begin::Withheld(reason)) => {
            record.steps = steps;
            record.status = AttemptStatus::Withheld { reason };
            return Ok(record);
        }
        Err(error) => {
            // Release whatever the backend started; keep the attempt in the ledger.
            let _ = backend.finish(context);
            record.steps = steps;
            record.status = AttemptStatus::Aborted {
                reason: format!("session start failed: {error}"),
            };
            return Ok(record);
        }
    }
    let mut view = LiveView::default();
    let executed = execute(backend, scenario, &timings, &mut steps, &mut view);
    // Always close the session, even after a runner error, so it stays bounded.
    let finished = backend.finish(context);
    record.steps = steps;
    record.live_events = view.events.into_iter().map(|(event, _, _)| event).collect();
    match finished {
        Ok(finished) => {
            record.events = finished.events;
            record.clock = finished.clock;
            record.evidence = finished.evidence;
            record.unmapped_trace_records = finished.unmapped;
            record.spoken_failure_notice = finished.spoken_failure_notice;
            for (step_id, reason) in finished.failed_deliveries {
                if let Some(step) = record.steps.iter_mut().find(|s| s.step == step_id) {
                    step.status = StepStatus::DeliveryFailed;
                    step.detail = Some(reason);
                }
            }
            for (step_id, started) in finished.corrected_starts {
                if let Some(step) = record.steps.iter_mut().find(|s| s.step == step_id) {
                    step.receipt["runner_start_us"] = json!(step.started_us);
                    step.started_us = Some(started);
                }
            }
            if let Some(reason) = finished.aborted {
                record.status = AttemptStatus::Aborted { reason };
            }
        }
        Err(error) => {
            record.status = AttemptStatus::Aborted {
                reason: format!("session close failed: {error}"),
            };
        }
    }
    if let Err(error) = executed {
        record.status = AttemptStatus::Aborted {
            reason: format!("runner failed during steps: {error}"),
        };
    }
    Ok(record)
}

/// The deterministic attempt order for a suite: each repetition is one block,
/// optionally shuffled with a seed derived from the run seed.
pub fn attempt_order(plan: &LoadedPlan, options: &SuiteOptions) -> Result<Vec<(u32, String)>> {
    let selected: Vec<String> = if options.scenarios.is_empty() {
        plan.plan.scenarios.iter().map(|s| s.id.clone()).collect()
    } else {
        let mut seen = BTreeSet::new();
        for id in &options.scenarios {
            plan.scenario(id)?;
            if !seen.insert(id) {
                return Err(crate::invalid(&format!("scenario {id} selected twice")));
            }
        }
        options.scenarios.clone()
    };
    if !(1..=1000).contains(&options.repetitions) {
        return Err(crate::invalid("repetitions must be 1..1000"));
    }
    let mut order = Vec::new();
    for repetition in 0..options.repetitions {
        let mut block = selected.clone();
        if options.shuffle {
            Rng::new(derive_seed(options.seed, &format!("order-{repetition}"))).shuffle(&mut block);
        }
        order.extend(block.into_iter().map(|id| (repetition, id)));
    }
    Ok(order)
}

/// Run every selected attempt and retain all of them in the ledger.
pub fn run_suite(
    plan: &LoadedPlan,
    catalog: &StimulusCatalog,
    backend: &mut dyn Backend,
    options: &SuiteOptions,
    run_directory: &std::path::Path,
) -> Result<Vec<AttemptRecord>> {
    let order = attempt_order(plan, options)?;
    let mut ledger = Ledger::create(run_directory)?;
    ledger.append(
        "run_start",
        json!({
            "run_id": options.run_id,
            "plan_id": plan.plan.id,
            "plan_sha256": plan.sha256,
            "desk_catalog_file_sha256": catalog.desk_file_sha256,
            "stimulus_extension_file_sha256": catalog.extension_file_sha256,
            "merged_catalog_sha256": catalog.merged_sha256,
            "backend": backend.describe(),
            "seed": options.seed,
            "repetitions": options.repetitions,
            "shuffle": options.shuffle,
            "attempt_order": order,
            "evaluator_self_test": options.self_test,
            "tool": {"package": env!("CARGO_PKG_NAME"), "version": env!("CARGO_PKG_VERSION"),
                     "os": std::env::consts::OS, "arch": std::env::consts::ARCH},
            "started_unix_ms": unix_ms(),
        }),
        true,
    )?;
    let mut records = Vec::with_capacity(order.len());
    for (order_index, (repetition, scenario_id)) in order.iter().enumerate() {
        let scenario = plan.scenario(scenario_id)?;
        let attempt_id = format!(
            "{}-{:04}-{}-r{}",
            options.run_id, order_index, scenario.id, repetition
        );
        let context = AttemptContext {
            seed: options
                .attempt_seed
                .unwrap_or_else(|| derive_seed(options.seed, &attempt_id)),
            attempt_id,
            run_id: options.run_id.clone(),
            scenario,
            repetition: *repetition,
            order: order_index as u32,
            run_directory: run_directory.to_path_buf(),
        };
        // Written and synced before any session starts or stimulus plays.
        ledger.append(
            "attempt_planned",
            json!({
                "attempt_id": context.attempt_id,
                "scenario": scenario.id,
                "cohort": scenario.cohort,
                "capability": scenario.capability,
                "repetition": repetition,
                "order": order_index,
                "stratum": backend.stratum(scenario),
                "expected": scenario.steps.iter().map(|s| json!({
                    "step": s.id, "scene": s.scene, "expect": s.expect,
                    "trigger": s.trigger, "reference": s.reference})).collect::<Vec<_>>(),
            }),
            true,
        )?;
        let record = run_attempt(backend, catalog, &context)?;
        ledger.append(
            "attempt_finished",
            json!({"attempt_id": record.attempt_id, "record": record}),
            true,
        )?;
        records.push(record);
    }
    ledger.append(
        "run_end",
        json!({"run_id": options.run_id, "attempts": records.len(), "finished_unix_ms": unix_ms()}),
        true,
    )?;
    Ok(records)
}
