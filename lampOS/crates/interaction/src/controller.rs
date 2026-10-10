use crate::{
    AdmissionState, AdmittedInput, BootId, CameraGrant, CaptureState, Error,
    MAX_AUTHORITY_LEASE_US, MAX_INPUT_LEASE_US, MAX_OUTPUT_LEASE_US, MonoTime, OutputKind,
    OutputOwner, OutputPermit, Permission, PlaybackToken, Snapshot, TurnOwner,
    types::{TurnState, valid_lease},
};
use std::num::NonZeroU64;

/// The only mutable conversation authority. No worker may create its own copy.
///
/// All state has fixed size. The controller stores no audio, text, task queue,
/// observation payloads, or history. Every callback carries the token returned
/// when that work began. Publication and urgent revocation delivery are the
/// supervisor's responsibility; publish after transitions, including fail-closed
/// invalid input leases, and before sending output that needs the new revision.
#[derive(Debug)]
pub struct Controller {
    boot: BootId,
    revision: NonZeroU64,
    generation: NonZeroU64,
    camera_generation: NonZeroU64,
    presentation_generation: NonZeroU64,
    turn_counter: u64,
    playback_counter: u64,
    last_now: MonoTime,
    microphone: Permission,
    microphone_generation: NonZeroU64,
    camera: Permission,
    capture_until: Option<MonoTime>,
    admission_until: Option<MonoTime>,
    turn: Option<TurnState>,
    faulted: bool,
}

impl Controller {
    pub fn new(boot: BootId, now: MonoTime) -> Self {
        Self {
            boot,
            revision: NonZeroU64::MIN,
            generation: NonZeroU64::MIN,
            camera_generation: NonZeroU64::MIN,
            presentation_generation: NonZeroU64::MIN,
            turn_counter: 0,
            playback_counter: 0,
            last_now: now,
            microphone: Permission::Unknown,
            microphone_generation: NonZeroU64::MIN,
            camera: Permission::Unknown,
            capture_until: None,
            admission_until: None,
            turn: None,
            faulted: false,
        }
    }

    pub fn is_faulted(&self) -> bool {
        self.faulted
    }

    /// Closing, reopening, or losing knowledge of microphone permission revokes
    /// conversation output. A fresh capture-retention report is required after
    /// every change; reopening privacy alone never displays readiness.
    pub fn set_microphone_permission(
        &mut self,
        now: MonoTime,
        permission: Permission,
    ) -> Result<(), Error> {
        self.advance(now)?;
        if permission != self.microphone {
            self.microphone_generation = match self.microphone_generation.checked_add(1) {
                Some(next) => next,
                None => return Err(self.fault(Error::CounterExhausted)),
            };
            self.invalidate_turn()?;
            self.microphone = permission;
            self.capture_until = None;
            self.bump_revision()?;
        }
        Ok(())
    }

    /// Camera permission has its own lineage. It does not disable an unrelated
    /// audio conversation or alter capture/admission readiness. Camera-derived
    /// work must keep its original grant attached through the output boundary.
    pub fn set_camera_permission(
        &mut self,
        now: MonoTime,
        permission: Permission,
    ) -> Result<(), Error> {
        self.advance(now)?;
        if permission != self.camera {
            self.camera_generation = match self.camera_generation.checked_add(1) {
                Some(next) => next,
                None => return Err(self.fault(Error::CounterExhausted)),
            };
            self.camera = permission;
            self.bump_revision()?;
        }
        Ok(())
    }

    pub fn camera_grant(&mut self, now: MonoTime) -> Result<CameraGrant, Error> {
        self.advance(now)?;
        if self.camera != Permission::Allowed {
            return Err(Error::StaleCameraGrant);
        }
        Ok(CameraGrant {
            boot: self.boot,
            generation: self.camera_generation,
        })
    }

