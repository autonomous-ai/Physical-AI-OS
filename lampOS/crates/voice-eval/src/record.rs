//! What one attempt actually did, as retained in the ledger. Step times are in
//! the runner's clock domain; runtime events keep their own domain.
use crate::{
    events::{ClockDomain, RuntimeEvent},
    plan::{Capability, Cohort, Expectation, ProviderKind, TriggerEvent},
    stimulus::SceneTiming,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Evidence strata. They are reported separately and never pooled.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Stratum {
    /// Fake runtime with declared speech-activity intervals.
    FakeTiming,
    /// Fake runtime with lamp-live's VAD on the exact cached digital mixes.
    FakeSignal,
    /// Real Lamp, `lamp-live directed-fixture`; source recorded separately.
    PhysicalFixture,
    /// Real Lamp, `lamp-live directed` with Gemini; source recorded separately.
    PhysicalGemini,
    /// An existing lamp-live trace scored against a declared scenario.
    ImportedTrace,
}
impl Stratum {
    pub fn is_physical(self) -> bool {
        matches!(
            self,
            Self::PhysicalFixture | Self::PhysicalGemini | Self::ImportedTrace
        )
    }
    pub fn label(self) -> &'static str {
        match self {
            Self::FakeTiming => "fake runtime, declared timing (policy simulation; not acoustic)",
            Self::FakeSignal => {
                "fake runtime, digital cached audio through lamp-live VAD (not room audio)"
            }
            Self::PhysicalFixture => "physical Lamp, cached fixed reply",
            Self::PhysicalGemini => "physical Lamp, Gemini",
            Self::ImportedTrace => "imported lamp-live trace, declared attribution",
        }
    }
}

