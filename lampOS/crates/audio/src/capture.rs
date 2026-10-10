//! Bounded Linux ALSA capture for a supervised, dedicated microphone process.
//!
//! Construction opens nothing. Install fresh trusted microphone authority, then
//! explicitly `start`. An open handle is not listening readiness or permission
//! to send audio to a provider. The supervisor must drain priority privacy state
//! first and call `maintain` at least every 10 ms, even with no consumer demand.
//! No provider calls, mixer changes, implicit device fallback, or ALSA recovery
//! happen here. One supervisor must own the device; an alias is not an exclusive
//! ownership mechanism. Use the explicitly provisioned alias (legacy primary capture was
//! `plug:device_micro2`; the installed board routing must be verified separately).
//!
//! Each read performs at most one nonblocking ALSA read into a fixed 160-sample
//! assembler. Requested buffers contain 2..=4 ten-millisecond blocks; accepted
//! periods are 1..=160 frames and buffers 320..=640 frames, capped by the request.
//! ALSA opening, configuration and kernel calls have no userspace hard deadline;
//! process supervision must enforce a stall timeout. Host read times and ALSA's
//! optional status clock are recorded separately. Neither is a measured acoustic
//! acquisition timestamp. A 10 ms read budget is checked after the call returns.
//!
//! Revocation, expiry and capture faults close the handle and erase local partial
//! audio. A caller must purge its own queued frames and reset AEC on discontinuity,
//! call `reset`, then explicitly `start` under fresh permitted authority. Returned
//! frames cannot be retracted by this library. Every reopen has a new epoch. A
//! microphone privacy-generation change closes capture even when a denied-to-
//! allowed interval was coalesced into one snapshot. Normal conversation changes
//! do not discard opening words. Control loss latches closed and requires a newly
//! supervised instance, not merely `reset`.
use crate::{CAPTURE_RATE, CAPTURE_SAMPLES, CaptureBlock};
use alsa::{
    Direction, ValueOr,
    pcm::{Access, Format, HwParams, PCM, State, TstampType},
};
use lamp_interaction::{BootId, BoundaryGuard, MonoTime, Permission, Snapshot};
use lamp_ipc::monotonic_us;
use serde::Serialize;
use std::{fmt, io};

const MAX_READ_US: u64 = 10_000;
const MIN_BUFFER_FRAMES: i64 = (CAPTURE_SAMPLES * 2) as i64;
const MAX_BUFFER_FRAMES: i64 = (CAPTURE_SAMPLES * 4) as i64;

pub type Result<T> = std::result::Result<T, CaptureError>;

#[derive(Clone, Copy, Debug)]
pub struct CaptureConfig {
    buffer_blocks: u8,
}

impl CaptureConfig {
    pub fn new(buffer_blocks: u8) -> Result<Self> {
        if !(2..=4).contains(&buffer_blocks) {
            return Err(CaptureError::new(ErrorKind::InvalidConfig(
                "capture buffer must contain 2..=4 ten-millisecond blocks",
            )));
        }
        Ok(Self { buffer_blocks })
    }

    pub fn buffer_blocks(self) -> u8 {
        self.buffer_blocks
    }
}

