//! Bounded input candidates before destructive turn replacement.
//!
//! This is sequencing and retention, not an echo/addressee classifier. The
//! directed harness explicitly accepts VAD-only evidence. No provider event or
//! session-scoped transcript is converted into a candidate decision here.
use crate::activity::{Activity, ObservedAudio, PRE_ROLL_BLOCKS};
use lamp_interaction::{BootId, MonoTime, Snapshot, TurnOwner};
use serde::Serialize;
use std::io;

pub const DECISION_BUDGET_US: u64 = 200_000;
pub const MAX_RETAINED_AGE_US: u64 = 600_000;
pub const MAX_EVIDENCE_AGE_US: u64 = 100_000;
pub const MAX_DECISION_BLOCKS: usize = 20;
pub const MAX_CANDIDATE_BLOCKS: usize = PRE_ROLL_BLOCKS + MAX_DECISION_BLOCKS;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct CaptureLineage {
    pub worker: BootId,
    pub epoch: u64,
    pub dsp_epoch: u64,
    pub privacy_generation: u64,
}

#[derive(Clone, Copy, Debug)]
pub struct Context {
    pub capture: CaptureLineage,
    pub authority: Snapshot,
}
impl Context {
    fn ready(self, at_us: u64) -> bool {
        self.capture.epoch > 0
            && self.capture.dsp_epoch > 0
            && self.capture.privacy_generation == self.authority.microphone_generation()
            && self.authority.listening_ready(MonoTime::from_micros(at_us))
    }
}

