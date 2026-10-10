//! Pre-registered conversation plans. A plan states stimuli, event triggers,
//! deadlines and expected behavior before any attempt runs; it is not a result.
use crate::{
    Result, invalid,
    stimulus::{StimulusCatalog, TriggerKind},
};
use lamp_acoustic::{Trigger as SceneTrigger, hash};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

pub const PLAN_VERSION: u32 = 1;
pub const DEFAULT_PLAN: &str = include_str!("../../../fixtures/voice-eval-v1.json");
/// Startup before readiness is outside the session; this bounds the wait for it.
pub const MAX_DEADLINE_MS: u32 = 60_000;
pub const MAX_SESSION_SECONDS: u16 = 600;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Plan {
    pub version: u32,
    pub id: String,
    /// Pre-registered conversational answer deadline. No answer by then is
    /// right-censored, not a successful answer at the deadline.
    pub answer_deadline_ms: u32,
    pub targets: Targets,
    pub fixed_reply: FixedReply,
    pub profiles: BTreeMap<String, FakeProfile>,
    pub scenarios: Vec<Scenario>,
}

/// Release targets quoted beside measured distributions; never used to pass a run.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Targets {
    pub answer_p50_ms: u32,
    pub answer_p95_ms: u32,
    pub yield_p95_ms: u32,
}

