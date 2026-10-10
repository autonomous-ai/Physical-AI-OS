//! Private single-planar MMAP ownership algorithm. The same code is exercised
//! with synthetic drivers on hosts that have no V4L2 implementation.
use crate::{
    CaptureId, DriverTimestamp, MAX_FRAME_BYTES, NegotiatedMode, OperationBudget, PortError,
    PortFrame, TimestampDomain, TimestampPoint,
};

pub(crate) const SLOTS: usize = 4;
const TIMESTAMP_MASK: u32 = 0x0000_e000;
const TIMESTAMP_MONOTONIC: u32 = 0x0000_2000;
const TIMESTAMP_COPY: u32 = 0x0000_4000;
const SOURCE_MASK: u32 = 0x0007_0000;
const SOURCE_SOE: u32 = 0x0001_0000;
const BUFFER_ERROR: u32 = 0x0000_0040;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Stage {
    #[cfg(target_os = "linux")]
    Identity,
    #[cfg(target_os = "linux")]
    Lock,
    #[cfg(target_os = "linux")]
    Open,
    #[cfg(target_os = "linux")]
    Capabilities,
    #[cfg(target_os = "linux")]
    SetFormat,
    #[cfg(target_os = "linux")]
    GetFormat,
    #[cfg(target_os = "linux")]
    GetInterval,
    #[cfg(target_os = "linux")]
    SetInterval,
    #[cfg(target_os = "linux")]
    Controls,
    RequestBuffers,
    QueryBuffer,
    Map,
    Queue,
    StreamOn,
    Dequeue,
    Copy,
    StreamOff,
    ReleaseBuffers,
    Close,
    Clock,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Fault {
    pub stage: Stage,
    pub error: PortError,
}
impl Fault {
    pub(crate) fn new(stage: Stage, error: PortError) -> Self {
        Self { stage, error }
    }
    pub(crate) fn invalid(stage: Stage) -> Self {
        Self::new(stage, PortError::InvalidData)
    }
}

pub(crate) trait Check {
    fn check(&mut self, stage: Stage) -> Result<(), Fault>;
}
pub(crate) fn checked<T>(
    check: &mut impl Check,
    stage: Stage,
    operation: impl FnOnce() -> Result<T, PortError>,
) -> Result<T, Fault> {
    check.check(stage)?;
    let result = operation().map_err(|error| Fault::new(stage, error));
    let after = check.check(stage);
    // The first I/O failure is retained even if its return also exceeded time.
    let value = result?;
    after?;
    Ok(value)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct Layout {
    pub index: u32,
    pub length: usize,
    pub offset: u32,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct Dequeued {
    pub index: u32,
    pub length: usize,
    pub bytes_used: usize,
    pub sequence: u32,
    pub flags: u32,
    pub seconds: i64,
    pub micros: i64,
}

/// Driver methods have no implicit retries. Mappings remain completely private
/// to the implementation. Only copy_dequeued may read mapped bytes.
pub(crate) trait Driver {
    fn request_buffers(&mut self, count: u32) -> Result<u32, PortError>;
    fn query_buffer(&mut self, index: u32) -> Result<Layout, PortError>;
    fn map(&mut self, layout: Layout) -> Result<(), PortError>;
    fn queue(&mut self, index: u32) -> Result<(), PortError>;
    fn stream_on(&mut self) -> Result<(), PortError>;
    fn dequeue(&mut self) -> Result<Option<Dequeued>, PortError>;
    fn copy_dequeued(&mut self, index: u32, destination: &mut [u8]) -> Result<(), PortError>;
    fn stream_off(&mut self) -> Result<(), PortError>;
    fn unmap_all(&mut self);
    fn release_buffers(&mut self) -> Result<(), PortError>;
    fn close(&mut self) -> Result<(), PortError>;
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Ownership {
    Empty,
    Mapped,
    Queued,
    Dequeued,
}
#[derive(Clone, Copy)]
struct Slot {
    layout: Option<Layout>,
    ownership: Ownership,
}
const EMPTY: Slot = Slot {
    layout: None,
    ownership: Ownership::Empty,
};

/// Errors are separately retained; cleanup always closes once and never retries.
/// A successful report does not prove munmap success for bindings that hide it.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct CleanupReport {
    pub stream_off: Option<Fault>,
    pub release_buffers: Option<Fault>,
    pub close: Option<Fault>,
}
impl CleanupReport {
    pub fn first_failure(self) -> Option<Fault> {
        self.stream_off.or(self.release_buffers).or(self.close)
    }
}

pub(crate) struct Queue<D: Driver> {
    driver: D,
    slots: [Slot; SLOTS],
    mode: NegotiatedMode,
    capture: CaptureId,
    requested_buffers: bool,
    touched_queue: bool,
    active: bool,
    faulted: bool,
    closed: bool,
    timestamp_flags: Option<u32>,
    pub(crate) last_flags: Option<u32>,
}
impl<D: Driver> Queue<D> {
    pub(crate) fn new(driver: D, mode: NegotiatedMode, capture: CaptureId) -> Self {
        Self {
            driver,
            slots: [EMPTY; SLOTS],
            mode,
            capture,
            requested_buffers: false,
            touched_queue: false,
            active: false,
            faulted: false,
            closed: false,
            timestamp_flags: None,
            last_flags: None,
        }
    }
    pub(crate) fn buffer_lengths(&self) -> [Option<usize>; SLOTS] {
        self.slots
            .map(|slot| slot.layout.map(|layout| layout.length))
    }
    pub(crate) fn start(&mut self, check: &mut impl Check) -> Result<NegotiatedMode, Fault> {
        if self.active || self.closed || self.faulted || self.requested_buffers {
            return Err(Fault::invalid(Stage::RequestBuffers));
        }
        let result = self.initialize(check);
        if result.is_err() {
            self.faulted = true;
        }
        result
    }
    fn initialize(&mut self, check: &mut impl Check) -> Result<NegotiatedMode, Fault> {
        // Even a failed ioctl may have changed kernel state. Cleanup must not
        // assume that resetting this Rust object freed those resources.
        let count = checked(check, Stage::RequestBuffers, || {
            self.requested_buffers = true;
            self.driver.request_buffers(u32::from(self.mode.buffers))
        })?;
        if !(2..=u32::from(self.mode.buffers)).contains(&count) || count as usize > SLOTS {
            return Err(Fault::invalid(Stage::RequestBuffers));
        }
        self.mode.buffers = count as u8;
        // Validate ALL layouts before the first mmap, not just each in turn.
        for index in 0..count {
            let layout = checked(check, Stage::QueryBuffer, || {
                self.driver.query_buffer(index)
            })?;
            if layout.index != index
                || layout.length < self.mode.size_image
                || layout.length > MAX_FRAME_BYTES
                || layout.length == 0
                || layout.offset.checked_add(layout.length as u32).is_none()
                || self
                    .slots
                    .iter()
                    .filter_map(|s| s.layout)
                    .any(|old| old.offset == layout.offset)
            {
                return Err(Fault::invalid(Stage::QueryBuffer));
            }
            self.slots[index as usize].layout = Some(layout);
        }
        for index in 0..count as usize {
            let layout = self.slots[index].layout.expect("validated layout");
            checked(check, Stage::Map, || self.driver.map(layout))?;
            self.slots[index].ownership = Ownership::Mapped;
        }
        for index in 0..count as usize {
            self.enqueue(index, check)?;
        }
        self.touched_queue = true;
        checked(check, Stage::StreamOn, || self.driver.stream_on())?;
        self.active = true;
        Ok(self.mode)
    }
    fn enqueue(&mut self, index: usize, check: &mut impl Check) -> Result<(), Fault> {
        if !matches!(
            self.slots[index].ownership,
            Ownership::Mapped | Ownership::Dequeued
        ) {
            return Err(Fault::invalid(Stage::Queue));
        }
        checked(check, Stage::Queue, || {
            self.touched_queue = true;
            self.driver.queue(index as u32)
        })?;
        self.slots[index].ownership = Ownership::Queued;
        Ok(())
    }
    pub(crate) fn read(
        &mut self,
        destination: &mut [u8],
        check: &mut impl Check,
    ) -> Result<Option<PortFrame>, Fault> {
        if !self.active || self.closed || self.faulted {
            return Err(Fault::invalid(Stage::Dequeue));
        }
        if destination.len() > MAX_FRAME_BYTES {
            self.faulted = true;
            return Err(Fault::invalid(Stage::Copy));
        }
        let result = self.read_inner(destination, check);
        if result.is_err() {
            self.active = false;
            self.faulted = true;
            destination.fill(0);
        }
        result
    }
    fn read_inner(
        &mut self,
        destination: &mut [u8],
        check: &mut impl Check,
    ) -> Result<Option<PortFrame>, Fault> {
        let Some(frame) = checked(check, Stage::Dequeue, || self.driver.dequeue())? else {
            return Ok(None);
        };
        let index = usize::try_from(frame.index).map_err(|_| Fault::invalid(Stage::Dequeue))?;
        let slot = self
            .slots
            .get_mut(index)
            .ok_or_else(|| Fault::invalid(Stage::Dequeue))?;
        let layout = slot.layout.ok_or_else(|| Fault::invalid(Stage::Dequeue))?;
        if slot.ownership != Ownership::Queued
            || frame.length != layout.length
            || frame.bytes_used < 4
            || frame.bytes_used > self.mode.size_image
            || frame.bytes_used > layout.length
            || frame.bytes_used > destination.len()
            || frame.flags & BUFFER_ERROR != 0
        {
            return Err(Fault::invalid(Stage::Dequeue));
        }
        // Ownership changes only on an unambiguous successful dequeue. No
        // mapping reference exists before this point or after copy returns.
        slot.ownership = Ownership::Dequeued;
        let flags = frame.flags & (TIMESTAMP_MASK | SOURCE_MASK);
        if self
            .timestamp_flags
            .is_some_and(|previous| previous != flags)
        {
            return Err(Fault::invalid(Stage::Dequeue));
        }
        let timestamp = timestamp(frame)?;
        self.timestamp_flags = Some(flags);
        self.last_flags = Some(frame.flags);
        checked(check, Stage::Copy, || {
            self.driver
                .copy_dequeued(frame.index, &mut destination[..frame.bytes_used])
        })?;
        // If copy or its deadline fails, do not requeue an ambiguous buffer.
        self.enqueue(index, check)?;
        Ok(Some(PortFrame {
            capture: self.capture,
            source: self.mode.source,
            bytes_used: frame.bytes_used,
            driver_sequence: frame.sequence,
            timestamp: Some(timestamp),
        }))
    }
    pub(crate) fn stop(&mut self, check: &mut impl Check) -> CleanupReport {
        if self.closed {
            return CleanupReport::default();
        }
        self.closed = true;
        self.active = false;
        let mut report = CleanupReport::default();
        if self.touched_queue {
            report.stream_off = checked(check, Stage::StreamOff, || self.driver.stream_off()).err();
        }
        // MMAP only: after an error no mapped bytes are touched. Removing these
        // process mappings is permitted even when STREAMOFF failed. Closing the
        // FD is still required to end kernel ownership; do not reuse this queue.
        self.driver.unmap_all();
        self.slots = [EMPTY; SLOTS];
        if self.requested_buffers && report.stream_off.is_none() {
            report.release_buffers = checked(check, Stage::ReleaseBuffers, || {
                self.driver.release_buffers()
            })
            .err();
        }
        // Closing is mandatory even after cancellation/deadline/STREAMOFF
        // failure. No retry of close: Linux may already have recycled the FD.
        report.close = self
            .driver
            .close()
            .err()
            .map(|e| Fault::new(Stage::Close, e));
        if report.close.is_none() {
            report.close = check.check(Stage::Close).err();
        }
        report
    }
}

fn timestamp(frame: Dequeued) -> Result<DriverTimestamp, Fault> {
    let domain = match frame.flags & TIMESTAMP_MASK {
        0 | TIMESTAMP_COPY => TimestampDomain::Unknown,
        TIMESTAMP_MONOTONIC => TimestampDomain::HostMonotonic,
        _ => return Err(Fault::invalid(Stage::Dequeue)),
    };
    // EOF is the zero value, not a bit whose `contains` check can be used.
    let point = match frame.flags & SOURCE_MASK {
        0 => TimestampPoint::EndOfFrame,
        SOURCE_SOE => TimestampPoint::StartOfExposure,
        _ => return Err(Fault::invalid(Stage::Dequeue)),
    };
    if frame.seconds < 0 || !(0..1_000_000).contains(&frame.micros) {
        return Err(Fault::invalid(Stage::Dequeue));
    }
    let micros = (frame.seconds as u64)
        .checked_mul(1_000_000)
        .and_then(|seconds| seconds.checked_add(frame.micros as u64))
        .ok_or_else(|| Fault::invalid(Stage::Dequeue))?;
    Ok(DriverTimestamp {
        micros,
        domain,
        point,
    })
}

/// Shared by the Linux adapter and deterministic tests. A cancellation source
/// and monotonic clock are sampled between bounded operations, never waited on.
pub(crate) struct Budget<'a> {
    pub(crate) limits: OperationBudget,
    pub(crate) last: u64,
    pub(crate) now: &'a mut dyn FnMut() -> u64,
    pub(crate) cancelled: &'a dyn Fn() -> bool,
}
impl Check for Budget<'_> {
    fn check(&mut self, stage: Stage) -> Result<(), Fault> {
        if (self.cancelled)() {
            return Err(Fault::new(stage, PortError::Cancelled));
        }
        let now = (self.now)();
        if now < self.last || now < self.limits.started_at.as_micros() {
            return Err(Fault::new(Stage::Clock, PortError::InvalidData));
        }
        self.last = now;
        if now >= self.limits.deadline.as_micros() {
            return Err(Fault::new(stage, PortError::DeadlineExceeded));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests;
