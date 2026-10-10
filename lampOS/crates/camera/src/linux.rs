//! Linux single-planar V4L2 MJPG capture for a dedicated supervised process.
//!
//! Constructing a port does not inspect or open hardware. Use it through
//! `Capture`: PortIo is a trusted boundary, not a replacement for physical
//! privacy or the interaction authority. No GPIO owner or permission is created
//! here. Cancellation is sticky; there is no source fallback or automatic retry.
//! O_NONBLOCK makes DQBUF nonblocking, not every USB/ioctl operation. Budgets
//! detect returns after a deadline; a supervisor must handle a stalled process.

mod identity;
mod params;
mod queue;

use crate::backend::{Budget, Check, Queue};
pub use crate::backend::{CleanupReport, Fault as BackendFault, Stage};
use crate::{
    CallTiming, CaptureConfig, CaptureId, Clock, ClockError, NegotiatedMode, OperationBudget,
    PortError, PortFrame, PortIo, STOP_BUDGET_US, SourceId,
};
pub use identity::IdentityReport;
use lamp_interaction::MonoTime;
pub use params::{
    CapabilityReport, ControlObservation, ControlReading, ControlState, IntervalStatus,
};
use std::path::PathBuf;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use v4l2r::ioctl::IntoErrno;

/// Expected or observed USB identity. The physical port topology and interface
/// are mandatory; serial may be absent. Values are checked labels, not secrets
/// or authenticated evidence. No VID/PID pair is hard-coded as a fallback.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct UsbIdentity {
    pub vendor: u16,
    pub product: u16,
    pub topology: String,
    pub serial: Option<String>,
    pub interface_number: u8,
    pub capture_index: u8,
}
impl UsbIdentity {
    fn validate(&self) -> Result<(), PortError> {
        if self.vendor == 0
            || self.product == 0
            || self.topology.is_empty()
            || self.topology.len() > 128
            || !self
                .topology
                .bytes()
                .all(|c| c.is_ascii_digit() || b"-.:".contains(&c))
            || self.serial.as_ref().is_some_and(|s| {
                s.is_empty()
                    || s.len() > 256
                    || !s.bytes().all(|c| c.is_ascii_graphic() || c == b' ')
            })
        {
            return Err(PortError::InvalidData);
        }
        Ok(())
    }
    fn matches(&self, actual: &Self) -> bool {
        self.vendor == actual.vendor
            && self.product == actual.product
            && self.topology == actual.topology
            && self.interface_number == actual.interface_number
            && self.capture_index == actual.capture_index
            && self
                .serial
                .as_ref()
                .is_none_or(|serial| actual.serial.as_ref() == Some(serial))
    }
}
#[derive(Clone, Debug)]
pub struct LinuxConfig {
    device: PathBuf,
    lock_directory: PathBuf,
    source: SourceId,
    expected: UsbIdentity,
}
impl LinuxConfig {
    pub fn new(
        device: PathBuf,
        lock_directory: PathBuf,
        source: SourceId,
        expected: UsbIdentity,
    ) -> Result<Self, PortError> {
        if !identity::valid_path(&device) || !identity::valid_path(&lock_directory) {
            return Err(PortError::InvalidData);
        }
        expected.validate()?;
        Ok(Self {
            device,
            lock_directory,
            source,
            expected,
        })
    }
}

/// Optional emergency cancellation can be signalled independently of provider
/// work. It never authorizes or reopens capture. Normal privacy changes still
/// flow through Capture's original-grant ownership checks.
#[derive(Clone, Default)]
pub struct Cancellation(Arc<AtomicBool>);
impl Cancellation {
    pub fn cancel(&self) {
        self.0.store(true, Ordering::Release);
    }
    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }
}

#[derive(Clone, Debug, Default)]
pub struct Diagnostics {
    pub identity: Option<IdentityReport>,
    pub capabilities: Option<CapabilityReport>,
    pub mode: Option<NegotiatedMode>,
    pub interval: Option<IntervalStatus>,
    pub controls: Option<[ControlObservation; 5]>,
    pub buffer_lengths: [Option<usize>; 4],
    pub start: Option<CallTiming>,
    pub last_read: Option<CallTiming>,
    pub stop: Option<CallTiming>,
    pub last_raw_buffer_flags: Option<u32>,
    pub fault: Option<BackendFault>,
    pub cleanup: Option<CleanupReport>,
}

