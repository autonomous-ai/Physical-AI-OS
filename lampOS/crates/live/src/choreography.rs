//! Static conversational ring policy; the Controller remains the sole authority.
//!
//! One immutable frame may await feedback. There is no cue queue, animation,
//! idle indication or actuator I/O here. The supervisor must publish every
//! authority transition independently, even while a frame is pending, and send
//! each returned frame's snapshot on priority control before its data message.
use crate::ring_wire::{RingFeedback, RingFrame, RingPhase, RingRejection};
use lamp_interaction::{
    BoundaryGuard, Controller, Error as AuthorityError, MonoTime, OutputKind, Snapshot,
};
use lamp_ring::ChannelCeiling;
use std::io;

pub const RENEW_INTERVAL_US: u64 = 20_000;
pub const FEEDBACK_TIMEOUT_US: u64 = 100_000;
pub const REQUEST_TO_WRITE_TARGET_US: u64 = 50_000;
const PERMIT_LEASE_US: u64 = 100_000;

/// Fixed-size policy state. `last_frame` is renewal metadata, never queued work.
/// A returned error latches failure; the supervisor owns worker stop/revocation.
/// Successful feedback is historical evidence, not a claim about optical output.
#[derive(Debug)]
pub struct RingChoreographer {
    ceiling: ChannelCeiling,
    pending: Option<RingFrame>,
    last_frame: Option<RingFrame>,
    guard: Option<BoundaryGuard>,
    last_now: Option<MonoTime>,
    last_write: Option<(u64, u64)>,
    faulted: bool,
}

impl RingChoreographer {
    pub fn new(ceiling: u16) -> io::Result<Self> {
        Ok(Self {
            ceiling: ChannelCeiling::new(ceiling).map_err(io::Error::other)?,
            pending: None,
            last_frame: None,
            guard: None,
            last_now: None,
            last_write: None,
            faulted: false,
        })
    }

    pub fn is_faulted(&self) -> bool {
        self.faulted
    }

    pub fn pending(&self) -> Option<RingFrame> {
        self.pending
    }

    /// Constant-size, allocation-free success path; no I/O or sleeps. Readiness
    /// without an admitted owner produces no cue. Every frame gets a fresh
    /// phase-bound Light plan; the old frame is never relabelled for a successor.
    pub fn prepare(
        &mut self,
        controller: &mut Controller,
        now: MonoTime,
    ) -> io::Result<Option<RingFrame>> {
        let result = self.prepare_inner(controller, now);
        if result.is_err() {
            self.faulted = true;
        }
        result
    }

    fn prepare_inner(
        &mut self,
        controller: &mut Controller,
        now: MonoTime,
    ) -> io::Result<Option<RingFrame>> {
        self.advance(now)?;
        let current = controller.snapshot(now).map_err(io::Error::other)?;
        self.install_current(current, now)?;
        if self.pending.is_some() {
            return Ok(None);
        }
        let Some(phase) = RingPhase::from_snapshot(current) else {
            return Ok(None);
        };
        let owner = current
            .owner()
            .ok_or_else(|| invalid("ring phase has no admitted owner"))?;
        let requested_at_us = now.as_micros();
        if requested_at_us == 0 {
            return Err(invalid("ring request timestamp must be nonzero"));
        }
        // Receipts identify a frame by owner, phase and request time. A phase
        // cycle within one clock tick must not reuse the old receipt identity.
        if self
            .last_frame
            .is_some_and(|last| requested_at_us == last.requested_at_us)
        {
            return Ok(None);
        }
        if let Some(last) = self.last_frame
            && requested_at_us - last.requested_at_us < RENEW_INTERVAL_US
            && !self.superseded(last, current, now)?
        {
            return Ok(None);
        }
        // This is an audio-conversation cue, not camera-derived output. Camera
        // privacy alone must not restart or disable an unrelated valid cue.
        let plan = controller
            .plan_output(now, owner, OutputKind::Light, None)
            .map_err(io::Error::other)?;
        let permit = controller
            .issue_output(now, plan, PERMIT_LEASE_US)
            .map_err(io::Error::other)?;
        self.guard
            .as_mut()
            .ok_or_else(|| invalid("ring policy has no authority snapshot"))?
            .check(now, permit, OutputKind::Light)
            .map_err(io::Error::other)?;
        let frame = RingFrame {
            snapshot: current,
            permit,
            phase,
            ceiling: u16::from(self.ceiling.get()),
            requested_at_us,
        };
        self.pending = Some(frame);
        self.last_frame = Some(frame);
        Ok(Some(frame))
    }