    /// An invalid retention lease fails closed, revoking an active turn. It does
    /// not change camera privacy. Equal receipt times are valid; decreasing
    /// trusted receipt times permanently fault this controller incarnation.
    pub fn set_capture(&mut self, now: MonoTime, state: CaptureState) -> Result<(), Error> {
        self.advance(now)?;
        let (until, error) = match state {
            CaptureState::Unavailable => (None, None),
            CaptureState::RetainingUntil(_) if self.microphone != Permission::Allowed => {
                (None, Some(Error::PrivacyClosed))
            }
            CaptureState::RetainingUntil(until) if !valid_lease(now, until, MAX_INPUT_LEASE_US) => {
                (None, Some(Error::InvalidLease))
            }
            CaptureState::RetainingUntil(until) => (Some(until), None),
        };
        self.capture_until = until;
        if !self.input_ready(now) {
            self.revoke_active_turn()?;
        }
        self.bump_revision()?;
        error.map_or(Ok(()), Err)
    }

    pub fn set_admission(&mut self, now: MonoTime, state: AdmissionState) -> Result<(), Error> {
        self.advance(now)?;
        let (until, error) = match state {
            AdmissionState::Unavailable => (None, None),
            AdmissionState::OpenUntil(until) if !valid_lease(now, until, MAX_INPUT_LEASE_US) => {
                (None, Some(Error::InvalidLease))
            }
            AdmissionState::OpenUntil(until) => (Some(until), None),
        };
        self.admission_until = until;
        if !self.input_ready(now) {
            self.revoke_active_turn()?;
        }
        self.bump_revision()?;
        error.map_or(Ok(()), Err)
    }

    /// Start an admitted turn and atomically revoke the previous turn's speech
    /// and conversational expression. Calling this for a listener's "yeah" is
    /// a classifier error; this core intentionally contains no social heuristic.
    pub fn admit(&mut self, now: MonoTime, input: AdmittedInput) -> Result<TurnOwner, Error> {
        self.advance(now)?;
        if !self.input_ready(now) {
            return Err(Error::InputUnavailable);
        }
        // Both typed events transfer ownership. Their distinction belongs in the
        // caller's trace; neither synthesizes an acknowledgment or movement.
        match input {
            AdmittedInput::NewTurn | AdmittedInput::Interruption => {}
        }
        self.invalidate_turn()?;
        let turn = match self.turn_counter.checked_add(1).and_then(NonZeroU64::new) {
            Some(turn) => turn,
            None => return Err(self.fault(Error::CounterExhausted)),
        };
        self.turn_counter = turn.get();
        let owner = TurnOwner {
            boot: self.boot,
            turn,
            generation: self.generation,
        };
        self.turn = Some(TurnState {
            owner,
            input_active: true,
            playback: None,
        });
        self.bump_revision()?;
        Ok(owner)
    }

    pub fn input_ended(&mut self, now: MonoTime, owner: TurnOwner) -> Result<(), Error> {
        self.advance(now)?;
        self.require_owner(owner)?;
        if let Some(turn) = self.turn.as_mut()
            && turn.input_active
        {
            turn.input_active = false;
            self.bump_presentation()?;
            self.bump_revision()?;
        }
        Ok(())
    }

    /// Capture lineage before scheduling work. Planning makes no output and
    /// does not bypass the final check. Speech may be prepared during input but
    /// cannot get a write permit until that admitted input has ended.
    pub fn plan_output(
        &mut self,
        now: MonoTime,
        owner: TurnOwner,
        kind: OutputKind,
        camera: Option<CameraGrant>,
    ) -> Result<OutputOwner, Error> {
        self.advance(now)?;
        self.require_owner(owner)?;
        if !self.input_ready(now) {
            return Err(Error::InputUnavailable);
        }
        if let Some(grant) = camera {
            self.require_camera(grant)?;
        }
        Ok(OutputOwner {
            owner,
            kind,
            camera,
            presentation_generation: self.presentation_generation,
        })
    }

