use crate::{
    BootId, CameraGrant, Error, MonoTime, OutputKind, OutputPermit, Permission, Snapshot, TurnOwner,
};

/// Session/privacy lineage for a sink that can write only internally generated
/// zero PCM. This is deliberately not an OutputPermit and authorizes no speech,
/// listening cue, movement, or capture. It remains usable across conversation
/// changes only while the latest installed state is fresh and privacy matches.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ZeroAuthority {
    boot: BootId,
    microphone_generation: u64,
}

impl ZeroAuthority {
    pub fn microphone_generation(self) -> u64 {
        self.microphone_generation
    }
}

/// A single output writer's final boundary. Authority starts disarmed.
///
/// The expected boot comes from a trusted supervisor handshake, never from an
/// unsolicited state packet. A controller restart requires a new guard bound to
/// its fresh boot ID. This object deliberately has no auto-rebind operation.
/// Old/duplicate packets cannot roll back a newer snapshot. Invalid state on a
/// trusted connection and a regressing local clock latch a fault; the worker
/// must stop/flush and report it rather than continue using cached authority.
#[derive(Debug)]
pub struct BoundaryGuard {
    expected_boot: BootId,
    last_now: MonoTime,
    state: Option<Snapshot>,
    playback_highwater: u64,
    faulted: bool,
}

impl BoundaryGuard {
    pub fn new(expected_boot: BootId, now: MonoTime) -> Self {
        Self {
            expected_boot,
            last_now: now,
            state: None,
            playback_highwater: 0,
            faulted: false,
        }
    }

    pub fn install(&mut self, now: MonoTime, state: Snapshot) -> Result<(), Error> {
        self.advance(now)?;
        if state.boot != self.expected_boot {
            return Err(Error::WrongBoot);
        }
        if let Err(error) = state.validate_shape() {
            self.invalidate();
            return Err(error);
        }
        if state.issued_at > now {
            return Err(Error::FutureState);
        }
        if state.expires_at <= now {
            return Err(Error::ExpiredState);
        }
        if let Some(previous) = self.state {
            if state.revision <= previous.revision {
                return Err(Error::StaleSnapshot);
            }
            let presentation_regressed = state.owner() == previous.owner()
                && state.turn != previous.turn
                && state.presentation_generation <= previous.presentation_generation;
            let lifecycle_regressed = state.generation == previous.generation
                && state.owner() == previous.owner()
                && !previous.input_active()
                && state.input_active();
            if state.issued_at < previous.issued_at
                || state.generation < previous.generation
                || state.turn_counter < previous.turn_counter
                || state.camera_generation < previous.camera_generation
                || state.microphone_generation < previous.microphone_generation
                || state.presentation_generation < previous.presentation_generation
                || (state.owner() != previous.owner() && state.generation <= previous.generation)
                || (state.microphone != previous.microphone
                    && (state.generation <= previous.generation
                        || state.microphone_generation <= previous.microphone_generation))
                || (state.microphone_generation != previous.microphone_generation
                    && state.generation <= previous.generation)
                || (state.camera != previous.camera
                    && state.camera_generation <= previous.camera_generation)
                || lifecycle_regressed
                || presentation_regressed
            {
                self.invalidate();
                return Err(Error::StateRegression);
            }
        }
        if let Some(playback) = state.playback() {
            if playback.sequence() < self.playback_highwater {
                self.invalidate();
                return Err(Error::StateRegression);
            }
            self.playback_highwater = playback.sequence();
        }
        self.state = Some(state);
        Ok(())
    }

    /// Call immediately before each bounded write, after handling priority
    /// cancellation/state messages. Do not cache a successful check or turn it
    /// into permission for a whole buffered utterance/movement. A worker that
    /// cannot stop a hardware operation must bound that operation separately.
    pub fn check(
        &mut self,
        now: MonoTime,
        permit: OutputPermit,
        kind: OutputKind,
    ) -> Result<(), Error> {
        self.advance(now)?;
        let state = self.state.ok_or(Error::NoAuthority)?;
        if state.issued_at > now {
            return Err(Error::FutureState);
        }
        if state.expires_at <= now {
            return Err(Error::ExpiredState);
        }
        state.check_permit(now, permit, kind)
    }

