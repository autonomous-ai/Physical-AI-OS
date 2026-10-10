use serde::{Deserialize, Serialize};
use std::{fmt, num::NonZeroU64};

/// Upper bounds, not measured device timings. Refresh leases before they expire.
pub const MAX_INPUT_LEASE_US: u64 = 1_000_000;
pub const MAX_AUTHORITY_LEASE_US: u64 = 250_000;
pub const MAX_OUTPUT_LEASE_US: u64 = 250_000;

/// Unique controller/worker incarnation, supplied by its trusted supervisor.
///
/// A restart must use a fresh, nonzero ID; this type does not generate randomness.
/// An old ID must never be reused. IDs are not secrets or authentication tokens.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(try_from = "[u8; 16]", into = "[u8; 16]")]
pub struct BootId([u8; 16]);

impl BootId {
    pub fn new(bytes: [u8; 16]) -> Result<Self, Error> {
        if bytes == [0; 16] {
            return Err(Error::InvalidBoot);
        }
        Ok(Self(bytes))
    }

    pub fn bytes(self) -> [u8; 16] {
        self.0
    }
}

impl TryFrom<[u8; 16]> for BootId {
    type Error = Error;
    fn try_from(value: [u8; 16]) -> Result<Self, Self::Error> {
        Self::new(value)
    }
}

impl From<BootId> for [u8; 16] {
    fn from(value: BootId) -> Self {
        value.0
    }
}

/// Microseconds in one OS monotonic-clock domain shared by all processes.
///
/// Do not use wall time or a process-relative `Instant::elapsed()` value. Public
/// controller/guard method timestamps are trusted *receipt/check* time. Sensor
/// acquisition time is carried separately by `Observation`.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(transparent)]
pub struct MonoTime(u64);

impl MonoTime {
    pub const fn from_micros(value: u64) -> Self {
        Self(value)
    }

    pub const fn as_micros(self) -> u64 {
        self.0
    }

    pub fn checked_add(self, micros: u64) -> Option<Self> {
        self.0.checked_add(micros).map(Self)
    }

    pub(crate) fn elapsed_since(self, earlier: Self) -> Option<u64> {
        self.0.checked_sub(earlier.0)
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Permission {
    #[default]
    Unknown,
    Denied,
    Allowed,
}

/// The capture worker can retain the opening words until this lease expires.
/// Being initialized or merely owning a microphone is not readiness.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CaptureState {
    Unavailable,
    RetainingUntil(MonoTime),
}

/// The input path can accept retained audio without waiting for an answer.
/// This describes bounded input capacity, not whether Gemini is currently busy.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AdmissionState {
    Unavailable,
    OpenUntil(MonoTime),
}

/// Supplied only after the separate addressee/turn classifier admits this input.
/// A colleague's conversation, media, or a listener acknowledgment must not be
/// manufactured into one of these events by the controller.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AdmittedInput {
    NewTurn,
    Interruption,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TurnOwner {
    pub(crate) boot: BootId,
    pub(crate) turn: NonZeroU64,
    pub(crate) generation: NonZeroU64,
}

impl TurnOwner {
    pub fn boot(self) -> BootId {
        self.boot
    }
    pub fn turn(self) -> u64 {
        self.turn.get()
    }
    pub fn generation(self) -> u64 {
        self.generation.get()
    }
}

/// Camera-derived work retains this grant from the time it accepted a frame.
/// Closing and reopening camera privacy never makes an earlier grant valid.
/// A grant checks permission lineage; observation freshness is a separate check.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CameraGrant {
    pub(crate) boot: BootId,
    pub(crate) generation: NonZeroU64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OutputKind {
    Speech,
    Light,
    Motion,
}

/// Immutable lineage captured when the controller plans asynchronous output.
/// Keep this token through preparation and renewal; a late callback may not
/// borrow a newer turn, presentation phase, or camera permission generation.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutputOwner {
    pub(crate) owner: TurnOwner,
    pub(crate) kind: OutputKind,
    pub(crate) camera: Option<CameraGrant>,
    pub(crate) presentation_generation: NonZeroU64,
}