/// Immutable across additional captured blocks. Reset never reuses a serial.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct CandidateId {
    controller: BootId,
    serial: u64,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct Candidate {
    pub id: CandidateId,
    pub capture: CaptureLineage,
    pub displaced_owner: Option<TurnOwner>,
    pub trigger_sequence: u64,
    pub trigger_read_at_us: u64,
    pub first_read_at_us: u64,
    pub decision_deadline_us: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AcceptanceBasis {
    /// Explicitly opened qualification session; not social/echo recognition.
    DirectedSessionVadOnly,
    /// A caller's classifier decision, not a correctness guarantee. There is
    /// no production classifier wired to this variant in the directed runtime.
    ClassifierDecision,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RejectReason {
    Policy,
    Deadline,
    Capacity,
    CaptureChanged,
    AuthorityChanged,
    InputUnavailable,
}

#[derive(Clone, Copy, Debug)]
pub enum Verdict {
    Accept(AcceptanceBasis),
    Reject,
}

/// Evidence must be explicitly correlated before construction. `through_sequence`
/// identifies retained audio; receipt time cannot freshen old observations.
#[derive(Clone, Copy, Debug)]
pub struct Evidence {
    pub candidate: CandidateId,
    pub through_sequence: u64,
    pub produced_at_us: u64,
    pub verdict: Verdict,
}

#[derive(Debug)]
pub struct Accepted {
    pub candidate: Candidate,
    pub basis: AcceptanceBasis,
    pub audio: Vec<ObservedAudio>,
    pub ended: bool,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct Rejection {
    pub candidate: CandidateId,
    pub reason: RejectReason,
}

#[derive(Debug)]
pub enum Decision {
    Accepted(Accepted),
    Rejected(Rejection),
    /// Old, future, unbound or already consumed evidence has no authority.
    Ignored,
}

#[derive(Debug)]
pub enum Step {
    None,
    Candidate(Candidate),
    Audio(ObservedAudio),
    End(ObservedAudio),
    Rejected(Rejection),
}

/// At most one older candidate can expire before this activity is handled.
/// Retain both events when an ended candidate expires as a new utterance starts.
#[derive(Debug)]
pub struct Update {
    pub rejection: Option<Rejection>,
    pub step: Step,
}

struct Pending {
    candidate: Candidate,
    expected_generation: u64,
    expected_owner: Option<TurnOwner>,
    audio: Vec<ObservedAudio>,
    ended: bool,
    decision_blocks: usize,
}

struct Streaming {
    capture: CaptureLineage,
    last_sequence: u64,
    last_read_at_us: u64,
}

enum Phase {
    Idle,
    Pending(Pending),
    Streaming(Streaming),
    Draining,
}

/// Owns at most 50 original 10 ms PCM blocks. It never calls the controller,
/// cancels speech, sends provider input, or grants a listening/actuator cue.
pub struct InputAdmission {
    phase: Phase,
    serial: u64,
    last_now_us: u64,
}
impl Default for InputAdmission {
    fn default() -> Self {
        Self {
            phase: Phase::Idle,
            serial: 0,
            last_now_us: 0,
        }
    }
}

impl InputAdmission {
    /// Reset alongside the detector after cancellation/failure. Keep identity
    /// high-water marks so a delayed decision cannot target a later candidate.
    pub fn reset(&mut self) {
        self.phase = Phase::Idle;
    }

    pub fn observe(
        &mut self,
        activity: Activity,
        context: Context,
        at_us: u64,
    ) -> io::Result<Update> {
        let result = self.observe_inner(activity, context, at_us);
        if result.is_err() {
            self.reset();
        }
        result
    }

    fn observe_inner(
        &mut self,
        activity: Activity,
        context: Context,
        at_us: u64,
    ) -> io::Result<Update> {
        let rejected = self.maintain(context, at_us)?;
        let step = match activity {
            Activity::Fault(reason) => {
                self.reset();
                return Err(io::Error::other(reason));
            }
            Activity::Quiet if matches!(self.phase, Phase::Idle) => Step::None,
            Activity::Quiet if matches!(&self.phase, Phase::Pending(pending) if pending.ended) => {
                Step::None
            }
            Activity::Start(audio) if matches!(self.phase, Phase::Idle) => {
                if !context.ready(at_us) {
                    return Err(io::Error::other("candidate input is unavailable"));
                }
                validate_prefix(&audio, at_us)?;
                let first = &audio[0];
                let trigger = audio.last().expect("validated nonempty prefix");
                self.serial = self
                    .serial
                    .checked_add(1)
                    .ok_or_else(|| io::Error::other("candidate serial exhausted"))?;
                let candidate = Candidate {
                    id: CandidateId {
                        controller: context.authority.boot(),
                        serial: self.serial,
                    },
                    capture: context.capture,
                    displaced_owner: context.authority.owner(),
                    trigger_sequence: trigger.sequence,
                    trigger_read_at_us: trigger.captured_at_us,
                    first_read_at_us: first.captured_at_us,
                    decision_deadline_us: trigger
                        .captured_at_us
                        .saturating_add(DECISION_BUDGET_US)
                        .min(first.captured_at_us.saturating_add(MAX_RETAINED_AGE_US)),
                };
                let mut retained = Vec::with_capacity(MAX_CANDIDATE_BLOCKS);
                retained.extend(audio);
                self.phase = Phase::Pending(Pending {
                    candidate,
                    expected_generation: context.authority.generation(),
                    expected_owner: context.authority.owner(),
                    audio: retained,
                    ended: false,
                    decision_blocks: 0,
                });
                if let Some(rejection) = self.maintain(context, at_us)? {
                    Step::Rejected(rejection)
                } else {
                    Step::Candidate(candidate)
                }
            }
            Activity::Continue(audio) => self.continue_audio(audio, false, context, at_us)?,
            Activity::End(audio) => self.continue_audio(audio, true, context, at_us)?,
            _ => return Err(io::Error::other("activity contradicts admission state")),
        };
        Ok(Update {
            rejection: rejected,
            step,
        })
    }

    /// Call every control tick, even when capture/evidence is quiet. Incoming
    /// frames and evidence never extend the original decision deadline.
    pub fn maintain(&mut self, context: Context, at_us: u64) -> io::Result<Option<Rejection>> {
        self.advance(at_us)?;
        if let Phase::Streaming(stream) = &self.phase {
            // A planned AEC reset may advance DSP epoch without dropping the
            // accepted utterance. Capture/privacy incarnation must not change.
            if !context.ready(at_us)
                || context.capture.worker != stream.capture.worker
                || context.capture.epoch != stream.capture.epoch
                || context.capture.privacy_generation != stream.capture.privacy_generation
                || context.capture.dsp_epoch < stream.capture.dsp_epoch
            {
                self.reset();
                return Err(io::Error::other("accepted capture lineage changed"));
            }
        }
        let Phase::Pending(pending) = &self.phase else {
            return Ok(None);
        };
        let reason = if !context.ready(at_us) {
            Some(RejectReason::InputUnavailable)
        } else if pending.candidate.capture != context.capture {
            Some(RejectReason::CaptureChanged)
        } else if pending.candidate.id.controller != context.authority.boot()
            || pending.expected_generation != context.authority.generation()
            || pending.expected_owner != context.authority.owner()
        {
            Some(RejectReason::AuthorityChanged)
        } else if at_us >= pending.candidate.decision_deadline_us {
            Some(RejectReason::Deadline)
        } else {
            None
        };
        Ok(reason.map(|reason| self.reject(reason)))
    }

    /// Natural completion is an explicit coordinator fact, not an inference
    /// from an absent owner. It can turn a pending interruption into a new turn.
    pub fn owner_completed(
        &mut self,
        completed: TurnOwner,
        authority: Snapshot,
        at_us: u64,
    ) -> io::Result<()> {
        self.advance(at_us)?;
        if let Phase::Pending(pending) = &mut self.phase
            && pending.expected_owner == Some(completed)
            && authority.boot() == pending.candidate.id.controller
            && authority.generation().checked_sub(1) == Some(pending.expected_generation)
            && authority.owner().is_none()
            && authority.microphone_generation() == pending.candidate.capture.privacy_generation
        {
            pending.expected_generation = authority.generation();
            pending.expected_owner = authority.owner();
        }
        Ok(())
    }

    pub fn decide(
        &mut self,
        evidence: Evidence,
        context: Context,
        at_us: u64,
    ) -> io::Result<Decision> {
        if let Some(rejected) = self.maintain(context, at_us)? {
            return Ok(Decision::Rejected(rejected));
        }
        let Phase::Pending(pending) = &self.phase else {
            return Ok(Decision::Ignored);
        };
        if evidence.candidate != pending.candidate.id
            || evidence.through_sequence < pending.candidate.trigger_sequence
            || evidence.produced_at_us > at_us
        {
            return Ok(Decision::Ignored);
        }
        let Some(frame) = pending
            .audio
            .iter()
            .find(|frame| frame.sequence == evidence.through_sequence)
        else {
            return Ok(Decision::Ignored);
        };
        if evidence.produced_at_us < frame.captured_at_us
            || at_us
                .checked_sub(frame.captured_at_us)
                .is_none_or(|age| age >= MAX_EVIDENCE_AGE_US)
        {
            return Ok(Decision::Ignored);
        }
        let Verdict::Accept(basis) = evidence.verdict else {
            return Ok(Decision::Rejected(self.reject(RejectReason::Policy)));
        };
        let Phase::Pending(pending) = std::mem::replace(&mut self.phase, Phase::Idle) else {
            unreachable!()
        };
        if !pending.ended {
            let last = pending.audio.last().expect("nonempty candidate");
            self.phase = Phase::Streaming(Streaming {
                capture: pending.candidate.capture,
                last_sequence: last.sequence,
                last_read_at_us: last.captured_at_us,
            });
        }
        Ok(Decision::Accepted(Accepted {
            candidate: pending.candidate,
            basis,
            audio: pending.audio,
            ended: pending.ended,
        }))
    }

    fn continue_audio(
        &mut self,
        audio: ObservedAudio,
        ended: bool,
        context: Context,
        at_us: u64,
    ) -> io::Result<Step> {
        match &mut self.phase {
            Phase::Pending(pending) => {
                if pending.ended {
                    return Err(io::Error::other("audio after candidate endpoint"));
                }
                validate_next(
                    pending.audio.last().expect("nonempty candidate"),
                    &audio,
                    at_us,
                )?;
                if pending.decision_blocks >= MAX_DECISION_BLOCKS
                    || pending.audio.len() >= MAX_CANDIDATE_BLOCKS
                {
                    let rejection = self.reject(RejectReason::Capacity);
                    if ended {
                        self.phase = Phase::Idle;
                    }
                    return Ok(Step::Rejected(rejection));
                }
                pending.audio.push(audio);
                pending.ended = ended;
                pending.decision_blocks += 1;
                Ok(Step::None)
            }
            Phase::Streaming(stream) => {
                if stream.last_sequence.checked_add(1) != Some(audio.sequence)
                    || audio.captured_at_us < stream.last_read_at_us
                    || audio.captured_at_us.saturating_sub(stream.last_read_at_us) > 50_000
                    || at_us
                        .checked_sub(audio.captured_at_us)
                        .is_none_or(|age| age >= MAX_EVIDENCE_AGE_US)
                {
                    return Err(io::Error::other("accepted capture discontinuity"));
                }
                stream.last_sequence = audio.sequence;
                stream.last_read_at_us = audio.captured_at_us;
                stream.capture = context.capture;
                if ended {
                    self.phase = Phase::Idle;
                    Ok(Step::End(audio))
                } else {
                    Ok(Step::Audio(audio))
                }
            }
            Phase::Draining => {
                if ended {
                    self.phase = Phase::Idle;
                }
                Ok(Step::None)
            }
            Phase::Idle => Err(io::Error::other("audio has no candidate or admitted owner")),
        }
    }

    fn reject(&mut self, reason: RejectReason) -> Rejection {
        let Phase::Pending(pending) = std::mem::replace(&mut self.phase, Phase::Draining) else {
            unreachable!()
        };
        if pending.ended {
            self.phase = Phase::Idle;
        }
        Rejection {
            candidate: pending.candidate.id,
            reason,
        }
    }

    fn advance(&mut self, at_us: u64) -> io::Result<()> {
        if at_us < self.last_now_us {
            self.reset();
            return Err(io::Error::other("admission clock moved backwards"));
        }
        self.last_now_us = at_us;
        Ok(())
    }
}

fn validate_prefix(audio: &[ObservedAudio], at_us: u64) -> io::Result<()> {
    if audio.is_empty()
        || audio.len() > PRE_ROLL_BLOCKS
        || audio[0].sequence == 0
        || audio[0].captured_at_us == 0
    {
        return Err(io::Error::other("invalid candidate prefix"));
    }
    for frames in audio.windows(2) {
        validate_next(&frames[0], &frames[1], at_us)?;
    }
    if at_us
        .checked_sub(audio.last().expect("nonempty prefix").captured_at_us)
        .is_none_or(|age| age >= MAX_EVIDENCE_AGE_US)
    {
        return Err(io::Error::other("candidate trigger is stale or future"));
    }
    Ok(())
}

fn validate_next(previous: &ObservedAudio, next: &ObservedAudio, at_us: u64) -> io::Result<()> {
    if previous.sequence.checked_add(1) != Some(next.sequence)
        || next.captured_at_us < previous.captured_at_us
        || next.captured_at_us.saturating_sub(previous.captured_at_us) > 50_000
        || next.captured_at_us > at_us
    {
        return Err(io::Error::other("candidate capture discontinuity"));
    }
    Ok(())
}