/// Where the person's speech came from. Sources are never pooled: Jieli's
/// onboard processing may treat loudspeaker replay and a person differently.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StimulusSource {
    /// Fake runtime with declared speech intervals; nothing is audible.
    DeclaredTiming,
    /// Fake runtime fed the exact cached digital mix; nothing is audible.
    DigitalMix,
    /// Cached synthetic voices played on the iMac loudspeaker.
    LoudspeakerSynthetic,
    /// A person speaking prompted lines in the room.
    DirectHuman,
    /// Not recorded (for example an imported historical trace).
    #[default]
    Unknown,
}
impl StimulusSource {
    pub fn label(self) -> &'static str {
        match self {
            Self::DeclaredTiming => "declared timing",
            Self::DigitalMix => "digital cached mix",
            Self::LoudspeakerSynthetic => "synthetic voices on a loudspeaker",
            Self::DirectHuman => "direct human speech",
            Self::Unknown => "stimulus source not recorded",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StepStatus {
    /// The stimulus started (or the observation window opened).
    Injected,
    /// The triggering event did not arrive within the step deadline.
    TriggerMissed,
    /// The overlap precondition was false at injection time; nothing played.
    PreconditionLost,
    /// The runtime session ended before the trigger.
    SessionEnded,
    /// Not attempted because an earlier step did not complete.
    Withheld,
    /// The backend cannot observe the required trigger event.
    TriggerUnavailable,
    /// The trigger arrived too late to start the stimulus at its planned time.
    TriggerStale,
    /// The player reported that the stimulus was not delivered.
    DeliveryFailed,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct TriggerObservation {
    pub event: TriggerEvent,
    pub turn: Option<u64>,
    /// Event time in its own domain.
    pub event_at_us: Option<u64>,
    pub event_domain: ClockDomain,
    /// The same event placed in the runner domain (mapped or receipt time).
    pub runner_us: u64,
    pub runner_time_method: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct StepRecord {
    pub step: String,
    pub expect: Expectation,
    pub scene: Option<String>,
    pub status: StepStatus,
    pub trigger: Option<TriggerObservation>,
    /// Intended start in the runner domain.
    pub target_us: Option<u64>,
    /// Best evidence of the actual start in the runner domain. For playback
    /// this is the player's start request, not acoustic onset.
    pub started_us: Option<u64>,
    /// The reply that was playing (overlap steps) or that triggered the step.
    pub bound_turn: Option<u64>,
    pub timing: Option<SceneTiming>,
    #[serde(default)]
    pub receipt: Value,
    #[serde(default)]
    pub detail: Option<String>,
    /// Utterances addressed to Lamp in a multi-talker scene.
    #[serde(default)]
    pub addressed: Vec<String>,
}
impl StepRecord {
    pub fn planned(step: &crate::plan::Step, status: StepStatus) -> Self {
        Self {
            step: step.id.clone(),
            expect: step.expect,
            scene: step.scene.clone(),
            status,
            trigger: None,
            target_us: None,
            started_us: None,
            bound_turn: None,
            timing: None,
            receipt: Value::Null,
            detail: None,
            addressed: step.addressed.clone(),
        }
    }
}

/// `event_time = runner_time + offset_us`, with a symmetric uncertainty.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct ClockMapping {
    pub from: ClockDomain,
    pub to: ClockDomain,
    pub offset_us: i64,
    pub uncertainty_us: u64,
    /// Expected stimulus delay from start request to the runtime's microphone
    /// (output buffering, air, input buffering). An allowance, not a measurement.
    pub path_allowance_us: u64,
    pub method: String,
}
impl ClockMapping {
    pub fn identity(domain: ClockDomain) -> Self {
        Self {
            from: domain,
            to: domain,
            offset_us: 0,
            uncertainty_us: 0,
            path_allowance_us: 0,
            method: "same clock".into(),
        }
    }
    pub fn to_event(&self, runner_us: u64) -> u64 {
        runner_us.saturating_add_signed(self.offset_us)
    }
    pub fn to_runner(&self, event_us: u64) -> u64 {
        event_us.saturating_add_signed(-self.offset_us)
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum AttemptStatus {
    Completed,
    /// Not run; the reason is retained and the attempt stays in the ledger.
    Withheld {
        reason: String,
    },
    /// Started but the runner or backend failed; partial evidence is kept.
    Aborted {
        reason: String,
    },
}

/// How admitted turns are related to stimuli.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(tag = "mode", rename_all = "snake_case")]
pub enum Attribution {
    /// Stimulus times mapped into the event clock domain.
    Timed,
    /// Declared `step -> admitted turn` pairs, for traces without stimulus times.
    /// `absent` lists stimulus steps declared to have produced no admission.
    Declared {
        turns: Vec<(String, u64)>,
        #[serde(default)]
        absent: Vec<String>,
    },
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AttemptRecord {
    pub attempt_id: String,
    pub run_id: String,
    pub scenario: String,
    pub cohort: Cohort,
    pub capability: Capability,
    pub provider: ProviderKind,
    pub stratum: Stratum,
    #[serde(default)]
    pub source: StimulusSource,
    pub repetition: u32,
    pub order: u32,
    pub profile: Option<String>,
    pub seed: Option<u64>,
    pub step_domain: ClockDomain,
    pub steps: Vec<StepRecord>,
    /// Authoritative runtime events (fake output or the final lamp-live trace).
    pub events: Vec<RuntimeEvent>,
    /// Events seen live for triggering (cues/stdout), retained for audit.
    #[serde(default)]
    pub live_events: Vec<RuntimeEvent>,
    pub clock: Option<ClockMapping>,
    pub attribution: Attribution,
    pub status: AttemptStatus,
    /// Whether the runtime can speak a failure notice. `None` means unknown
    /// in software and requires listening.
    pub spoken_failure_notice: Option<bool>,
    /// Backend evidence: trace hashes, player/recorder reports, room audio.
    #[serde(default)]
    pub evidence: Value,
    #[serde(default)]
    pub unmapped_trace_records: usize,
    /// Command that reproduces this attempt.
    pub reproduce: String,
}