    /// Mint/renew a short permit from the immutable plan, not current state.
    /// Input expiry/cancellation invalidates the turn permanently; phase changes
    /// invalidate older nonverbal plans. Camera reopening cannot renew an old
    /// visual plan. Deadlines are clipped to actual input readiness leases.
    pub fn issue_output(
        &mut self,
        now: MonoTime,
        output: OutputOwner,
        lease_us: u64,
    ) -> Result<OutputPermit, Error> {
        self.advance(now)?;
        let requested_deadline = now.checked_add(lease_us).ok_or(Error::InvalidLease)?;
        if !valid_lease(now, requested_deadline, MAX_OUTPUT_LEASE_US) {
            return Err(Error::InvalidLease);
        }
        self.require_owner(output.owner)?;
        if !self.input_ready(now) {
            return Err(Error::InputUnavailable);
        }
        if output.kind != OutputKind::Speech
            && output.presentation_generation != self.presentation_generation
        {
            return Err(Error::StalePresentation);
        }
        if output.kind == OutputKind::Speech && self.turn.is_some_and(|turn| turn.input_active) {
            return Err(Error::InputStillActive);
        }
        if let Some(grant) = output.camera {
            self.require_camera(grant)?;
        }
        let expires_at = [self.capture_until, self.admission_until]
            .into_iter()
            .flatten()
            .fold(requested_deadline, MonoTime::min);
        Ok(OutputPermit {
            owner: output.owner,
            revision: self.revision,
            presentation_generation: output.presentation_generation,
            kind: output.kind,
            issued_at: now,
            expires_at,
            camera: output.camera,
        })
    }

    /// Report actual playback start, not merely receipt of model audio. The sole
    /// speaker writer remains responsible for its own final-boundary check.
    pub fn playback_started(
        &mut self,
        now: MonoTime,
        permit: OutputPermit,
    ) -> Result<PlaybackToken, Error> {
        self.advance(now)?;
        self.state_at(now)?
            .check_permit(now, permit, OutputKind::Speech)?;
        if self.turn.is_some_and(|turn| turn.playback.is_some()) {
            return Err(Error::AlreadyPlaying);
        }
        let sequence = match self
            .playback_counter
            .checked_add(1)
            .and_then(NonZeroU64::new)
        {
            Some(sequence) => sequence,
            None => return Err(self.fault(Error::CounterExhausted)),
        };
        self.playback_counter = sequence.get();
        let token = PlaybackToken {
            owner: permit.owner,
            sequence,
        };
        if let Some(turn) = self.turn.as_mut() {
            turn.playback = Some(token);
        }
        self.bump_presentation()?;
        self.bump_revision()?;
        Ok(token)
    }

    pub fn playback_ended(&mut self, now: MonoTime, token: PlaybackToken) -> Result<(), Error> {
        self.advance(now)?;
        if self.turn.and_then(|turn| turn.playback) != Some(token) {
            return Err(Error::StalePlayback);
        }
        if let Some(turn) = self.turn.as_mut() {
            turn.playback = None;
        }
        self.bump_presentation()?;
        self.bump_revision()
    }

    /// Ends the whole conversational response; no later output may borrow it.
    /// A brief PCM underrun is a playback event, not a completed turn.
    pub fn complete_turn(&mut self, now: MonoTime, owner: TurnOwner) -> Result<(), Error> {
        self.cancel_turn(now, owner)
    }

    /// A stale cancellation is harmless to the newer turn.
    pub fn cancel_turn(&mut self, now: MonoTime, owner: TurnOwner) -> Result<(), Error> {
        self.advance(now)?;
        self.require_owner(owner)?;
        self.invalidate_turn()?;
        self.bump_revision()
    }

    /// Publish a fresh, strictly increasing revision. The snapshot is a lease,
    /// never durable authority that an output worker may keep indefinitely.
    pub fn snapshot(&mut self, now: MonoTime) -> Result<Snapshot, Error> {
        self.advance(now)?;
        self.bump_revision()?;
        self.state_at(now)
    }