/// Shared CLOCK_MONOTONIC clock, also used by lamp-ipc and the controller.
#[derive(Default)]
pub struct SystemClock;
impl Clock for SystemClock {
    fn now(&mut self) -> Result<MonoTime, ClockError> {
        Ok(now())
    }
}
fn now() -> MonoTime {
    MonoTime::from_micros(lamp_ipc::monotonic_us())
}
fn limits(start: MonoTime) -> OperationBudget {
    OperationBudget {
        started_at: start,
        deadline: start.checked_add(STOP_BUDGET_US).unwrap_or(start),
    }
}
fn capped(mut budget: OperationBudget, duration_us: u64) -> OperationBudget {
    let deadline = budget
        .started_at
        .checked_add(duration_us)
        .unwrap_or(budget.started_at);
    budget.deadline = budget.deadline.min(deadline);
    budget
}
fn run_budget<T>(
    limits: OperationBudget,
    cancellation: &Cancellation,
    operation: impl FnOnce(&mut Budget<'_>) -> T,
) -> T {
    let mut clock = lamp_ipc::monotonic_us;
    let cancelled = || cancellation.is_cancelled();
    let mut check = Budget {
        limits,
        last: limits.started_at.as_micros(),
        now: &mut clock,
        cancelled: &cancelled,
    };
    operation(&mut check)
}
pub(super) fn errno(errno: i32) -> PortError {
    if errno == 19 || errno == 6 {
        PortError::Disconnected
    } else {
        PortError::Io(errno)
    }
}
pub(super) fn map_ioctl(error: impl IntoErrno) -> PortError {
    errno(error.into_errno())
}

fn clear_destination(destination: &mut [u8]) {
    let len = destination.len().min(crate::MAX_FRAME_BYTES);
    destination[..len].fill(0);
}

pub struct LinuxPort {
    config: LinuxConfig,
    cancellation: Cancellation,
    queue: Option<Queue<queue::LinuxDriver>>,
    last_capture: Option<CaptureId>,
    diagnostics: Diagnostics,
    faulted: bool,
}
impl LinuxPort {
    /// No filesystem, device, ioctl, allocation of frame storage or permission
    /// check occurs here. Source identity is verified during permitted start.
    pub fn new(config: LinuxConfig) -> Self {
        Self {
            config,
            cancellation: Cancellation::default(),
            queue: None,
            last_capture: None,
            diagnostics: Diagnostics::default(),
            faulted: false,
        }
    }
    pub fn cancellation(&self) -> Cancellation {
        self.cancellation.clone()
    }
    pub fn diagnostics(&self) -> &Diagnostics {
        &self.diagnostics
    }
    fn close_queue(&mut self, budget: OperationBudget) -> CleanupReport {
        let Some(mut queue) = self.queue.take() else {
            return self.diagnostics.cleanup.unwrap_or_default();
        };
        let start = now();
        // Teardown must still run after a latched emergency cancellation.
        let cleanup = run_budget(
            capped(budget, STOP_BUDGET_US),
            &Cancellation::default(),
            |check| queue.stop(check),
        );
        self.diagnostics.stop = Some(CallTiming {
            started_at: start,
            completed_at: now(),
        });
        self.diagnostics.cleanup = Some(cleanup);
        cleanup
    }
    fn fail(&mut self, fault: BackendFault) -> PortError {
        self.faulted = true;
        self.diagnostics.fault.get_or_insert(fault);
        self.close_queue(limits(now()));
        fault.error
    }
    fn start_inner(
        &mut self,
        config: CaptureConfig,
        capture: CaptureId,
        check: &mut impl Check,
    ) -> Result<NegotiatedMode, BackendFault> {
        check.check(Stage::Open)?;
        let mut opened = identity::open(&self.config, check)?;
        self.diagnostics.identity = Some(opened.report.clone());
        let preparation = (|| {
            let fd = opened
                .fd
                .as_mut()
                .ok_or_else(|| BackendFault::invalid(Stage::Open))?;
            self.diagnostics.capabilities = Some(params::capabilities(fd, check)?);
            let (mode, interval) = params::negotiate(fd, config, check)?;
            self.diagnostics.interval = Some(interval);
            self.diagnostics.controls = Some(params::controls(fd, check)?);
            Ok(mode)
        })();
        let mode = match preparation {
            Ok(mode) => mode,
            Err(error) => {
                let started_at = now();
                let close = opened
                    .close()
                    .err()
                    .map(|e| BackendFault::new(Stage::Close, e));
                self.diagnostics.cleanup = Some(CleanupReport {
                    close,
                    ..Default::default()
                });
                self.diagnostics.stop = Some(CallTiming {
                    started_at,
                    completed_at: now(),
                });
                return Err(error);
            }
        };
        self.queue = Some(Queue::new(queue::LinuxDriver::new(opened), mode, capture));
        let queue = self.queue.as_mut().expect("queue just installed");
        let mode = queue.start(check)?;
        self.diagnostics.buffer_lengths = queue.buffer_lengths();
        self.diagnostics.mode = Some(mode);
        Ok(mode)
    }
}
impl PortIo for LinuxPort {
    fn start(
        &mut self,
        config: CaptureConfig,
        capture: CaptureId,
        budget: OperationBudget,
    ) -> Result<NegotiatedMode, PortError> {
        if self.faulted
            || self.queue.is_some()
            || config.source != self.config.source
            || config.validate().is_err()
            || self
                .last_capture
                .is_some_and(|last| last.worker != capture.worker || capture.epoch <= last.epoch)
        {
            return Err(PortError::InvalidData);
        }
        self.last_capture = Some(capture);
        self.diagnostics = Diagnostics::default();
        let started = now();
        let cancellation = self.cancellation.clone();
        let result = run_budget(
            capped(budget, crate::START_BUDGET_US),
            &cancellation,
            |check| self.start_inner(config, capture, check),
        );
        self.diagnostics.start = Some(CallTiming {
            started_at: started,
            completed_at: now(),
        });
        result.map_err(|error| self.fail(error))
    }
    fn try_frame(
        &mut self,
        destination: &mut [u8],
        budget: OperationBudget,
    ) -> Result<Option<PortFrame>, PortError> {
        if self.faulted {
            clear_destination(destination);
            return Err(PortError::InvalidData);
        }
        let started = now();
        let result = if let Some(queue) = self.queue.as_mut() {
            let result = run_budget(
                capped(budget, crate::READ_BUDGET_US),
                &self.cancellation,
                |check| queue.read(destination, check),
            );
            self.diagnostics.last_raw_buffer_flags = queue.last_flags;
            result
        } else {
            Err(BackendFault::invalid(Stage::Dequeue))
        };
        self.diagnostics.last_read = Some(CallTiming {
            started_at: started,
            completed_at: now(),
        });
        result.map_err(|error| {
            clear_destination(destination);
            self.fail(error)
        })
    }
    fn stop(&mut self, budget: OperationBudget) -> Result<(), PortError> {
        let cleanup = self.close_queue(budget);
        if let Some(fault) = cleanup.first_failure() {
            self.faulted = true;
            self.diagnostics.fault.get_or_insert(fault);
            Err(fault.error)
        } else {
            Ok(())
        }
    }
}
impl Drop for LinuxPort {
    fn drop(&mut self) {
        if self.queue.is_some() {
            self.close_queue(limits(now()));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn identity() -> UsbIdentity {
        UsbIdentity {
            vendor: 0x1bcf,
            product: 0x28cc,
            topology: "1-1.2".into(),
            serial: None,
            interface_number: 0,
            capture_index: 0,
        }
    }
    #[test]
    fn cancellation_or_expired_budget_cannot_reach_identity_or_open() {
        for cancel in [false, true] {
            let source = SourceId::new("test-camera").unwrap();
            let config = LinuxConfig::new(
                "/dev/this-camera-does-not-exist".into(),
                "/home/nonexistent/private-camera-locks".into(),
                source,
                identity(),
            )
            .unwrap();
            let mut port = LinuxPort::new(config);
            if cancel {
                port.cancellation().cancel();
            }
            let capture = CaptureId {
                worker: lamp_interaction::BootId::new([1; 16]).unwrap(),
                epoch: std::num::NonZeroU64::new(1).unwrap(),
            };
            let config = CaptureConfig {
                source,
                width: 1280,
                height: 720,
                interval: None,
                max_frame_bytes: crate::MAX_FRAME_BYTES,
                buffers: 2,
            };
            let result = port.start(
                config,
                capture,
                OperationBudget {
                    started_at: MonoTime::from_micros(0),
                    deadline: MonoTime::from_micros(0),
                },
            );
            assert_eq!(
                result.unwrap_err(),
                if cancel {
                    PortError::Cancelled
                } else {
                    PortError::DeadlineExceeded
                }
            );
            assert_eq!(port.diagnostics().fault.unwrap().stage, Stage::Open);
            assert!(port.diagnostics().identity.is_none());
        }
    }
    #[test]
    fn caller_cannot_extend_an_operation_budget_and_overflow_expires() {
        let budget = OperationBudget {
            started_at: MonoTime::from_micros(10),
            deadline: MonoTime::from_micros(100_000),
        };
        assert_eq!(capped(budget, 20).deadline.as_micros(), 30);
        assert_eq!(capped(budget, 200_000).deadline.as_micros(), 100_000);
        let budget = OperationBudget {
            started_at: MonoTime::from_micros(u64::MAX),
            deadline: MonoTime::from_micros(u64::MAX),
        };
        assert_eq!(capped(budget, 20).deadline.as_micros(), u64::MAX);
    }
    #[test]
    fn identity_requires_exact_topology_interface_and_optional_expected_serial() {
        let expected = identity();
        assert!(expected.matches(&identity()));
        let mut actual = identity();
        actual.interface_number = 1;
        assert!(!expected.matches(&actual));
        actual = identity();
        actual.topology = "1-1.3".into();
        assert!(!expected.matches(&actual));
        let mut expected = identity();
        expected.serial = Some("unit123".into());
        assert!(!expected.matches(&identity()));
        actual = identity();
        actual.serial = expected.serial.clone();
        assert!(expected.matches(&actual));
    }
    #[test]
    fn construction_and_cancel_do_not_open_or_inspect_the_nonexistent_node() {
        let config = LinuxConfig::new(
            "/dev/this-camera-does-not-exist".into(),
            "/home/nonexistent/private-camera-locks".into(),
            SourceId::new("test-camera").unwrap(),
            identity(),
        )
        .unwrap();
        let port = LinuxPort::new(config);
        assert!(port.diagnostics().identity.is_none());
        let cancellation = port.cancellation();
        cancellation.cancel();
        assert!(port.cancellation().is_cancelled());
        assert!(port.diagnostics().identity.is_none());
    }
}