impl OutputOwner {
    pub fn owner(self) -> TurnOwner {
        self.owner
    }
    pub fn kind(self) -> OutputKind {
        self.kind
    }
    pub fn camera(self) -> Option<CameraGrant> {
        self.camera
    }
}

/// An immutable authorization for bounded work, never a standing authorization.
///
/// `Light` means a conversational cue; safety/privacy/off indicators are not
/// conversational output and need their own explicit local status policy.
/// A permit does not itself request a filler, gesture, light change, or speech.
/// Nonverbal permits also retain the presentation generation: input-end and
/// actual playback transitions invalidate older same-turn cues. Keep the permit
/// with delayed work; do not remint a late cue from the currently visible state.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutputPermit {
    pub(crate) owner: TurnOwner,
    pub(crate) revision: NonZeroU64,
    pub(crate) presentation_generation: NonZeroU64,
    pub(crate) kind: OutputKind,
    pub(crate) issued_at: MonoTime,
    pub(crate) expires_at: MonoTime,
    pub(crate) camera: Option<CameraGrant>,
}

impl OutputPermit {
    pub fn owner(self) -> TurnOwner {
        self.owner
    }
    pub fn kind(self) -> OutputKind {
        self.kind
    }
    pub fn issued_at(self) -> MonoTime {
        self.issued_at
    }
    pub fn expires_at(self) -> MonoTime {
        self.expires_at
    }
    pub fn camera(self) -> Option<CameraGrant> {
        self.camera
    }

    pub(crate) fn validate_shape(self) -> Result<(), Error> {
        if !valid_lease(self.issued_at, self.expires_at, MAX_OUTPUT_LEASE_US)
            || self
                .camera
                .is_some_and(|camera| camera.boot != self.owner.boot)
        {
            return Err(Error::MalformedState);
        }
        Ok(())
    }
}

/// An actual playback occurrence, distinct from its conversational turn.
/// An underrun/end callback for an earlier occurrence cannot stop a later one.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaybackToken {
    pub(crate) owner: TurnOwner,
    pub(crate) sequence: NonZeroU64,
}

impl PlaybackToken {
    pub fn owner(self) -> TurnOwner {
        self.owner
    }
    pub fn sequence(self) -> u64 {
        self.sequence.get()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct TurnState {
    pub owner: TurnOwner,
    pub input_active: bool,
    pub playback: Option<PlaybackToken>,
}

/// A short authority lease sent on the priority control path to output workers.
///
/// Serialization is strict about unknown fields and enum names. Deserialization
/// alone is not validation: `BoundaryGuard::install` checks all invariants.
/// Never restore controller authority from a serialized snapshot after restart.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Snapshot {
    pub(crate) boot: BootId,
    pub(crate) revision: NonZeroU64,
    pub(crate) generation: NonZeroU64,
    pub(crate) turn_counter: u64,
    pub(crate) issued_at: MonoTime,
    pub(crate) expires_at: MonoTime,
    pub(crate) microphone: Permission,
    pub(crate) microphone_generation: NonZeroU64,
    pub(crate) camera: Permission,
    pub(crate) camera_generation: NonZeroU64,
    pub(crate) presentation_generation: NonZeroU64,
    pub(crate) capture_until: Option<MonoTime>,
    pub(crate) admission_until: Option<MonoTime>,
    pub(crate) turn: Option<TurnState>,
}

impl Snapshot {
    pub fn boot(self) -> BootId {
        self.boot
    }
    pub fn revision(self) -> u64 {
        self.revision.get()
    }
    pub fn generation(self) -> u64 {
        self.generation.get()
    }
    pub fn issued_at(self) -> MonoTime {
        self.issued_at
    }
    pub fn expires_at(self) -> MonoTime {
        self.expires_at
    }
    pub fn microphone_permission(self) -> Permission {
        self.microphone
    }
    /// Changes on every microphone privacy transition, independent of turns.
    /// Capture workers discard buffered audio when this changes, even if a
    /// denied/reopened pair was coalesced before their next state receipt.
    pub fn microphone_generation(self) -> u64 {
        self.microphone_generation.get()
    }
    pub fn camera_permission(self) -> Permission {
        self.camera
    }
    pub fn owner(self) -> Option<TurnOwner> {
        self.turn.map(|turn| turn.owner)
    }
    pub fn input_active(self) -> bool {
        self.turn.is_some_and(|turn| turn.input_active)
    }
    pub fn playback(self) -> Option<PlaybackToken> {
        self.turn.and_then(|turn| turn.playback)
    }