    fn state_at(&self, now: MonoTime) -> Result<Snapshot, Error> {
        let mut expires_at = now
            .checked_add(MAX_AUTHORITY_LEASE_US)
            .ok_or(Error::InvalidLease)?;
        if self.turn.is_some() {
            expires_at = [self.capture_until, self.admission_until]
                .into_iter()
                .flatten()
                .fold(expires_at, MonoTime::min);
        }
        Ok(Snapshot {
            boot: self.boot,
            revision: self.revision,
            generation: self.generation,
            turn_counter: self.turn_counter,
            issued_at: now,
            expires_at,
            microphone: self.microphone,
            microphone_generation: self.microphone_generation,
            camera: self.camera,
            camera_generation: self.camera_generation,
            presentation_generation: self.presentation_generation,
            capture_until: self.capture_until,
            admission_until: self.admission_until,
            turn: self.turn,
        })
    }

    fn input_ready(&self, now: MonoTime) -> bool {
        self.microphone == Permission::Allowed
            && self.capture_until.is_some_and(|until| now < until)
            && self.admission_until.is_some_and(|until| now < until)
    }

    fn require_owner(&self, owner: TurnOwner) -> Result<(), Error> {
        if self.turn.is_some_and(|turn| turn.owner == owner) {
            Ok(())
        } else {
            Err(Error::StaleOwner)
        }
    }

    fn require_camera(&self, grant: CameraGrant) -> Result<(), Error> {
        if self.camera == Permission::Allowed
            && grant.boot == self.boot
            && grant.generation == self.camera_generation
        {
            Ok(())
        } else {
            Err(Error::StaleCameraGrant)
        }
    }

    fn advance(&mut self, now: MonoTime) -> Result<(), Error> {
        if self.faulted {
            return Err(Error::Faulted);
        }
        if now < self.last_now {
            return Err(self.fault(Error::ClockRegression));
        }
        self.last_now = now;
        let capture_expired = self.capture_until.is_some_and(|until| until <= now);
        let admission_expired = self.admission_until.is_some_and(|until| until <= now);
        if capture_expired {
            self.capture_until = None;
        }
        if admission_expired {
            self.admission_until = None;
        }
        if capture_expired || admission_expired {
            self.revoke_active_turn()?;
            self.bump_revision()?;
        }
        Ok(())
    }

    fn revoke_active_turn(&mut self) -> Result<(), Error> {
        if self.turn.is_some() {
            self.invalidate_turn()?;
        }
        Ok(())
    }

    fn invalidate_turn(&mut self) -> Result<(), Error> {
        self.turn = None;
        self.generation = match self.generation.checked_add(1) {
            Some(next) => next,
            None => return Err(self.fault(Error::CounterExhausted)),
        };
        Ok(())
    }

    fn bump_presentation(&mut self) -> Result<(), Error> {
        self.presentation_generation = match self.presentation_generation.checked_add(1) {
            Some(next) => next,
            None => return Err(self.fault(Error::CounterExhausted)),
        };
        Ok(())
    }

    fn bump_revision(&mut self) -> Result<(), Error> {
        self.revision = match self.revision.checked_add(1) {
            Some(next) => next,
            None => return Err(self.fault(Error::CounterExhausted)),
        };
        Ok(())
    }

    fn fault(&mut self, error: Error) -> Error {
        self.faulted = true;
        self.turn = None;
        self.capture_until = None;
        self.admission_until = None;
        error
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exhausting_a_generation_fails_closed_instead_of_wrapping() {
        let mut controller =
            Controller::new(BootId::new([1; 16]).unwrap(), MonoTime::from_micros(0));
        controller.generation = NonZeroU64::MAX;
        assert_eq!(
            controller.set_microphone_permission(MonoTime::from_micros(1), Permission::Allowed),
            Err(Error::CounterExhausted)
        );
        assert!(controller.is_faulted());
        assert_eq!(
            controller.snapshot(MonoTime::from_micros(2)),
            Err(Error::Faulted)
        );
    }
}