    /// Validate feedback without changing conversation, capture or playback.
    /// Blanking is authority cleanup, not an acknowledgment of a pending frame.
    /// A late historical write cannot select the current phase or owner.
    pub fn feedback(
        &mut self,
        report: &RingFeedback,
        current: Snapshot,
        now: MonoTime,
    ) -> io::Result<()> {
        let result = self.feedback_inner(report, current, now);
        if result.is_err() {
            self.faulted = true;
        }
        result
    }

    fn feedback_inner(
        &mut self,
        report: &RingFeedback,
        current: Snapshot,
        now: MonoTime,
    ) -> io::Result<()> {
        self.advance(now)?;
        self.install_current(current, now)?;
        match *report {
            RingFeedback::Presented {
                owner,
                phase,
                requested_at_us,
                write_started_at_us,
                write_finished_at_us,
            } => {
                let frame = self
                    .pending
                    .ok_or_else(|| invalid("unknown or duplicate ring presentation"))?;
                if (owner, phase, requested_at_us)
                    != (frame.permit.owner(), frame.phase, frame.requested_at_us)
                {
                    return Err(invalid(
                        "ring presentation identity does not match pending frame",
                    ));
                }
                self.validate_write(write_started_at_us, write_finished_at_us, now)?;
                if write_started_at_us < requested_at_us {
                    return Err(invalid("ring write predates its request"));
                }
                if write_finished_at_us >= frame.permit.expires_at().as_micros() {
                    return Err(invalid(
                        "ring presentation completed outside its original permit",
                    ));
                }
                if write_finished_at_us - requested_at_us > REQUEST_TO_WRITE_TARGET_US {
                    return Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "ring request-to-write completion exceeded 50 ms target",
                    ));
                }
                self.last_write = Some((write_started_at_us, write_finished_at_us));
                self.pending = None;
            }
            RingFeedback::Rejected {
                owner,
                phase,
                requested_at_us,
                reason,
            } => {
                let frame = self
                    .pending
                    .ok_or_else(|| invalid("unknown or duplicate ring rejection"))?;
                if (owner, phase, requested_at_us)
                    != (frame.permit.owner(), frame.phase, frame.requested_at_us)
                {
                    return Err(invalid(
                        "ring rejection identity does not match pending frame",
                    ));
                }
                if !supersession_reason(reason) || !self.superseded(frame, current, now)? {
                    return Err(invalid(
                        "ring rejected a current cue or reported an invalid rejection",
                    ));
                }
                self.pending = None;
            }
            RingFeedback::Blanked {
                write_started_at_us,
                write_finished_at_us,
                ..
            } => {
                self.validate_write(write_started_at_us, write_finished_at_us, now)?;
                self.last_write = Some((write_started_at_us, write_finished_at_us));
            }
        }
        Ok(())
    }

    fn advance(&mut self, now: MonoTime) -> io::Result<()> {
        if self.faulted {
            return Err(invalid("ring choreography is faulted"));
        }
        if self.last_now.is_some_and(|previous| now < previous) {
            return Err(invalid("ring choreography clock regressed"));
        }
        self.last_now = Some(now);
        if let Some(frame) = self.pending
            && now.as_micros() - frame.requested_at_us >= FEEDBACK_TIMEOUT_US
        {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "ring frame feedback exceeded 100 ms deadline",
            ));
        }
        Ok(())
    }

    fn install_current(&mut self, current: Snapshot, now: MonoTime) -> io::Result<()> {
        let guard = self
            .guard
            .get_or_insert_with(|| BoundaryGuard::new(current.boot(), now));
        // The caller may give the same unchanged snapshot with its feedback.
        // Any different packet must pass the guard's strict revision/lineage rules.
        if guard.state() != Some(current) {
            guard.install(now, current).map_err(io::Error::other)?;
        } else if current.issued_at() > now || current.expires_at() <= now {
            return Err(invalid("ring current snapshot is not fresh"));
        }
        Ok(())
    }

    fn superseded(
        &mut self,
        frame: RingFrame,
        current: Snapshot,
        now: MonoTime,
    ) -> io::Result<bool> {
        if current.owner() != Some(frame.permit.owner())
            || RingPhase::from_snapshot(current) != Some(frame.phase)
        {
            return Ok(true);
        }
        match self
            .guard
            .as_mut()
            .ok_or_else(|| invalid("ring policy has no authority snapshot"))?
            .check(now, frame.permit, OutputKind::Light)
        {
            Ok(()) | Err(AuthorityError::ExpiredPermit) => Ok(false),
            Err(AuthorityError::StalePresentation | AuthorityError::StaleCameraGrant) => Ok(true),
            Err(error) => Err(io::Error::other(error)),
        }
    }

    fn validate_write(&self, start: u64, finish: u64, now: MonoTime) -> io::Result<()> {
        if start == 0 || finish < start || finish > now.as_micros() {
            return Err(invalid("ring write timestamps are future or regressing"));
        }
        if let Some(previous) = self.last_write
            && (start < previous.1 || (start, finish) == previous)
        {
            return Err(invalid("ring write receipt is duplicate or regressing"));
        }
        Ok(())
    }
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn supersession_reason(reason: RingRejection) -> bool {
    matches!(
        reason,
        RingRejection::StaleOwner
            | RingRejection::PhaseMismatch
            | RingRejection::StalePresentation
            | RingRejection::StaleCameraGrant
            | RingRejection::PrivacyClosed
            | RingRejection::InputUnavailable
            | RingRejection::ExpiredRequest
            | RingRejection::ExpiredPermit
            | RingRejection::ExpiredState
            | RingRejection::StaleSnapshot
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ring_wire::RingBlankReason;
    use lamp_interaction::{
        AdmissionState, AdmittedInput, BootId, CaptureState, OutputPermit, Permission, TurnOwner,
    };
    use std::time::Instant;

    fn t(us: u64) -> MonoTime {
        MonoTime::from_micros(us)
    }

    fn ready() -> Controller {
        let mut controller = Controller::new(BootId::new([5; 16]).unwrap(), t(0));
        controller
            .set_microphone_permission(t(0), Permission::Allowed)
            .unwrap();
        renew_input(&mut controller, 0);
        controller
    }

    fn renew_input(controller: &mut Controller, now: u64) {
        controller
            .set_capture(t(now), CaptureState::RetainingUntil(t(now + 1_000_000)))
            .unwrap();
        controller
            .set_admission(t(now), AdmissionState::OpenUntil(t(now + 1_000_000)))
            .unwrap();
    }

    fn speaking_permit(controller: &mut Controller, owner: TurnOwner, now: u64) -> OutputPermit {
        let output = controller
            .plan_output(t(now), owner, OutputKind::Speech, None)
            .unwrap();
        controller.issue_output(t(now), output, 100_000).unwrap()
    }

    fn presented(frame: RingFrame, start: u64, finish: u64) -> RingFeedback {
        RingFeedback::Presented {
            owner: frame.permit.owner(),
            phase: frame.phase,
            requested_at_us: frame.requested_at_us,
            write_started_at_us: start,
            write_finished_at_us: finish,
        }
    }

    fn rejected(frame: RingFrame, reason: RingRejection) -> RingFeedback {
        RingFeedback::Rejected {
            owner: frame.permit.owner(),
            phase: frame.phase,
            requested_at_us: frame.requested_at_us,
            reason,
        }
    }

    fn acknowledge(
        policy: &mut RingChoreographer,
        controller: &mut Controller,
        frame: RingFrame,
        now: u64,
    ) {
        let current = controller.snapshot(t(now)).unwrap();
        policy
            .feedback(&presented(frame, now - 1, now), current, t(now))
            .unwrap();
    }

    #[test]
    fn ceiling_matches_existing_hardware_bound_including_zero() {
        for value in [0, 120] {
            let mut policy = RingChoreographer::new(value).unwrap();
            let mut controller = ready();
            controller.admit(t(1), AdmittedInput::NewTurn).unwrap();
            assert_eq!(
                policy
                    .prepare(&mut controller, t(1))
                    .unwrap()
                    .unwrap()
                    .ceiling,
                value
            );
        }
        for value in [121, u16::MAX] {
            assert!(RingChoreographer::new(value).is_err());
        }
    }

    #[test]
    fn readiness_and_privacy_reopening_never_invent_listening() {
        let mut controller = Controller::new(BootId::new([5; 16]).unwrap(), t(0));
        let mut policy = RingChoreographer::new(40).unwrap();
        assert!(policy.prepare(&mut controller, t(0)).unwrap().is_none());
        controller
            .set_microphone_permission(t(1), Permission::Allowed)
            .unwrap();
        assert!(policy.prepare(&mut controller, t(1)).unwrap().is_none());
        renew_input(&mut controller, 2);
        assert!(controller.snapshot(t(2)).unwrap().listening_ready(t(2)));
        assert!(policy.prepare(&mut controller, t(2)).unwrap().is_none());
        controller
            .set_microphone_permission(t(3), Permission::Denied)
            .unwrap();
        controller
            .set_microphone_permission(t(4), Permission::Allowed)
            .unwrap();
        assert!(policy.prepare(&mut controller, t(4)).unwrap().is_none());
        renew_input(&mut controller, 5);
        assert!(policy.prepare(&mut controller, t(5)).unwrap().is_none());
        let owner = controller.admit(t(6), AdmittedInput::NewTurn).unwrap();
        let frame = policy.prepare(&mut controller, t(6)).unwrap().unwrap();
        assert_eq!(frame.phase, RingPhase::Listening);
        assert_eq!(frame.permit.owner(), owner);
        assert_eq!(frame.permit.kind(), OutputKind::Light);
        assert!(frame.snapshot.input_active());
    }

    #[test]
    fn phases_follow_input_and_actual_playback_not_prepared_audio() {
        let mut controller = ready();
        let owner = controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
        let mut policy = RingChoreographer::new(40).unwrap();
        let listening = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
        acknowledge(&mut policy, &mut controller, listening, 12);
        controller.input_ended(t(13), owner).unwrap();
        let waiting = policy.prepare(&mut controller, t(13)).unwrap().unwrap();
        assert_eq!(waiting.phase, RingPhase::Waiting);
        acknowledge(&mut policy, &mut controller, waiting, 15);
        let speech = speaking_permit(&mut controller, owner, 16);
        assert!(policy.prepare(&mut controller, t(16)).unwrap().is_none());
        assert_eq!(
            RingPhase::from_snapshot(controller.snapshot(t(16)).unwrap()),
            Some(RingPhase::Waiting)
        );
        let playback = controller.playback_started(t(17), speech).unwrap();
        let speaking = policy.prepare(&mut controller, t(17)).unwrap().unwrap();
        assert_eq!(speaking.phase, RingPhase::Speaking);
        acknowledge(&mut policy, &mut controller, speaking, 19);
        controller.playback_ended(t(20), playback).unwrap();
        let waiting_again = policy.prepare(&mut controller, t(20)).unwrap().unwrap();
        assert_eq!(waiting_again.phase, RingPhase::Waiting);
        acknowledge(&mut policy, &mut controller, waiting_again, 22);
        controller.complete_turn(t(23), owner).unwrap();
        assert!(policy.prepare(&mut controller, t(23)).unwrap().is_none());
        assert_eq!(controller.snapshot(t(23)).unwrap().owner(), None);
    }

    #[test]
    fn one_pending_frame_retains_original_identity_during_new_turn() {
        let mut controller = ready();
        let old = controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
        let mut policy = RingChoreographer::new(40).unwrap();
        let frame = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
        let next = controller
            .admit(t(11), AdmittedInput::Interruption)
            .unwrap();
        assert!(policy.prepare(&mut controller, t(11)).unwrap().is_none());
        assert_eq!(policy.pending(), Some(frame));
        assert_eq!(frame.permit.owner(), old);
        let current = controller.snapshot(t(12)).unwrap();
        policy
            .feedback(&rejected(frame, RingRejection::StaleOwner), current, t(12))
            .unwrap();
        assert_eq!(current.owner(), Some(next));
        let fresh = policy.prepare(&mut controller, t(12)).unwrap().unwrap();
        assert_eq!(fresh.permit.owner(), next);
        assert_eq!(fresh.phase, RingPhase::Listening);
        assert_eq!(policy.pending(), Some(fresh));
    }

    #[test]
    fn same_turn_same_enum_after_playback_cycle_has_new_presentation_lineage() {
        let mut controller = ready();
        let owner = controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
        controller.input_ended(t(10), owner).unwrap();
        let mut policy = RingChoreographer::new(40).unwrap();
        let old = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
        let speech = speaking_permit(&mut controller, owner, 11);
        let playback = controller.playback_started(t(11), speech).unwrap();
        controller.playback_ended(t(12), playback).unwrap();
        let current = controller.snapshot(t(13)).unwrap();
        assert_eq!(RingPhase::from_snapshot(current), Some(old.phase));
        policy
            .feedback(
                &rejected(old, RingRejection::StalePresentation),
                current,
                t(13),
            )
            .unwrap();
        let new = policy.prepare(&mut controller, t(13)).unwrap().unwrap();
        assert_eq!(new.phase, old.phase);
        assert_eq!(new.permit.owner(), old.permit.owner());
        let mut guard = BoundaryGuard::new(current.boot(), t(13));
        guard.install(t(13), new.snapshot).unwrap();
        assert_eq!(
            guard.check(t(13), old.permit, OutputKind::Light),
            Err(AuthorityError::StalePresentation)
        );
        guard.check(t(13), new.permit, OutputKind::Light).unwrap();
    }

    #[test]
    fn instantaneous_same_phase_cycle_cannot_reuse_receipt_identity() {
        let mut controller = ready();
        let owner = controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
        controller.input_ended(t(10), owner).unwrap();
        let mut policy = RingChoreographer::new(40).unwrap();
        let old = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
        policy
            .feedback(&presented(old, 10, 10), old.snapshot, t(10))
            .unwrap();
        let speech = speaking_permit(&mut controller, owner, 10);
        let playback = controller.playback_started(t(10), speech).unwrap();
        controller.playback_ended(t(10), playback).unwrap();
        assert!(policy.prepare(&mut controller, t(10)).unwrap().is_none());
        let fresh = policy.prepare(&mut controller, t(11)).unwrap().unwrap();
        assert_ne!(fresh.requested_at_us, old.requested_at_us);
    }

    #[test]
    fn acknowledged_old_phase_is_only_historical_evidence() {
        let mut controller = ready();
        let owner = controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
        let mut policy = RingChoreographer::new(40).unwrap();
        let old = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
        controller.input_ended(t(12), owner).unwrap();
        let current = controller.snapshot(t(13)).unwrap();
        policy
            .feedback(&presented(old, 10, 11), current, t(13))
            .unwrap();
        assert!(!current.input_active());
        assert_eq!(
            policy
                .prepare(&mut controller, t(13))
                .unwrap()
                .unwrap()
                .phase,
            RingPhase::Waiting
        );
    }

    #[test]
    fn late_old_receipt_cannot_clear_a_new_pending_frame_or_mutate_controller() {
        let mut controller = ready();
        controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
        let mut policy = RingChoreographer::new(40).unwrap();
        let old = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
        let next = controller
            .admit(t(11), AdmittedInput::Interruption)
            .unwrap();
        let current = controller.snapshot(t(12)).unwrap();
        let old_receipt = rejected(old, RingRejection::StaleOwner);
        policy.feedback(&old_receipt, current, t(12)).unwrap();
        let fresh = policy.prepare(&mut controller, t(13)).unwrap().unwrap();
        let current = controller.snapshot(t(14)).unwrap();
        assert!(policy.feedback(&old_receipt, current, t(14)).is_err());
        assert_eq!(policy.pending(), Some(fresh));
        assert!(policy.is_faulted());
        assert_eq!(controller.snapshot(t(15)).unwrap().owner(), Some(next));
        assert!(controller.snapshot(t(15)).unwrap().input_active());
    }

    #[test]
    fn current_phase_rejection_is_a_latched_failure() {
        let mut controller = ready();
        controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
        let mut policy = RingChoreographer::new(40).unwrap();
        let frame = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
        let current = controller.snapshot(t(11)).unwrap();
        assert!(
            policy
                .feedback(&rejected(frame, RingRejection::StaleOwner), current, t(11))
                .is_err()
        );
        assert!(policy.is_faulted());
        assert_eq!(policy.pending(), Some(frame));
        assert!(policy.prepare(&mut controller, t(12)).is_err());
    }

    #[test]
    fn invalid_rejection_reason_is_not_excused_by_a_new_turn() {
        let mut controller = ready();
        controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
        let mut policy = RingChoreographer::new(40).unwrap();
        let frame = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
        controller
            .admit(t(11), AdmittedInput::Interruption)
            .unwrap();
        let current = controller.snapshot(t(11)).unwrap();
        assert!(
            policy
                .feedback(
                    &rejected(frame, RingRejection::InvalidCeiling),
                    current,
                    t(11)
                )
                .is_err()
        );
    }

    #[test]
    fn privacy_and_expired_input_revoke_cues_without_an_off_command() {
        for privacy in [false, true] {
            let mut controller = ready();
            if !privacy {
                controller
                    .set_capture(t(1), CaptureState::RetainingUntil(t(20)))
                    .unwrap();
            }
            controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
            let mut policy = RingChoreographer::new(40).unwrap();
            let frame = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
            if privacy {
                controller
                    .set_microphone_permission(t(21), Permission::Denied)
                    .unwrap();
            }
            let current = controller.snapshot(t(21)).unwrap();
            assert_eq!(current.owner(), None);
            policy
                .feedback(&rejected(frame, RingRejection::StaleOwner), current, t(21))
                .unwrap();
            assert!(policy.prepare(&mut controller, t(22)).unwrap().is_none());
        }
    }

    #[test]
    fn unrelated_camera_privacy_does_not_restart_audio_cue() {
        let mut controller = ready();
        controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
        let mut policy = RingChoreographer::new(40).unwrap();
        let frame = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
        assert_eq!(frame.permit.camera(), None);
        acknowledge(&mut policy, &mut controller, frame, 12);
        controller
            .set_camera_permission(t(13), Permission::Allowed)
            .unwrap();
        controller
            .set_camera_permission(t(14), Permission::Denied)
            .unwrap();
        assert!(policy.prepare(&mut controller, t(14)).unwrap().is_none());
        assert!(
            policy
                .prepare(&mut controller, t(20_009))
                .unwrap()
                .is_none()
        );
        assert!(
            policy
                .prepare(&mut controller, t(20_010))
                .unwrap()
                .is_some()
        );
    }

    #[test]
    fn long_waiting_renews_only_at_bound_and_retains_owner_with_local_latency_sample() {
        let mut controller = ready();
        let owner = controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
        controller.input_ended(t(10), owner).unwrap();
        let mut policy = RingChoreographer::new(40).unwrap();
        let mut durations = Vec::with_capacity(2_000);
        for sequence in 0..2_000 {
            let now = 10 + sequence * RENEW_INTERVAL_US;
            renew_input(&mut controller, now);
            let started = Instant::now();
            let frame = policy.prepare(&mut controller, t(now)).unwrap().unwrap();
            durations.push(started.elapsed().as_nanos());
            assert_eq!(frame.phase, RingPhase::Waiting);
            assert_eq!(frame.permit.owner(), owner);
            assert_eq!(frame.permit.expires_at(), t(now + PERMIT_LEASE_US));
            acknowledge(&mut policy, &mut controller, frame, now + 2);
            assert!(
                policy
                    .prepare(&mut controller, t(now + RENEW_INTERVAL_US - 1))
                    .unwrap()
                    .is_none()
            );
        }
        let first = durations[0];
        durations.sort_unstable();
        eprintln!(
            "host-only prepare sample n={} first={}ns p50={}ns p95={}ns max={}ns; no optical/device claim",
            durations.len(),
            first,
            durations[999],
            durations[1899],
            durations[1999]
        );
        assert_eq!(
            controller.snapshot(t(40_000_009)).unwrap().owner(),
            Some(owner)
        );
    }

    #[test]
    fn unknown_duplicate_and_mismatched_presentations_fault() {
        for case in ["duplicate", "phase", "request", "owner"] {
            let mut controller = ready();
            controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
            let mut policy = RingChoreographer::new(40).unwrap();
            let frame = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
            let mut report = presented(frame, 11, 12);
            if case == "duplicate" {
                acknowledge(&mut policy, &mut controller, frame, 12);
            } else if let RingFeedback::Presented {
                owner,
                phase,
                requested_at_us,
                ..
            } = &mut report
            {
                match case {
                    "phase" => *phase = RingPhase::Speaking,
                    "request" => *requested_at_us += 1,
                    "owner" => {
                        *owner = controller
                            .admit(t(12), AdmittedInput::Interruption)
                            .unwrap()
                    }
                    _ => unreachable!(),
                }
            }
            let current = controller.snapshot(t(13)).unwrap();
            assert!(policy.feedback(&report, current, t(13)).is_err(), "{case}");
            assert!(policy.is_faulted());
        }
    }

    #[test]
    fn future_reversed_and_pre_request_write_times_are_rejected() {
        for (start, finish) in [(11, 21), (12, 11), (9, 12), (0, 12)] {
            let mut controller = ready();
            controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
            let mut policy = RingChoreographer::new(40).unwrap();
            let frame = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
            let current = controller.snapshot(t(20)).unwrap();
            assert!(
                policy
                    .feedback(&presented(frame, start, finish), current, t(20))
                    .is_err()
            );
        }
    }

    #[test]
    fn request_completion_latency_target_is_explicit_and_inclusive() {
        for elapsed in [50_000, 50_001] {
            let mut controller = ready();
            controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
            let mut policy = RingChoreographer::new(40).unwrap();
            let frame = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
            let now = 10 + elapsed;
            let current = controller.snapshot(t(now)).unwrap();
            let result = policy.feedback(&presented(frame, now - 1, now), current, t(now));
            if elapsed == 50_000 {
                result.unwrap();
                assert!(!policy.is_faulted());
            } else {
                assert_eq!(result.unwrap_err().kind(), io::ErrorKind::TimedOut);
                assert!(policy.is_faulted());
            }
        }
    }

    #[test]
    fn historical_presentation_cannot_claim_completion_after_its_clipped_permit() {
        let mut controller = ready();
        controller
            .set_capture(t(1), CaptureState::RetainingUntil(t(20)))
            .unwrap();
        controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
        let mut policy = RingChoreographer::new(40).unwrap();
        let frame = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
        assert_eq!(frame.permit.expires_at(), t(20));
        let current = controller.snapshot(t(30)).unwrap();
        assert!(
            policy
                .feedback(&presented(frame, 15, 20), current, t(30))
                .is_err()
        );
        assert!(policy.is_faulted());
    }

    #[test]
    fn pending_feedback_deadline_is_not_renewed_by_snapshot_heartbeats() {
        for through_feedback in [false, true] {
            let mut controller = ready();
            controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
            let mut policy = RingChoreographer::new(40).unwrap();
            let frame = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
            assert!(
                policy
                    .prepare(&mut controller, t(100_009))
                    .unwrap()
                    .is_none()
            );
            let error = if through_feedback {
                let current = controller.snapshot(t(100_010)).unwrap();
                policy
                    .feedback(&presented(frame, 11, 12), current, t(100_010))
                    .unwrap_err()
            } else {
                policy.prepare(&mut controller, t(100_010)).unwrap_err()
            };
            assert_eq!(error.kind(), io::ErrorKind::TimedOut);
            assert!(policy.is_faulted());
        }
    }

    #[test]
    fn clock_regression_latches_without_mutating_controller() {
        for through_feedback in [false, true] {
            let mut controller = ready();
            let owner = controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
            let mut policy = RingChoreographer::new(40).unwrap();
            let frame = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
            if through_feedback {
                assert!(
                    policy
                        .feedback(&presented(frame, 10, 10), frame.snapshot, t(9))
                        .is_err()
                );
            } else {
                assert!(policy.prepare(&mut controller, t(9)).is_err());
            }
            assert!(policy.is_faulted());
            assert_eq!(controller.snapshot(t(11)).unwrap().owner(), Some(owner));
        }
    }

    #[test]
    fn stale_or_future_current_snapshot_cannot_validate_feedback() {
        for future in [false, true] {
            let mut controller = ready();
            controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
            let mut policy = RingChoreographer::new(40).unwrap();
            let frame = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
            let current = if future {
                controller.snapshot(t(30)).unwrap()
            } else {
                assert!(policy.prepare(&mut controller, t(20)).unwrap().is_none());
                frame.snapshot
            };
            assert!(
                policy
                    .feedback(&presented(frame, 11, 12), current, t(20))
                    .is_err()
            );
            assert!(policy.is_faulted());
        }
    }

    #[test]
    fn blank_receipt_never_acknowledges_or_relabels_pending_frame() {
        let mut controller = ready();
        controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
        let mut policy = RingChoreographer::new(40).unwrap();
        let frame = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
        let current = controller.snapshot(t(13)).unwrap();
        let blank = RingFeedback::Blanked {
            reason: RingBlankReason::AuthorityLost {
                reason: RingRejection::StalePresentation,
            },
            write_started_at_us: 11,
            write_finished_at_us: 12,
        };
        policy.feedback(&blank, current, t(13)).unwrap();
        assert_eq!(policy.pending(), Some(frame));
        assert!(policy.feedback(&blank, current, t(13)).is_err());
        assert_eq!(policy.pending(), Some(frame));
    }

    #[test]
    fn write_sequence_must_not_regress_across_blanks_and_presentations() {
        let mut controller = ready();
        controller.admit(t(10), AdmittedInput::NewTurn).unwrap();
        let mut policy = RingChoreographer::new(40).unwrap();
        let frame = policy.prepare(&mut controller, t(10)).unwrap().unwrap();
        let current = controller.snapshot(t(20)).unwrap();
        policy
            .feedback(
                &RingFeedback::Blanked {
                    reason: RingBlankReason::Shutdown,
                    write_started_at_us: 15,
                    write_finished_at_us: 16,
                },
                current,
                t(20),
            )
            .unwrap();
        assert!(
            policy
                .feedback(&presented(frame, 14, 17), current, t(20))
                .is_err()
        );
    }
}