    /// Computed from fresh authority and both input leases, independent of speech.
    pub fn listening_ready(self, now: MonoTime) -> bool {
        self.current_at(now)
            && self.microphone == Permission::Allowed
            && self.capture_until.is_some_and(|until| now < until)
            && self.admission_until.is_some_and(|until| now < until)
    }

    pub fn camera_allowed(self, now: MonoTime) -> bool {
        self.current_at(now) && self.camera == Permission::Allowed
    }

    pub(crate) fn current_at(self, now: MonoTime) -> bool {
        self.issued_at <= now && now < self.expires_at
    }

    pub(crate) fn validate_shape(self) -> Result<(), Error> {
        let input_leases_valid = [self.capture_until, self.admission_until]
            .into_iter()
            .flatten()
            .all(|until| valid_lease(self.issued_at, until, MAX_INPUT_LEASE_US));
        if !valid_lease(self.issued_at, self.expires_at, MAX_AUTHORITY_LEASE_US)
            || !input_leases_valid
            || (self.microphone != Permission::Allowed && self.capture_until.is_some())
        {
            return Err(Error::MalformedState);
        }
        if let Some(turn) = self.turn
            && (turn.owner.boot != self.boot
                || turn.owner.generation != self.generation
                || turn.owner.turn.get() != self.turn_counter
                || !self.listening_ready(self.issued_at)
                || (turn.input_active && turn.playback.is_some())
                || turn
                    .playback
                    .is_some_and(|playback| playback.owner != turn.owner)
                || self
                    .capture_until
                    .is_some_and(|until| self.expires_at > until)
                || self
                    .admission_until
                    .is_some_and(|until| self.expires_at > until))
        {
            return Err(Error::MalformedState);
        }
        Ok(())
    }

    pub(crate) fn check_permit(
        self,
        now: MonoTime,
        permit: OutputPermit,
        kind: OutputKind,
    ) -> Result<(), Error> {
        self.check_permit_envelope(now, permit, kind)?;
        let turn = self.turn.ok_or(Error::StaleOwner)?;
        if turn.owner != permit.owner {
            return Err(Error::StaleOwner);
        }
        if !self.listening_ready(now) {
            return Err(Error::InputUnavailable);
        }
        if kind != OutputKind::Speech
            && permit.presentation_generation != self.presentation_generation
        {
            return Err(Error::StalePresentation);
        }
        if kind == OutputKind::Speech && turn.input_active {
            return Err(Error::InputStillActive);
        }
        self.check_permit_camera(now, permit)
    }

    // Shares only hard permit checks. The ordinary write check above retains
    // its strict owner/input rules and its existing error ordering.
    fn check_permit_envelope(
        self,
        now: MonoTime,
        permit: OutputPermit,
        kind: OutputKind,
    ) -> Result<(), Error> {
        permit.validate_shape()?;
        if permit.owner.boot != self.boot {
            return Err(Error::WrongBoot);
        }
        if permit.kind != kind {
            return Err(Error::WrongOutputKind);
        }
        if permit.issued_at > now {
            return Err(Error::FuturePermit);
        }
        if permit.expires_at <= now {
            return Err(Error::ExpiredPermit);
        }
        if permit.revision > self.revision {
            return Err(Error::StateTooOld);
        }
        Ok(())
    }

    fn check_permit_camera(self, now: MonoTime, permit: OutputPermit) -> Result<(), Error> {
        if let Some(camera) = permit.camera
            && (camera.boot != self.boot
                || camera.generation != self.camera_generation
                || !self.camera_allowed(now))
        {
            return Err(Error::StaleCameraGrant);
        }
        Ok(())
    }

