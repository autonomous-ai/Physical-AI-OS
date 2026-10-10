use lamp_interaction::{BootId, CameraGrant, MonoTime};
use std::{fmt, num::NonZeroU64};

pub const MAX_FRAME_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_WIDTH: u32 = 1280;
pub const MAX_HEIGHT: u32 = 720;
pub const POLL_TARGET_US: u64 = 2_000;
pub const REVOKE_TARGET_US: u64 = 20_000;
pub const READ_BUDGET_US: u64 = 10_000;
pub const START_BUDGET_US: u64 = 100_000;
pub const STOP_BUDGET_US: u64 = 20_000;
pub const FIRST_FRAME_DEADLINE_US: u64 = 2_000_000;
pub const FRAME_SILENCE_DEADLINE_US: u64 = 1_000_000;
pub const MAX_DEQUEUE_AGE_US: u64 = 250_000;

/// A provisioned label checked against the port, not authenticated hardware.
/// No path discovery, alias fallback, or USB identity inference happens here.
#[derive(Clone, Copy, Eq, PartialEq)]
pub struct SourceId {
    bytes: [u8; 128],
    len: u8,
}

impl SourceId {
    pub fn new(value: &str) -> Result<Self, Error> {
        if value.is_empty()
            || value.len() > 128
            || !value
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"/_:.-".contains(&b))
        {
            return Err(Error::new(ErrorKind::InvalidConfig("invalid source label")));
        }
        let mut bytes = [0; 128];
        bytes[..value.len()].copy_from_slice(value.as_bytes());
        Ok(Self {
            bytes,
            len: value.len() as u8,
        })
    }

    pub fn as_str(&self) -> &str {
        // Construction admits ASCII only; no unchecked conversion is needed.
        std::str::from_utf8(&self.bytes[..usize::from(self.len)]).expect("ASCII source label")
    }
}

impl fmt::Debug for SourceId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.as_str().fmt(f)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PixelFormat(pub [u8; 4]);
impl PixelFormat {
    pub const MJPG: Self = Self(*b"MJPG");
}