impl Default for CaptureConfig {
    fn default() -> Self {
        Self { buffer_blocks: 4 }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct CaptureFormat {
    pub rate: u32,
    pub channels: u32,
    pub period_frames: i64,
    pub buffer_frames: i64,
    /// True only after ALSA accepts and reports enabled monotonic timestamps.
    pub monotonic_status_timestamps: bool,
}

impl CaptureFormat {
    fn buffer_us(self) -> u64 {
        self.buffer_frames as u64 * 1_000_000 / u64::from(self.rate)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub enum Discontinuity {
    PrivacyRevoked,
    AuthorityExpired,
    ControlFault,
    ClockRegression,
    XRun,
    Suspended,
    Disconnected,
    DeviceError,
    PartialTimeout,
    ReadBudgetExceeded,
    InvalidRead,
    CounterExhausted,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ErrorKind {
    InvalidConfig(&'static str),
    UnsupportedFormat(CaptureFormat),
    Authority(lamp_interaction::Error),
    NotPermitted,
    Closed,
    AlreadyOpen,
    NotStarted,
    ResetRequired(Discontinuity),
    Discontinuity(Discontinuity),
}

#[derive(Debug)]
pub struct CaptureError {
    pub kind: ErrorKind,
    pub alsa_error: Option<alsa::Error>,
    /// A failed ALSA discard is reported even though the handle is then closed.
    pub close_error: Option<alsa::Error>,
}

impl CaptureError {
    fn new(kind: ErrorKind) -> Self {
        Self {
            kind,
            alsa_error: None,
            close_error: None,
        }
    }

    fn device(error: alsa::Error) -> Self {
        Self {
            kind: ErrorKind::Discontinuity(classify_device_error(&error)),
            alsa_error: Some(error),
            close_error: None,
        }
    }
}

impl fmt::Display for CaptureError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "capture: {:?}", self.kind)?;
        if let Some(error) = &self.alsa_error {
            write!(f, ": {error}")?;
        }
        if let Some(error) = &self.close_error {
            write!(f, "; discard also failed: {error}")?;
        }
        Ok(())
    }
}

impl std::error::Error for CaptureError {}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct PrepareReport {
    pub epoch: u64,
    pub format: CaptureFormat,
    pub open_started_at_us: u64,
    pub prepared_at_us: u64,
    pub microphone_generation: u64,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct StartReport {
    pub epoch: u64,
    pub format: CaptureFormat,
    pub open_started_at_us: u64,
    pub prepared_at_us: u64,
    /// Host time immediately before snd_pcm_start, not ADC acquisition.
    pub capture_started_at_us: u64,
    pub microphone_generation: u64,
    /// Completion of start, not proof of retained audio or listening readiness.
    pub start_completed_at_us: u64,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct StateUpdate {
    /// Purge downstream frames/reset AEC on privacy closure, even if this is zero.
    pub partial_samples_discarded: usize,
    pub closed: bool,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct ReadTiming {
    /// First successful host read that contributed to this frame.
    pub first_read_started_at_us: u64,
    pub last_read_started_at_us: u64,
    pub read_completed_at_us: u64,
    pub successful_reads: u16,
    /// Driver-reported status clock, not sample acquisition time.
    pub alsa_status_monotonic_us: Option<u64>,
    /// Host time when the post-read status observation completed.
    pub status_observed_at_us: u64,
    pub available_frames: i64,
    pub delayed_frames: i64,
}

#[derive(Clone, Debug)]
pub struct CapturedFrame {
    pub samples: CaptureBlock,
    pub epoch: u64,
    pub sequence: u64,
    pub authority_generation: u64,
    pub microphone_generation: u64,
    pub timing: ReadTiming,
}

pub struct Microphone {
    device: String,
    config: CaptureConfig,
    guard: BoundaryGuard,
    last_now: MonoTime,
    revoked_revision: u64,
    pcm: Option<PCM>,
    format: Option<CaptureFormat>,
    partial: Assembler,
    epoch: u64,
    sequence: u64,
    reset_required: Option<Discontinuity>,
    prepared: Option<PrepareReport>,
    running: bool,
}

impl Microphone {
    /// Validate only; no ALSA handle is opened and no microphone samples are read.
    pub fn new(device: &str, boot: BootId, config: CaptureConfig) -> Result<Self> {
        if device.is_empty()
            || device.len() > 128
            || device
                .bytes()
                .any(|byte| byte == 0 || byte.is_ascii_control())
        {
            return Err(CaptureError::new(ErrorKind::InvalidConfig(
                "an explicit ALSA alias of 1..=128 non-control bytes is required",
            )));
        }
        let now = now();
        Ok(Self {
            device: device.into(),
            config,
            guard: BoundaryGuard::new(boot, now),
            last_now: now,
            revoked_revision: 0,
            pcm: None,
            format: None,
            partial: Assembler::default(),
            epoch: 0,
            sequence: 0,
            reset_required: None,
            prepared: None,
            running: false,
        })
    }

    /// Rejected stale/duplicate state cannot interrupt a newer valid capture.
    /// Installing Allowed state never opens the microphone implicitly.
    pub fn install(&mut self, snapshot: Snapshot) -> Result<StateUpdate> {
        let now = self.advance()?;
        let previous_generation = self.guard.state().map(Snapshot::microphone_generation);
        if let Err(error) = self.guard.install(now, snapshot) {
            if self.guard.is_faulted() {
                return Err(self.fail(
                    Discontinuity::ControlFault,
                    CaptureError::new(ErrorKind::Authority(error)),
                ));
            }
            self.maintain()?;
            return Err(CaptureError::new(ErrorKind::Authority(error)));
        }
        let privacy_changed =
            previous_generation.is_some_and(|old| old != snapshot.microphone_generation());
        if self.is_open()
            && (privacy_changed || snapshot.microphone_permission() != Permission::Allowed)
        {
            let discarded = self.partial.len;
            self.close(Discontinuity::PrivacyRevoked)?;
            return Ok(StateUpdate {
                partial_samples_discarded: discarded,
                closed: true,
            });
        }
        self.maintain()?;
        Ok(StateUpdate::default())
    }

    /// Explicitly open and start capture; fresh microphone permission is enough.
    /// Requiring listening_ready here would create a readiness dependency cycle.
    pub fn start(&mut self) -> Result<StartReport> {
        if !self.is_open() {
            self.prepare()?;
        }
        self.start_prepared()
    }

    /// Open/configure without starting acquisition or reading samples. This
    /// keeps potentially slow device setup outside the speaker/capture clock
    /// barrier. A prepared handle is explicitly not listening readiness.
    pub fn prepare(&mut self) -> Result<PrepareReport> {
        self.require_permission()?;
        if self.is_open() {
            return Err(CaptureError::new(ErrorKind::AlreadyOpen));
        }
        if let Some(reason) = self.reset_required {
            return Err(CaptureError::new(ErrorKind::ResetRequired(reason)));
        }
        let epoch = self.epoch.checked_add(1).ok_or_else(|| {
            self.fail(
                Discontinuity::CounterExhausted,
                CaptureError::new(ErrorKind::Discontinuity(Discontinuity::CounterExhausted)),
            )
        })?;
        let open_started_at_us = monotonic_us();
        let opened = PCM::new(&self.device, Direction::Capture, true);
        self.pcm = match opened {
            Ok(pcm) => Some(pcm),
            Err(error) => return Err(self.device_failed(error)),
        };
        let format = match configure(self.pcm.as_ref().expect("just opened"), self.config) {
            Ok(format) => format,
            Err(error) => return Err(self.fail(Discontinuity::DeviceError, error)),
        };
        self.format = Some(format);
        self.require_permission()?;
        self.epoch = epoch;
        self.sequence = 0;
        self.partial.clear();
        let report = PrepareReport {
            epoch,
            format,
            open_started_at_us,
            prepared_at_us: monotonic_us(),
            microphone_generation: self
                .guard
                .state()
                .expect("permission checked")
                .microphone_generation(),
        };
        self.prepared = Some(report);
        Ok(report)
    }

    /// Start an already configured microphone only after the speaker reference
    /// barrier. The first returned block is sequence 1; no warmup frames are read
    /// or discarded. Fresh permission is checked around the start syscall.
    pub fn start_prepared(&mut self) -> Result<StartReport> {
        self.require_permission()?;
        if self.running {
            return Err(CaptureError::new(ErrorKind::AlreadyOpen));
        }
        let prepared = self
            .prepared
            .ok_or_else(|| CaptureError::new(ErrorKind::Closed))?;
        let capture_started_at_us = monotonic_us();
        if let Err(error) = self
            .pcm
            .as_ref()
            .expect("permission preserved handle")
            .start()
        {
            return Err(self.device_failed(error));
        }
        self.require_permission()?;
        self.running = true;
        Ok(StartReport {
            epoch: prepared.epoch,
            format: prepared.format,
            open_started_at_us: prepared.open_started_at_us,
            prepared_at_us: prepared.prepared_at_us,
            capture_started_at_us,
            microphone_generation: self
                .guard
                .state()
                .expect("permission checked")
                .microphone_generation(),
            start_completed_at_us: monotonic_us(),
        })
    }

    /// At most one nonblocking read, no retry loop. None means partial/would-block.
    /// Process priority control first; do not cache permission outside this method.
    pub fn try_read(&mut self) -> Result<Option<CapturedFrame>> {
        self.maintain()?;
        self.require_permission()?;
        if !self.running {
            return Err(CaptureError::new(ErrorKind::NotStarted));
        }
        let Some(pcm) = self.pcm.as_ref() else {
            return Err(CaptureError::new(ErrorKind::Closed));
        };
        let read_started_at_us = monotonic_us();
        let result = match pcm.io_i16() {
            Ok(reader) => reader.readi(self.partial.remaining_mut()),
            Err(error) => Err(error),
        };
        let read_completed_at_us = monotonic_us();
        // A lease can expire during a syscall. Never return that audio.
        self.require_permission()?;
        let frames = match result {
            Ok(frames) => frames,
            Err(error) if would_block(&error) => 0,
            Err(error) => return Err(self.device_failed(error)),
        };
        if read_completed_at_us.saturating_sub(read_started_at_us) > MAX_READ_US {
            return Err(self.discontinued(Discontinuity::ReadBudgetExceeded));
        }
        if frames == 0 {
            // The incomplete block keeps its original deadline across EAGAIN.
            self.maintain()?;
            return Ok(None);
        }
        if let Err(reason) = self
            .partial
            .accept(frames, read_started_at_us, read_completed_at_us)
        {
            return Err(self.discontinued(reason));
        }
        self.maintain()?;
        if self.partial.len != CAPTURE_SAMPLES {
            return Ok(None);
        }
        let status = match self
            .pcm
            .as_ref()
            .expect("maintain preserved handle")
            .status()
        {
            Ok(status) => status,
            Err(error) => return Err(self.device_failed(error)),
        };
        if let Some(reason) = state_discontinuity(status.get_state()) {
            return Err(self.discontinued(reason));
        }
        let status_observed_at_us = monotonic_us();
        self.maintain()?;
        let sequence = self
            .sequence
            .checked_add(1)
            .ok_or_else(|| self.discontinued(Discontinuity::CounterExhausted))?;
        let stamp = status.get_htstamp();
        let driver_time = self
            .format
            .is_some_and(|format| format.monotonic_status_timestamps)
            .then(|| timestamp_us(stamp.tv_sec, stamp.tv_nsec))
            .flatten();
        let frame = CapturedFrame {
            samples: self.partial.samples,
            epoch: self.epoch,
            sequence,
            authority_generation: self.guard.state().expect("permission checked").generation(),
            microphone_generation: self
                .guard
                .state()
                .expect("permission checked")
                .microphone_generation(),
            timing: ReadTiming {
                first_read_started_at_us: self.partial.first_read_started_at_us,
                last_read_started_at_us: read_started_at_us,
                read_completed_at_us,
                successful_reads: self.partial.successful_reads,
                alsa_status_monotonic_us: driver_time,
                status_observed_at_us,
                available_frames: status.get_avail(),
                delayed_frames: status.get_delay(),
            },
        };
        self.partial.clear();
        self.sequence = sequence;
        Ok(Some(frame))
    }

    /// Call every worker tick (target <=10 ms), including while input is idle.
    pub fn maintain(&mut self) -> Result<()> {
        let now = self.advance()?;
        if !self.is_open() {
            return Ok(());
        }
        self.require_permission()?;
        if self.partial.expired(
            now.as_micros(),
            self.format.expect("open is configured").buffer_us(),
        ) {
            return Err(self.discontinued(Discontinuity::PartialTimeout));
        }
        Ok(())
    }

    /// Immediate local privacy action; no provider/network work and no drain.
    /// A newer snapshot and explicit reset/start are required before reopening.
    pub fn revoke(&mut self) -> Result<()> {
        if let Some(state) = self.guard.state() {
            self.revoked_revision = self.revoked_revision.max(state.revision());
        }
        self.close(Discontinuity::PrivacyRevoked)
    }

    /// A missing/broken trusted control stream is a permanent fault of this worker.
    pub fn control_lost(&mut self) -> Result<()> {
        self.guard.invalidate();
        self.close(Discontinuity::ControlFault)
    }

    /// Acknowledge discontinuity only after purging downstream queues/resetting AEC.
    /// Does not reopen or clear a control fault or local privacy revision floor.
    pub fn reset(&mut self) -> Result<()> {
        if self.is_open() {
            return Err(CaptureError::new(ErrorKind::AlreadyOpen));
        }
        if self.guard.is_faulted() {
            return Err(CaptureError::new(ErrorKind::Authority(
                lamp_interaction::Error::Faulted,
            )));
        }
        self.partial.clear();
        self.reset_required = None;
        Ok(())
    }

    pub fn is_open(&self) -> bool {
        self.pcm.is_some()
    }

    pub fn is_running(&self) -> bool {
        self.running
    }

    pub fn reset_required(&self) -> Option<Discontinuity> {
        self.reset_required
    }

    pub fn format(&self) -> Option<CaptureFormat> {
        self.format
    }

    pub fn epoch(&self) -> u64 {
        self.epoch
    }

    fn advance(&mut self) -> Result<MonoTime> {
        let now = now();
        if now < self.last_now {
            self.guard.invalidate();
            return Err(self.discontinued(Discontinuity::ClockRegression));
        }
        self.last_now = now;
        Ok(now)
    }

    fn require_permission(&mut self) -> Result<()> {
        let now = self.advance()?;
        if self.guard.is_faulted() {
            return Err(self.fail(
                Discontinuity::ControlFault,
                CaptureError::new(ErrorKind::Authority(lamp_interaction::Error::Faulted)),
            ));
        }
        let state = self.guard.state();
        let reason = match state {
            Some(state) if state.issued_at() <= now && now < state.expires_at() => {
                if state.microphone_permission() == Permission::Allowed
                    && state.revision() > self.revoked_revision
                {
                    return Ok(());
                }
                Discontinuity::PrivacyRevoked
            }
            Some(_) => Discontinuity::AuthorityExpired,
            None => Discontinuity::ControlFault,
        };
        if self.is_open() {
            Err(self.discontinued(reason))
        } else {
            Err(CaptureError::new(ErrorKind::NotPermitted))
        }
    }

    fn discontinued(&mut self, reason: Discontinuity) -> CaptureError {
        self.fail(reason, CaptureError::new(ErrorKind::Discontinuity(reason)))
    }

    fn device_failed(&mut self, error: alsa::Error) -> CaptureError {
        let reason = classify_device_error(&error);
        self.fail(reason, CaptureError::device(error))
    }

    fn fail(&mut self, reason: Discontinuity, mut error: CaptureError) -> CaptureError {
        if let Err(close) = self.close(reason) {
            error.close_error = close.close_error;
        }
        error
    }

    fn close(&mut self, reason: Discontinuity) -> Result<()> {
        self.partial.clear();
        self.format = None;
        self.prepared = None;
        self.running = false;
        self.reset_required = Some(reason);
        // Taking the handle guarantees closure even when snd_pcm_drop fails.
        if let Some(pcm) = self.pcm.take()
            && let Err(error) = pcm.drop()
        {
            return Err(CaptureError {
                kind: ErrorKind::Discontinuity(reason),
                alsa_error: None,
                close_error: Some(error),
            });
        }
        Ok(())
    }
}

impl Drop for Microphone {
    fn drop(&mut self) {
        // Explicit revoke/control_lost returns discard errors. Destructor cleanup
        // cannot report them; OS handle close is still attempted by PCM's Drop.
        self.partial.clear();
        if let Some(pcm) = self.pcm.take() {
            let _ = pcm.drop();
        }
    }
}

fn configure(pcm: &PCM, config: CaptureConfig) -> Result<CaptureFormat> {
    let hardware = HwParams::any(pcm).map_err(CaptureError::device)?;
    hardware
        .set_access(Access::RWInterleaved)
        .map_err(CaptureError::device)?;
    hardware
        .set_format(Format::s16())
        .map_err(CaptureError::device)?;
    hardware.set_channels(1).map_err(CaptureError::device)?;
    hardware
        .set_rate(CAPTURE_RATE, ValueOr::Nearest)
        .map_err(CaptureError::device)?;
    hardware
        .set_period_size_near(CAPTURE_SAMPLES as i64, ValueOr::Nearest)
        .map_err(CaptureError::device)?;
    let requested_buffer = i64::from(config.buffer_blocks) * CAPTURE_SAMPLES as i64;
    hardware
        .set_buffer_size_near(requested_buffer)
        .map_err(CaptureError::device)?;
    pcm.hw_params(&hardware).map_err(CaptureError::device)?;
    let current = pcm.hw_params_current().map_err(CaptureError::device)?;
    let mut format = CaptureFormat {
        rate: current.get_rate().map_err(CaptureError::device)?,
        channels: current.get_channels().map_err(CaptureError::device)?,
        period_frames: current.get_period_size().map_err(CaptureError::device)?,
        buffer_frames: current.get_buffer_size().map_err(CaptureError::device)?,
        monotonic_status_timestamps: false,
    };
    if current.get_format().map_err(CaptureError::device)? != Format::s16()
        || current.get_access().map_err(CaptureError::device)? != Access::RWInterleaved
        || !valid_format(format, requested_buffer)
    {
        return Err(CaptureError::new(ErrorKind::UnsupportedFormat(format)));
    }
    let software = pcm.sw_params_current().map_err(CaptureError::device)?;
    software.set_avail_min(1).map_err(CaptureError::device)?;
    software
        .set_start_threshold(1)
        .map_err(CaptureError::device)?;
    // Timestamp support is optional; inability is reported in negotiated format.
    let timestamps = software.set_tstamp_type(TstampType::Monotonic).is_ok()
        && software.set_tstamp_mode(true).is_ok();
    if !timestamps {
        software
            .set_tstamp_mode(false)
            .map_err(CaptureError::device)?;
    }
    pcm.sw_params(&software).map_err(CaptureError::device)?;
    let installed = pcm.sw_params_current().map_err(CaptureError::device)?;
    format.monotonic_status_timestamps =
        installed.get_tstamp_mode().map_err(CaptureError::device)?
            && installed.get_tstamp_type().map_err(CaptureError::device)? == TstampType::Monotonic;
    Ok(format)
}

fn valid_format(format: CaptureFormat, requested_buffer: i64) -> bool {
    format.rate == CAPTURE_RATE
        && format.channels == 1
        && (1..=CAPTURE_SAMPLES as i64).contains(&format.period_frames)
        && (MIN_BUFFER_FRAMES..=MAX_BUFFER_FRAMES).contains(&format.buffer_frames)
        && format.buffer_frames <= requested_buffer
}

fn classify_device_error(error: &alsa::Error) -> Discontinuity {
    match error.errno() {
        code if code == rustix::io::Errno::PIPE.raw_os_error() => Discontinuity::XRun,
        code if code == rustix::io::Errno::STRPIPE.raw_os_error() => Discontinuity::Suspended,
        code if code == rustix::io::Errno::NODEV.raw_os_error()
            || code == rustix::io::Errno::BADFD.raw_os_error() =>
        {
            Discontinuity::Disconnected
        }
        _ => Discontinuity::DeviceError,
    }
}

fn state_discontinuity(state: State) -> Option<Discontinuity> {
    match state {
        State::Running => None,
        State::XRun => Some(Discontinuity::XRun),
        State::Suspended => Some(Discontinuity::Suspended),
        State::Disconnected => Some(Discontinuity::Disconnected),
        _ => Some(Discontinuity::DeviceError),
    }
}

fn would_block(error: &alsa::Error) -> bool {
    io::Error::from_raw_os_error(error.errno()).kind() == io::ErrorKind::WouldBlock
}

fn now() -> MonoTime {
    MonoTime::from_micros(monotonic_us())
}

fn timestamp_us(seconds: i64, nanos: i64) -> Option<u64> {
    if seconds < 0 || !(0..1_000_000_000).contains(&nanos) || (seconds == 0 && nanos == 0) {
        return None;
    }
    u64::try_from(seconds)
        .ok()?
        .checked_mul(1_000_000)?
        .checked_add(nanos as u64 / 1_000)
}

struct Assembler {
    samples: CaptureBlock,
    len: usize,
    first_read_started_at_us: u64,
    successful_reads: u16,
}

impl Default for Assembler {
    fn default() -> Self {
        Self {
            samples: [0; CAPTURE_SAMPLES],
            len: 0,
            first_read_started_at_us: 0,
            successful_reads: 0,
        }
    }
}

impl Assembler {
    fn remaining_mut(&mut self) -> &mut [i16] {
        &mut self.samples[self.len..]
    }

    fn accept(
        &mut self,
        frames: usize,
        started: u64,
        completed: u64,
    ) -> std::result::Result<(), Discontinuity> {
        if frames == 0 || frames > CAPTURE_SAMPLES - self.len {
            return Err(Discontinuity::InvalidRead);
        }
        if completed < started || (self.len > 0 && started < self.first_read_started_at_us) {
            return Err(Discontinuity::ClockRegression);
        }
        if self.len == 0 {
            self.first_read_started_at_us = started;
        }
        self.len += frames;
        self.successful_reads += 1; // At most 160 one-sample reads per block.
        Ok(())
    }

    fn expired(&self, now: u64, max_age_us: u64) -> bool {
        self.len > 0 && now.saturating_sub(self.first_read_started_at_us) >= max_age_us
    }

    fn clear(&mut self) -> usize {
        let discarded = self.len;
        self.samples.fill(0);
        self.len = 0;
        self.first_read_started_at_us = 0;
        self.successful_reads = 0;
        discarded
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // The injected-fault tests exercise cleanup/lineage, not null-plugin cold
    // initialization latency. Configure that software sink before minting its
    // real short lease, then exercise the production start_prepared boundary.
    // Public prepare/start integration tests still use the full startup path.
    fn start_null_fault_fixture(
        microphone: &mut Microphone,
        controller: &mut lamp_interaction::Controller,
    ) {
        assert_eq!(microphone.device, "null");
        assert!(!microphone.is_open());
        let opened = monotonic_us();
        let pcm = PCM::new("null", Direction::Capture, true).unwrap();
        let format = configure(&pcm, microphone.config).unwrap();
        microphone
            .install(controller.snapshot(now()).unwrap())
            .unwrap();
        microphone.epoch += 1;
        microphone.sequence = 0;
        microphone.format = Some(format);
        microphone.pcm = Some(pcm);
        microphone.prepared = Some(PrepareReport {
            epoch: microphone.epoch,
            format,
            open_started_at_us: opened,
            prepared_at_us: monotonic_us(),
            microphone_generation: microphone.guard.state().unwrap().microphone_generation(),
        });
        microphone.start_prepared().unwrap();
    }

    #[test]
    fn partial_reads_preserve_sample_order_and_have_fixed_capacity() {
        for split in 1..CAPTURE_SAMPLES {
            let mut block = Assembler::default();
            block.remaining_mut()[..split].fill(11);
            block.accept(split, 100, 110).unwrap();
            block.remaining_mut().fill(22);
            block.accept(CAPTURE_SAMPLES - split, 200, 210).unwrap();
            assert_eq!(block.len, CAPTURE_SAMPLES);
            assert!(block.samples[..split].iter().all(|&value| value == 11));
            assert!(block.samples[split..].iter().all(|&value| value == 22));
            assert_eq!(block.first_read_started_at_us, 100);
            assert_eq!(block.successful_reads, 2);
            assert_eq!(block.clear(), CAPTURE_SAMPLES);
            assert_eq!(block.samples, [0; CAPTURE_SAMPLES]);
        }
    }

    #[test]
    fn partial_deadline_is_not_extended_by_more_samples() {
        let mut block = Assembler::default();
        block.accept(1, 100, 101).unwrap();
        block.accept(1, 19_000, 19_001).unwrap();
        assert!(!block.expired(20_099, 20_000));
        assert!(block.expired(20_100, 20_000));
        block.clear();
        assert!(!block.expired(1_000_000, 20_000));
        assert_eq!(block.accept(0, 0, 1), Err(Discontinuity::InvalidRead));
        assert_eq!(block.accept(161, 0, 1), Err(Discontinuity::InvalidRead));
        assert_eq!(block.accept(1, 2, 1), Err(Discontinuity::ClockRegression));
    }

    #[test]
    fn negotiation_rejects_large_or_incompatible_audio_buffers() {
        let good = CaptureFormat {
            rate: 16_000,
            channels: 1,
            period_frames: 160,
            buffer_frames: 640,
            monotonic_status_timestamps: false,
        };
        assert!(valid_format(good, 640));
        assert!(!valid_format(good, 320));
        for frames in [-1, 0, 1, 319, 641, i64::MAX] {
            assert!(!valid_format(
                CaptureFormat {
                    buffer_frames: frames,
                    ..good
                },
                640
            ));
        }
        for period in [-1, 0, 161, i64::MAX] {
            assert!(!valid_format(
                CaptureFormat {
                    period_frames: period,
                    ..good
                },
                640
            ));
        }
        assert!(!valid_format(
            CaptureFormat {
                rate: 48_000,
                ..good
            },
            640
        ));
        assert!(!valid_format(
            CaptureFormat {
                channels: 2,
                ..good
            },
            640
        ));
        for blocks in 0..=u8::MAX {
            assert_eq!(
                CaptureConfig::new(blocks).is_ok(),
                (2..=4).contains(&blocks)
            );
        }
    }

    #[test]
    fn injected_xrun_closes_null_capture_and_destroys_the_partial_block() {
        use lamp_interaction::Controller;
        let boot = BootId::new([91; 16]).unwrap();
        let mut microphone = Microphone::new("null", boot, CaptureConfig::default()).unwrap();
        let mut controller = Controller::new(boot, now());
        controller
            .set_microphone_permission(now(), Permission::Allowed)
            .unwrap();
        start_null_fault_fixture(&mut microphone, &mut controller);
        microphone.partial.samples.fill(300);
        microphone
            .partial
            .accept(80, monotonic_us(), monotonic_us())
            .unwrap();
        let error = microphone.device_failed(alsa::Error::new(
            "injected",
            rustix::io::Errno::PIPE.raw_os_error(),
        ));
        assert_eq!(error.kind, ErrorKind::Discontinuity(Discontinuity::XRun));
        assert!(!microphone.is_open());
        assert_eq!(microphone.partial.len, 0);
        assert_eq!(microphone.partial.samples, [0; CAPTURE_SAMPLES]);
        assert_eq!(microphone.reset_required(), Some(Discontinuity::XRun));
        assert!(microphone.start().is_err());
        microphone.reset().unwrap();
        start_null_fault_fixture(&mut microphone, &mut controller);
        assert_eq!(microphone.try_read().unwrap().unwrap().sequence, 1);
        microphone.revoke().unwrap();
    }

    #[test]
    fn privacy_lineage_change_discards_an_incomplete_block() {
        use lamp_interaction::Controller;
        let boot = BootId::new([92; 16]).unwrap();
        let mut microphone = Microphone::new("null", boot, CaptureConfig::default()).unwrap();
        let mut controller = Controller::new(boot, now());
        controller
            .set_microphone_permission(now(), Permission::Allowed)
            .unwrap();
        start_null_fault_fixture(&mut microphone, &mut controller);
        microphone.partial.samples.fill(400);
        microphone
            .partial
            .accept(40, monotonic_us(), monotonic_us())
            .unwrap();
        controller
            .set_microphone_permission(now(), Permission::Denied)
            .unwrap();
        controller
            .set_microphone_permission(now(), Permission::Allowed)
            .unwrap();
        let update = microphone
            .install(controller.snapshot(now()).unwrap())
            .unwrap();
        assert_eq!(update.partial_samples_discarded, 40);
        assert!(update.closed);
        assert_eq!(microphone.partial.samples, [0; CAPTURE_SAMPLES]);
    }

    #[test]
    fn status_and_timestamp_failures_are_not_fabricated_audio() {
        assert_eq!(state_discontinuity(State::XRun), Some(Discontinuity::XRun));
        assert_eq!(
            state_discontinuity(State::Disconnected),
            Some(Discontinuity::Disconnected)
        );
        assert_eq!(
            state_discontinuity(State::Suspended),
            Some(Discontinuity::Suspended)
        );
        for (errno, expected) in [
            (rustix::io::Errno::PIPE, Discontinuity::XRun),
            (rustix::io::Errno::STRPIPE, Discontinuity::Suspended),
            (rustix::io::Errno::NODEV, Discontinuity::Disconnected),
            (rustix::io::Errno::IO, Discontinuity::DeviceError),
        ] {
            assert_eq!(
                classify_device_error(&alsa::Error::new("injected", errno.raw_os_error())),
                expected
            );
        }
        assert_eq!(timestamp_us(5, 120_000), Some(5_000_120));
        for (seconds, nanos) in [(0, 0), (-1, 2), (1, -1), (1, 1_000_000_000), (i64::MAX, 1)] {
            assert_eq!(timestamp_us(seconds, nanos), None);
        }
    }
}