    /// Does not authorize a write. Only an already accepted, immutable speech
    /// tail may outlive its conversational owner, under a newer admitted owner.
    /// Check hard fences before ownership so StaleOwner cannot hide revocation.
    pub(crate) fn check_accepted_speech(
        self,
        now: MonoTime,
        permit: OutputPermit,
    ) -> Result<TurnOwner, Error> {
        self.check_permit_envelope(now, permit, OutputKind::Speech)?;
        if !self.listening_ready(now) {
            return Err(Error::InputUnavailable);
        }
        self.check_permit_camera(now, permit)?;
        let successor = self.owner().ok_or(Error::StaleOwner)?;
        if successor.turn <= permit.owner.turn || successor.generation <= permit.owner.generation {
            return Err(Error::StaleOwner);
        }
        Ok(successor)
    }
}

pub(crate) fn valid_lease(start: MonoTime, end: MonoTime, max_us: u64) -> bool {
    end.elapsed_since(start)
        .is_some_and(|duration| duration > 0 && duration <= max_us)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    InvalidBoot,
    InvalidLease,
    ClockRegression,
    Faulted,
    CounterExhausted,
    PrivacyClosed,
    InputUnavailable,
    StaleOwner,
    InputStillActive,
    StaleCameraGrant,
    StalePresentation,
    MalformedState,
    StaleSnapshot,
    StateRegression,
    WrongBoot,
    FutureState,
    ExpiredState,
    WrongOutputKind,
    FuturePermit,
    ExpiredPermit,
    StateTooOld,
    NoAuthority,
    StalePlayback,
    AlreadyPlaying,
    InvalidConfidence,
    WrongObservationSource,
    OutOfOrderObservation,
    FutureObservation,
    InvalidFreshnessPolicy,
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let message = match self {
            Self::InvalidBoot => "boot ID must be nonzero and unique for this incarnation",
            Self::InvalidLease => "lease is empty, expired, overflowing, or above its bound",
            Self::ClockRegression => "trusted monotonic clock regressed; authority is faulted",
            Self::Faulted => "authority is faulted and requires a fresh controller incarnation",
            Self::CounterExhausted => "ownership counter exhausted; authority is faulted",
            Self::PrivacyClosed => "microphone privacy does not permit capture",
            Self::InputUnavailable => "input cannot currently be retained and admitted",
            Self::StaleOwner => "turn ownership has been revoked or superseded",
            Self::InputStillActive => "speech is not permitted before admitted input ends",
            Self::StaleCameraGrant => "camera grant was revoked or is unavailable",
            Self::StalePresentation => "nonverbal output belongs to an earlier interaction phase",
            Self::MalformedState => "authority state or output permit violates its invariants",
            Self::StaleSnapshot => "authority revision is not newer than the installed revision",
            Self::StateRegression => "authority snapshot regresses ownership or privacy lineage",
            Self::WrongBoot => "message belongs to a different process incarnation",
            Self::FutureState => "authority snapshot is from the future",
            Self::ExpiredState => "authority snapshot has expired",
            Self::WrongOutputKind => "output permit belongs to a different actuator class",
            Self::FuturePermit => "output permit is from the future",
            Self::ExpiredPermit => "output permit has expired",
            Self::StateTooOld => "output requires an authority revision not yet installed",
            Self::NoAuthority => "output boundary has no installed authority",
            Self::StalePlayback => "playback callback belongs to an earlier occurrence",
            Self::AlreadyPlaying => "this turn already has an active playback occurrence",
            Self::InvalidConfidence => "confidence must be between zero and one thousand",
            Self::WrongObservationSource => "observation belongs to a different worker incarnation",
            Self::OutOfOrderObservation => "observation sequence or acquisition time regressed",
            Self::FutureObservation => "observation acquisition time is after receipt time",
            Self::InvalidFreshnessPolicy => {
                "freshness policy has an invalid age or confidence bound"
            }
        };
        f.write_str(message)
    }
}

impl std::error::Error for Error {}