/// Identity of the cached Gemini reply used by `lamp-live directed-fixture`.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixedReply {
    pub text: String,
    pub frames: u32,
    pub rate_hz: u32,
    pub sha256: String,
}
impl FixedReply {
    pub fn duration_ms(&self) -> u32 {
        (u64::from(self.frames) * 1000 / u64::from(self.rate_hz.max(1))) as u32
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Cohort {
    QuickChat,
    FollowUp,
    ShortAnswer,
    LongAnswer,
    TopicChange,
    Hesitation,
    Correction,
    QuietSpeech,
    RapidSpeech,
    Acknowledgment,
    TwoPeople,
    ComputerAudio,
    Noise,
    EchoOnly,
    ProviderDelay,
    ProviderFailure,
    ProviderDisconnect,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Capability {
    Conversation,
    Interruption,
    Acknowledgment,
    Restraint,
    Robustness,
    Echo,
    ProviderResilience,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderKind {
    /// Live provider: every admitted turn may receive a generated answer.
    Gemini,
    /// `lamp-live directed-fixture`: only the first admitted owner receives the
    /// cached reply; later turns complete without audio.
    FixedReply,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Scenario {
    pub id: String,
    pub cohort: Cohort,
    pub capability: Capability,
    pub description: String,
    pub provider: ProviderKind,
    /// False when the scenario needs fault injection that the physical runtime
    /// does not expose. Such scenarios run only against the fake runtime.
    pub physical: bool,
    /// The finite `lamp-live` session length after listening readiness.
    pub session_seconds: u16,
    /// Bounded observation after the last step before the attempt is closed.
    pub observe_ms: u32,
    pub steps: Vec<Step>,
    #[serde(default)]
    pub fake_replies: Vec<FakeReply>,
    #[serde(default)]
    pub fake_faults: Vec<ProviderFault>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Step {
    pub id: String,
    /// Scene in the merged stimulus catalog; `None` is an observation-only step.
    pub scene: Option<String>,
    pub trigger: Trigger,
    pub expect: Expectation,
    /// Reviewer hint for correctness; never scored automatically.
    pub reference: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TriggerEvent {
    /// Runtime readiness: input can actually be retained.
    ListeningReady,
    /// First ALSA-accepted reply write of the turn answering the previous step.
    SpeakerFirstWrite,
    /// ALSA retirement of that reply's final speech sample.
    SpeechRetired,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Trigger {
    pub after: TriggerEvent,
    pub delay_ms: u32,
    /// Bounded wait for the triggering event, measured from the previous anchor.
    pub deadline_ms: u32,
    /// Overlap precondition: the triggering reply must still be playing when the
    /// stimulus starts. If it is not, the step is withheld rather than injected.
    #[serde(default)]
    pub require_lamp_speaking: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Expectation {
    /// Admitted as exactly one turn that receives a complete answer.
    Answer,
    /// The playing answer yields to this stimulus, which then gets an answer.
    InterruptAndAnswer,
    /// The playing answer continues and completes; this stimulus is not a turn.
    NoInterrupt,
    /// No admission and no response.
    Silence,
    /// Observation window over Lamp's own playback: no admission may occur.
    Observe,
    /// An explicit failure outcome with no fabricated completion, plus a spoken
    /// notice to the person.
    HonestFailure,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FakeReply {
    pub text: String,
    pub speech_ms: u32,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum ProviderFault {
    FirstAudioDelay {
        turn: u64,
        delay_ms: u32,
    },
    SupplyGap {
        turn: u64,
        after_audio_ms: u32,
        gap_ms: u32,
    },
    FailBeforeAudio {
        turn: u64,
        after_endpoint_ms: u32,
    },
    DisconnectMidReply {
        turn: u64,
        after_audio_ms: u32,
    },
    SpuriousInterrupt {
        turn: u64,
        after_first_write_ms: u32,
    },
}
impl ProviderFault {
    pub fn turn(&self) -> u64 {
        match self {
            Self::FirstAudioDelay { turn, .. }
            | Self::SupplyGap { turn, .. }
            | Self::FailBeforeAudio { turn, .. }
            | Self::DisconnectMidReply { turn, .. }
            | Self::SpuriousInterrupt { turn, .. } => *turn,
        }
    }
    pub fn ends_run(&self) -> bool {
        matches!(
            self,
            Self::FailBeforeAudio { .. } | Self::DisconnectMidReply { .. }
        )
    }
}

/// Timing and fault parameters for the fake runtime. These describe a policy
/// simulation; they are not measurements of the physical Lamp.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FakeProfile {
    pub description: String,
    pub startup_ms: u32,
    pub first_audio_ms: u32,
    pub first_audio_jitter_ms: u32,
    pub fixed_reply_first_audio_ms: u32,
    /// Provider generation rate relative to real time (100 = real time).
    pub generation_speed_percent: u32,
    /// Audio delivered at once with the first provider packet.
    pub first_burst_ms: u32,
    pub speaker_dispatch_ms: u32,
    pub alsa_delay_ms: u32,
    pub cancel_tail_ms: u32,
    pub echo: EchoModel,
    /// Declared speech probability while a timing-mode stimulus is active.
    pub timing_speech_probability: f32,
    pub timing_silence_probability: f32,
    /// The current runtime has no audible failure message; keep this false
    /// until the runtime implements one.
    pub spoken_failure_notice: bool,
    /// Emit ring requests like `lamp-live --ring-channel-ceiling` (phase
    /// changes only; the real policy also renews each static cue every 20 ms).
    #[serde(default)]
    pub ring_cues: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum EchoModel {
    None,
    /// Inject high speech probability for `duration_ms`, `after_first_write_ms`
    /// after a reply's first write: only the first reply of the attempt, or
    /// every reply when `every_reply` is set (a self-sustaining echo loop).
    /// Reproduces a failure class; it does not estimate how often real residual
    /// echo occurs.
    Leak {
        after_first_write_ms: u32,
        duration_ms: u32,
        every_reply: bool,
    },
}

pub fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 80
        && id
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}

/// A parsed plan with its exact source identity.
#[derive(Clone, Debug)]
pub struct LoadedPlan {
    pub plan: Plan,
    pub sha256: String,
}

impl LoadedPlan {
    pub fn parse(source: &str, catalog: &StimulusCatalog) -> Result<Self> {
        if source.len() > 1_000_000 {
            return Err(invalid("plan exceeds 1 MB"));
        }
        let plan: Plan = serde_json::from_str(source)?;
        plan.validate(catalog)?;
        Ok(Self {
            plan,
            sha256: hash(source.as_bytes()),
        })
    }

    pub fn default_plan(catalog: &StimulusCatalog) -> Result<Self> {
        Self::parse(DEFAULT_PLAN, catalog)
    }

    pub fn scenario(&self, id: &str) -> Result<&Scenario> {
        self.plan
            .scenarios
            .iter()
            .find(|scenario| scenario.id == id)
            .ok_or_else(|| invalid(&format!("unknown scenario {id}")))
    }

    pub fn profile(&self, id: &str) -> Result<&FakeProfile> {
        self.plan
            .profiles
            .get(id)
            .ok_or_else(|| invalid(&format!("unknown fake profile {id}")))
    }
}

impl Plan {
    pub fn validate(&self, catalog: &StimulusCatalog) -> Result<()> {
        if self.version != PLAN_VERSION || !valid_id(&self.id) {
            return Err(invalid("unsupported plan version or invalid plan id"));
        }
        if !(1_000..=60_000).contains(&self.answer_deadline_ms) {
            return Err(invalid("answer deadline must be 1..60 s"));
        }
        if self.fixed_reply.sha256.len() != 64
            || self.fixed_reply.rate_hz == 0
            || self.fixed_reply.frames == 0
            || self.fixed_reply.text.is_empty()
        {
            return Err(invalid("invalid fixed reply identity"));
        }
        if self.profiles.is_empty() || self.profiles.len() > 32 {
            return Err(invalid("a plan needs 1..32 fake profiles"));
        }
        for (id, profile) in &self.profiles {
            if !valid_id(id) {
                return Err(invalid(&format!("invalid profile id {id}")));
            }
            profile.validate(id)?;
        }
        if self.scenarios.is_empty() || self.scenarios.len() > 256 {
            return Err(invalid("a plan needs 1..256 scenarios"));
        }
        let mut ids = BTreeSet::new();
        for scenario in &self.scenarios {
            if !ids.insert(scenario.id.as_str()) {
                return Err(invalid(&format!("duplicate scenario {}", scenario.id)));
            }
            scenario.validate(catalog)?;
        }
        Ok(())
    }
}

impl FakeProfile {
    fn validate(&self, id: &str) -> Result<()> {
        let probability = |value: f32| value.is_finite() && (0.0..=1.0).contains(&value);
        let bounded = [
            self.startup_ms,
            self.first_audio_ms,
            self.first_audio_jitter_ms,
            self.fixed_reply_first_audio_ms,
            self.speaker_dispatch_ms,
            self.alsa_delay_ms,
            self.cancel_tail_ms,
            self.first_burst_ms,
        ]
        .iter()
        .all(|&value| value <= MAX_DEADLINE_MS);
        if !bounded
            || !(50..=1_000).contains(&self.generation_speed_percent)
            || !probability(self.timing_speech_probability)
            || !probability(self.timing_silence_probability)
            || self.description.is_empty()
        {
            return Err(invalid(&format!("invalid fake profile {id}")));
        }
        if let EchoModel::Leak {
            after_first_write_ms,
            duration_ms,
            ..
        } = self.echo
            && (after_first_write_ms > MAX_DEADLINE_MS || !(10..=5_000).contains(&duration_ms))
        {
            return Err(invalid(&format!("invalid echo leak in {id}")));
        }
        Ok(())
    }
}

impl Scenario {
    fn validate(&self, catalog: &StimulusCatalog) -> Result<()> {
        let id = &self.id;
        let fail = |what: &str| invalid(&format!("scenario {id}: {what}"));
        if !valid_id(id) || self.description.is_empty() {
            return Err(fail("invalid id or description"));
        }
        if self.steps.is_empty() || self.steps.len() > 8 {
            return Err(fail("needs 1..8 steps"));
        }
        if !(5..=MAX_SESSION_SECONDS).contains(&self.session_seconds)
            || self.observe_ms > MAX_DEADLINE_MS
        {
            return Err(fail(
                "session must be 5..600 s and observation at most 60 s",
            ));
        }
        if self.fake_replies.len() > 16
            || self
                .fake_replies
                .iter()
                .any(|r| r.text.is_empty() || !(200..=60_000).contains(&r.speech_ms))
        {
            return Err(fail("invalid fake replies"));
        }
        if self.fake_faults.len() > 4 || self.fake_faults.iter().any(|f| f.turn() == 0) {
            return Err(fail("invalid fake faults"));
        }
        if !self.fake_faults.is_empty() && self.physical {
            return Err(fail("provider faults cannot be injected physically"));
        }
        if self.provider == ProviderKind::FixedReply && !self.fake_replies.is_empty() {
            return Err(fail("the fixed reply provider has no scripted replies"));
        }
        let mut step_ids = BTreeSet::new();
        let mut budget = 0_u64;
        for (index, step) in self.steps.iter().enumerate() {
            if !valid_id(&step.id) || !step_ids.insert(step.id.as_str()) {
                return Err(fail("invalid or duplicate step id"));
            }
            if step.reference.is_empty() {
                return Err(fail("every step needs a reviewer reference"));
            }
            let trigger = &step.trigger;
            if trigger.deadline_ms == 0
                || trigger.deadline_ms > MAX_DEADLINE_MS
                || trigger.delay_ms > MAX_DEADLINE_MS
            {
                return Err(fail("trigger delay/deadline must be bounded to 60 s"));
            }
            // The first step anchors on readiness; later steps anchor on the
            // reply to an earlier step, so readiness cannot recur.
            if (index == 0) != (trigger.after == TriggerEvent::ListeningReady) {
                return Err(fail("only the first step may follow listening_ready"));
            }
            if trigger.require_lamp_speaking && trigger.after != TriggerEvent::SpeakerFirstWrite {
                return Err(fail(
                    "an overlap precondition needs a speaker_first_write trigger",
                ));
            }
            match step.expect {
                Expectation::InterruptAndAnswer | Expectation::NoInterrupt => {
                    if !trigger.require_lamp_speaking || step.scene.is_none() {
                        return Err(fail(
                            "overlap expectations need speech and require_lamp_speaking",
                        ));
                    }
                }
                Expectation::Observe => {
                    if step.scene.is_some() || trigger.after != TriggerEvent::SpeakerFirstWrite {
                        return Err(fail(
                            "observe steps play nothing and follow speaker_first_write",
                        ));
                    }
                }
                Expectation::HonestFailure => {
                    if self.fake_faults.is_empty() || step.scene.is_none() {
                        return Err(fail("honest_failure needs a stimulus and a provider fault"));
                    }
                }
                Expectation::Answer | Expectation::Silence => {
                    if step.scene.is_none() {
                        return Err(fail("answer/silence steps need a stimulus"));
                    }
                }
            }
            if let Some(scene_id) = &step.scene {
                let scene = catalog.scene(scene_id)?;
                // A scene's declared trigger documents its intended use. Keep the
                // executed plan consistent with it rather than silently diverging.
                let expected = match scene.trigger {
                    SceneTrigger::SceneStart => (TriggerKind::SceneStart, 0),
                    SceneTrigger::LampSpeechStarted { delay_ms } => {
                        (TriggerKind::LampSpeechStarted, delay_ms)
                    }
                    SceneTrigger::LampSpeechEnded { delay_ms } => {
                        (TriggerKind::LampSpeechEnded, delay_ms)
                    }
                };
                let actual = match trigger.after {
                    TriggerEvent::ListeningReady => (TriggerKind::SceneStart, 0),
                    TriggerEvent::SpeakerFirstWrite => {
                        (TriggerKind::LampSpeechStarted, trigger.delay_ms)
                    }
                    TriggerEvent::SpeechRetired => (TriggerKind::LampSpeechEnded, trigger.delay_ms),
                };
                if expected != actual {
                    return Err(fail(&format!(
                        "step {} trigger disagrees with scene {scene_id}",
                        step.id
                    )));
                }
                budget += u64::from(scene.duration_ms).min(10_000);
            }
            budget += u64::from(trigger.delay_ms);
        }
        if !self.steps.iter().any(|step| step.scene.is_some()) {
            return Err(fail("at least one step must play a stimulus"));
        }
        // Waiting for events is bounded by each deadline and by the session end
        // at run time; the fixed delays, stimuli and observation must fit.
        budget += u64::from(self.observe_ms);
        if budget > u64::from(self.session_seconds) * 1000 {
            return Err(fail(
                "fixed delays, stimuli and observation exceed the session",
            ));
        }
        Ok(())
    }

    pub fn uses_events_after_readiness(&self) -> bool {
        self.steps
            .iter()
            .any(|step| step.trigger.after != TriggerEvent::ListeningReady)
    }
}