/// Rational seconds per frame; no default frame rate is invented.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FrameInterval {
    numerator: u32,
    denominator: u32,
}
impl FrameInterval {
    pub fn new(numerator: u32, denominator: u32) -> Result<Self, Error> {
        if numerator == 0 || denominator == 0 {
            return Err(Error::new(ErrorKind::InvalidConfig("zero frame interval")));
        }
        Ok(Self {
            numerator,
            denominator,
        })
    }
    pub fn numerator(self) -> u32 {
        self.numerator
    }
    pub fn denominator(self) -> u32 {
        self.denominator
    }
    pub(crate) fn equivalent(self, other: Self) -> bool {
        u64::from(self.numerator) * u64::from(other.denominator)
            == u64::from(other.numerator) * u64::from(self.denominator)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CaptureConfig {
    pub source: SourceId,
    pub width: u32,
    pub height: u32,
    pub interval: Option<FrameInterval>,
    pub max_frame_bytes: usize,
    pub buffers: u8,
}
impl CaptureConfig {
    pub fn validate(self) -> Result<(), Error> {
        if self.width == 0
            || self.width > MAX_WIDTH
            || self.height == 0
            || self.height > MAX_HEIGHT
            || self.max_frame_bytes < 4
            || self.max_frame_bytes > MAX_FRAME_BYTES
            || !(2..=4).contains(&self.buffers)
        {
            return Err(Error::new(ErrorKind::InvalidConfig(
                "capture bounds exceeded",
            )));
        }
        Ok(())
    }
}

/// The backend must report actual negotiation, including unknown cadence.
/// A requested size/rate is never substituted for an unobserved negotiated one.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct NegotiatedMode {
    pub source: SourceId,
    pub format: PixelFormat,
    pub width: u32,
    pub height: u32,
    pub interval: Option<FrameInterval>,
    pub size_image: usize,
    pub buffers: u8,
}
impl NegotiatedMode {
    pub(crate) fn validate(self, config: CaptureConfig) -> Result<(), Error> {
        if self.source != config.source
            || self.format != PixelFormat::MJPG
            || self.width != config.width
            || self.height != config.height
            || self.size_image < 4
            || self.size_image > config.max_frame_bytes
            || !(2..=config.buffers).contains(&self.buffers)
            || config.interval.is_some_and(|requested| {
                self.interval
                    .is_none_or(|actual| !actual.equivalent(requested))
            })
        {
            return Err(Error::new(ErrorKind::IncompatibleNegotiation));
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CaptureId {
    pub worker: BootId,
    pub epoch: NonZeroU64,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FrameId {
    pub capture: CaptureId,
    pub sequence: NonZeroU64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TimestampDomain {
    /// Driver explicitly reports the same OS CLOCK_MONOTONIC domain as Clock.
    HostMonotonic,
    Realtime,
    Unknown,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TimestampPoint {
    StartOfExposure,
    EndOfFrame,
    Unknown,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DriverTimestamp {
    pub micros: u64,
    pub domain: TimestampDomain,
    pub point: TimestampPoint,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PortFrame {
    pub capture: CaptureId,
    pub source: SourceId,
    pub bytes_used: usize,
    pub driver_sequence: u32,
    pub timestamp: Option<DriverTimestamp>,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CallTiming {
    pub started_at: MonoTime,
    pub completed_at: MonoTime,
}
impl CallTiming {
    pub fn elapsed_us(self) -> u64 {
        self.completed_at
            .as_micros()
            .saturating_sub(self.started_at.as_micros())
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct OperationBudget {
    pub started_at: MonoTime,
    pub deadline: MonoTime,
}

/// A trusted, nonblocking capture port. One owner calls it serially.
///
/// `start` must bind a newly cleared capture queue to the supplied ID; retained
/// earlier-epoch buffers must not be relabeled. No fallback source or USB reset.
/// `try_frame` performs at most one bounded read and returns None on WouldBlock.
/// It must not allocate frame storage, exceed the supplied slice, wait for a
/// frame, or decode/infer. A reported size is validated again by this layer.
/// `stop` discards queued capture and releases resources, including after failed
/// start. No retries or automatic restart. Resource release on Drop must not
/// restart streaming; explicit stop is required to surface cleanup errors.
/// These are implementation obligations, not proof that a physical port obeys.
pub trait PortIo {
    fn start(
        &mut self,
        config: CaptureConfig,
        capture: CaptureId,
        budget: OperationBudget,
    ) -> Result<NegotiatedMode, PortError>;
    fn try_frame(
        &mut self,
        destination: &mut [u8],
        budget: OperationBudget,
    ) -> Result<Option<PortFrame>, PortError>;
    fn stop(&mut self, budget: OperationBudget) -> Result<(), PortError>;
}

/// Use one OS monotonic domain shared with the controller, never wall time or
/// an instance-relative Instant. Linux `SystemClock` uses lamp-ipc CLOCK_MONOTONIC.
pub trait Clock {
    fn now(&mut self) -> Result<MonoTime, ClockError>;
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ClockError;
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PortError {
    Disconnected,
    Unsupported,
    Io(i32),
    InvalidData,
    Cancelled,
    DeadlineExceeded,
}

impl fmt::Display for PortError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "camera port: {self:?}")
    }
}
impl std::error::Error for PortError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FrameObservation {
    pub id: FrameId,
    pub source: SourceId,
    pub grant: CameraGrant,
    pub mode: NegotiatedMode,
    pub driver_sequence: u32,
    pub driver_timestamp: Option<DriverTimestamp>,
    /// Host call boundaries, not sensor exposure or optical acquisition time.
    pub dequeue: CallTiming,
}
impl FrameObservation {
    /// Only host queue age; not evidence that the visible scene is this recent.
    pub fn dequeue_age_us(self, now: MonoTime) -> Option<u64> {
        now.as_micros()
            .checked_sub(self.dequeue.completed_at.as_micros())
    }
}

/// Opaque, non-cloneable handoff identity. It authorizes no downstream output.
#[derive(Debug, Eq, PartialEq)]
pub struct DeliveryToken(pub(crate) FrameId);
impl DeliveryToken {
    pub fn frame_id(&self) -> FrameId {
        self.0
    }
}
#[derive(Debug)]
pub struct FrameView<'a> {
    pub observation: FrameObservation,
    pub bytes: &'a [u8],
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Phase {
    Closed,
    Acquiring,
    Ready,
    Faulted,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Status {
    pub phase: Phase,
    pub capture: Option<CaptureId>,
    pub latest_pending: Option<FrameId>,
    pub delivery_in_flight: Option<FrameId>,
}
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct Counters {
    pub frames_received: u64,
    pub pending_replaced: u64,
    pub aged_out: u64,
    pub invalidated: u64,
    pub delivery_completed: u64,
    pub driver_sequence_gaps: u64,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StartReport {
    pub capture: CaptureId,
    pub mode: NegotiatedMode,
    pub timing: CallTiming,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PollReport {
    pub frame: Option<FrameObservation>,
    pub read_timing: CallTiming,
    /// Final host permission/budget check after local retention work.
    pub completed_at: MonoTime,
    pub status: Status,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StopReport {
    pub timing: Option<CallTiming>,
    pub invalidated_frames: u64,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ErrorKind {
    InvalidConfig(&'static str),
    AllocationFailed,
    IncompatibleNegotiation,
    Authority(lamp_interaction::Error),
    ClockUnavailable,
    ClockRegression,
    Closed,
    AlreadyStarted,
    Faulted,
    CounterExhausted,
    Port(PortError),
    StartBudgetExceeded,
    ReadBudgetExceeded,
    StopBudgetExceeded,
    FirstFrameTimeout,
    FrameSilenceTimeout,
    OldCapture,
    WrongSource,
    InvalidFrameSize,
    InvalidJpegEnvelope,
    OldDriverSequence,
    InvalidDriverTimestamp,
    DeliveryBusy,
    StaleDelivery,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CleanupFailure {
    pub port: Option<PortError>,
    pub clock: Option<ErrorKind>,
    pub timing: Option<CallTiming>,
    pub budget_exceeded: bool,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Error {
    pub kind: ErrorKind,
    pub cleanup: Option<CleanupFailure>,
}
impl Error {
    pub(crate) fn new(kind: ErrorKind) -> Self {
        Self {
            kind,
            cleanup: None,
        }
    }
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "camera: {:?}", self.kind)?;
        if let Some(cleanup) = self.cleanup {
            write!(f, "; cleanup: {cleanup:?}")?;
        }
        Ok(())
    }
}
impl std::error::Error for Error {}