    /// Capture camera privacy lineage immediately before a bounded frame read.
    /// Recheck after the read and retain the original grant through delivery.
    /// This does not imply a current frame, visual confidence, or input readiness.
    /// Ordinary conversation changes leave the grant unchanged; every camera
    /// privacy transition changes it, including a coalesced close/reopen pair.
    pub fn current_camera_grant(&mut self, now: MonoTime) -> Result<CameraGrant, Error> {
        self.advance(now)?;
        let state = self.state.ok_or(Error::NoAuthority)?;
        if state.issued_at > now {
            return Err(Error::FutureState);
        }
        if state.expires_at <= now {
            return Err(Error::ExpiredState);
        }
        if state.camera != Permission::Allowed {
            return Err(Error::PrivacyClosed);
        }
        Ok(CameraGrant {
            boot: state.boot,
            generation: state.camera_generation,
        })
    }

    /// Mint only a zero-PCM session token. Input need not be ready yet: accepted
    /// silence primes the reference clock before capture can become ready.
    pub fn zero_authority(&mut self, now: MonoTime) -> Result<ZeroAuthority, Error> {
        self.advance(now)?;
        let state = self.current_microphone_state(now)?;
        Ok(ZeroAuthority {
            boot: state.boot(),
            microphone_generation: state.microphone_generation(),
        })
    }

    /// Recheck immediately around each bounded zero-only device operation.
    /// New snapshots may refresh freshness, but cannot renew an old privacy era.
    pub fn check_zero(&mut self, now: MonoTime, token: ZeroAuthority) -> Result<(), Error> {
        self.advance(now)?;
        if token.boot != self.expected_boot {
            return Err(Error::WrongBoot);
        }
        let state = self.current_microphone_state(now)?;
        if state.microphone_generation() != token.microphone_generation {
            return Err(Error::PrivacyClosed);
        }
        Ok(())
    }

    /// Validate only speech already accepted under this original permit and
    /// microphone era. This never authorizes additional PCM, including the
    /// remainder of a partial write. The sink must separately bound the fixed
    /// accepted range and nonrenewable retirement deadline; use `check` for
    /// every new write. OwnerNone and every hard revocation still fail closed.
    pub fn check_accepted_speech_retirement(
        &mut self,
        now: MonoTime,
        permit: OutputPermit,
        accepted_microphone: ZeroAuthority,
    ) -> Result<TurnOwner, Error> {
        self.check_zero(now, accepted_microphone)?;
        self.current_microphone_state(now)?
            .check_accepted_speech(now, permit)
    }

    fn current_microphone_state(&self, now: MonoTime) -> Result<Snapshot, Error> {
        let state = self.state.ok_or(Error::NoAuthority)?;
        if state.issued_at() > now {
            return Err(Error::FutureState);
        }
        if now >= state.expires_at() {
            return Err(Error::ExpiredState);
        }
        if state.microphone_permission() != Permission::Allowed {
            return Err(Error::PrivacyClosed);
        }
        Ok(state)
    }

    /// Call on decode failure, control-transport loss, or a local worker fault.
    /// Merely ignoring a malformed control packet could otherwise leave a prior
    /// listening/speaking cue alive until its old lease expires.
    pub fn invalidate(&mut self) {
        self.state = None;
        self.faulted = true;
    }

    pub fn is_faulted(&self) -> bool {
        self.faulted
    }

    /// For diagnostics/cue selection only. Freshness still requires `now`, and
    /// every conversational actuator write still requires `check`.
    pub fn state(&self) -> Option<Snapshot> {
        self.state
    }

    fn advance(&mut self, now: MonoTime) -> Result<(), Error> {
        if self.faulted {
            return Err(Error::Faulted);
        }
        if now < self.last_now {
            self.invalidate();
            return Err(Error::ClockRegression);
        }
        self.last_now = now;
        Ok(())
    }
}
