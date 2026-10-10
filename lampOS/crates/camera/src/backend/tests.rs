use super::*;
use crate::{FrameInterval, PixelFormat, SourceId};
use lamp_interaction::{BootId, MonoTime};
use std::{
    cell::{Cell, RefCell},
    collections::VecDeque,
    num::NonZeroU64,
    rc::Rc,
};

#[derive(Default)]
struct State {
    calls: Vec<&'static str>,
    fail_at: Option<usize>,
    cancel_at: Option<usize>,
    frames: VecDeque<Option<Dequeued>>,
    count: u32,
    bad_layout: Option<Layout>,
    advance: u64,
}
struct Mock {
    cancelled: Rc<Cell<bool>>,
    state: Rc<RefCell<State>>,
    time: Rc<Cell<u64>>,
}
impl Mock {
    fn step(&mut self, name: &'static str) -> Result<(), PortError> {
        let mut state = self.state.borrow_mut();
        let index = state.calls.len();
        state.calls.push(name);
        if state.cancel_at == Some(index) {
            self.cancelled.set(true);
        }
        self.time.set(self.time.get() + state.advance);
        if state.fail_at == Some(index) {
            Err(PortError::Io(5))
        } else {
            Ok(())
        }
    }
}
impl Driver for Mock {
    fn request_buffers(&mut self, _: u32) -> Result<u32, PortError> {
        self.step("request")?;
        Ok(self.state.borrow().count)
    }
    fn query_buffer(&mut self, index: u32) -> Result<Layout, PortError> {
        self.step("query")?;
        Ok(self
            .state
            .borrow()
            .bad_layout
            .filter(|l| l.index == index)
            .unwrap_or(Layout {
                index,
                length: 4096,
                offset: index * 4096,
            }))
    }
    fn map(&mut self, _: Layout) -> Result<(), PortError> {
        self.step("map")
    }
    fn queue(&mut self, _: u32) -> Result<(), PortError> {
        self.step("queue")
    }
    fn stream_on(&mut self) -> Result<(), PortError> {
        self.step("on")
    }
    fn dequeue(&mut self) -> Result<Option<Dequeued>, PortError> {
        self.step("dequeue")?;
        Ok(self.state.borrow_mut().frames.pop_front().flatten())
    }
    fn copy_dequeued(&mut self, _: u32, destination: &mut [u8]) -> Result<(), PortError> {
        self.step("copy")?;
        destination.fill(42);
        Ok(())
    }
    fn stream_off(&mut self) -> Result<(), PortError> {
        self.step("off")
    }
    fn unmap_all(&mut self) {
        let _ = self.step("unmap");
    }
    fn release_buffers(&mut self) -> Result<(), PortError> {
        self.step("release")
    }
    fn close(&mut self) -> Result<(), PortError> {
        self.step("close")
    }
}
struct Harness {
    state: Rc<RefCell<State>>,
    time: Rc<Cell<u64>>,
    cancelled: Rc<Cell<bool>>,
}
impl Harness {
    fn new() -> Self {
        Self {
            state: Rc::new(RefCell::new(State {
                count: 2,
                ..Default::default()
            })),
            time: Rc::new(Cell::new(100)),
            cancelled: Rc::new(Cell::new(false)),
        }
    }
    fn queue(&self) -> Queue<Mock> {
        Queue::new(
            Mock {
                cancelled: self.cancelled.clone(),
                state: self.state.clone(),
                time: self.time.clone(),
            },
            NegotiatedMode {
                source: SourceId::new("unit-camera").unwrap(),
                format: PixelFormat::MJPG,
                width: 1280,
                height: 720,
                interval: Some(FrameInterval::new(1, 30).unwrap()),
                size_image: 4096,
                buffers: 2,
            },
            CaptureId {
                worker: BootId::new([1; 16]).unwrap(),
                epoch: NonZeroU64::new(1).unwrap(),
            },
        )
    }
    fn budget<T>(&self, end: u64, operation: impl FnOnce(&mut Budget<'_>) -> T) -> T {
        let mut clock = || self.time.get();
        let cancelled = || self.cancelled.get();
        operation(&mut Budget {
            limits: OperationBudget {
                started_at: MonoTime::from_micros(100),
                deadline: MonoTime::from_micros(end),
            },
            last: 100,
            now: &mut clock,
            cancelled: &cancelled,
        })
    }
    fn calls(&self, name: &str) -> usize {
        self.state
            .borrow()
            .calls
            .iter()
            .filter(|c| **c == name)
            .count()
    }
}
fn frame() -> Dequeued {
    Dequeued {
        index: 0,
        length: 4096,
        bytes_used: 8,
        sequence: 1,
        flags: TIMESTAMP_MONOTONIC,
        seconds: 1,
        micros: 2,
    }
}

#[test]
fn validate_every_layout_before_any_mapping_and_bound_driver_count() {
    for count in [0, 1, 3, 5, u32::MAX] {
        let h = Harness::new();
        h.state.borrow_mut().count = count;
        let mut q = h.queue();
        assert!(h.budget(10_000, |c| q.start(c)).is_err());
        assert_eq!(h.calls("map"), 0);
        h.budget(10_000, |c| q.stop(c));
        assert_eq!(h.calls("close"), 1);
    }
    for length in [0, 4095, MAX_FRAME_BYTES + 1] {
        let h = Harness::new();
        h.state.borrow_mut().bad_layout = Some(Layout {
            index: 1,
            length,
            offset: 4096,
        });
        let mut q = h.queue();
        assert!(h.budget(10_000, |c| q.start(c)).is_err());
        assert_eq!(h.calls("map"), 0);
    }
    let h = Harness::new();
    h.state.borrow_mut().bad_layout = Some(Layout {
        index: 1,
        length: 4096,
        offset: 0,
    });
    let mut q = h.queue();
    assert!(h.budget(10_000, |c| q.start(c)).is_err());
    assert_eq!(h.calls("map"), 0);
}
#[test]
fn partial_start_faults_close_once_without_retry_and_streamoff_after_queue_attempt() {
    // request, query0, query1, map0, map1, queue0, queue1, streamon
    for stage in 0..8 {
        let h = Harness::new();
        h.state.borrow_mut().fail_at = Some(stage);
        let mut q = h.queue();
        assert!(h.budget(10_000, |c| q.start(c)).is_err(), "stage {stage}");
        h.budget(10_000, |c| q.stop(c));
        assert_eq!(h.calls("close"), 1);
        assert_eq!(h.calls("off"), usize::from(stage >= 5));
        let calls = h.state.borrow().calls.len();
        h.budget(10_000, |c| q.stop(c));
        assert_eq!(h.state.borrow().calls.len(), calls);
        assert!(h.budget(10_000, |c| q.start(c)).is_err());
    }
}
#[test]
fn not_ready_never_requeues_or_changes_ownership() {
    let h = Harness::new();
    let mut q = h.queue();
    let mode = h.budget(10_000, |c| q.start(c)).unwrap();
    assert_eq!(mode.buffers, 2);
    assert_eq!(q.buffer_lengths(), [Some(4096), Some(4096), None, None]);
    for _ in 0..50 {
        assert!(
            h.budget(10_000, |c| q.read(&mut [0; 32], c))
                .unwrap()
                .is_none()
        );
    }
    assert_eq!(h.calls("queue"), 2);
    assert_eq!(h.calls("copy"), 0);
    h.state.borrow_mut().frames.push_back(Some(frame()));
    let mut data = [0; 32];
    let result = h.budget(10_000, |c| q.read(&mut data, c)).unwrap().unwrap();
    assert_eq!(result.bytes_used, 8);
    assert_eq!(&data[..8], &[42; 8]);
    assert_eq!(h.calls("queue"), 3);
    assert_eq!(h.calls("copy"), 1);
}
#[test]
fn every_read_failure_is_terminal_and_erases_copied_prefix() {
    for offset in 0..3 {
        let h = Harness::new();
        let mut q = h.queue();
        h.budget(10_000, |c| q.start(c)).unwrap();
        {
            let mut s = h.state.borrow_mut();
            s.fail_at = Some(s.calls.len() + offset);
            s.frames.push_back(Some(frame()));
        }
        let mut data = [99; 32];
        assert!(h.budget(10_000, |c| q.read(&mut data, c)).is_err());
        assert_eq!(data, [0; 32]);
        let calls = h.state.borrow().calls.len();
        assert!(h.budget(10_000, |c| q.read(&mut data, c)).is_err());
        assert_eq!(h.state.borrow().calls.len(), calls);
        h.budget(10_000, |c| q.stop(c));
        assert_eq!(h.calls("close"), 1);
    }
}
#[test]
fn bad_dequeue_index_length_and_error_flags_are_never_copied_or_requeued() {
    let mut cases = Vec::new();
    for index in [2, 4, u32::MAX] {
        cases.push(Dequeued { index, ..frame() });
    }
    for length in [0, 4095, 4097, usize::MAX] {
        cases.push(Dequeued { length, ..frame() });
    }
    for bytes_used in [0, 3, 33, 4097, usize::MAX] {
        cases.push(Dequeued {
            bytes_used,
            ..frame()
        });
    }
    cases.push(Dequeued {
        flags: BUFFER_ERROR,
        ..frame()
    });
    for bad in cases {
        let h = Harness::new();
        let mut q = h.queue();
        h.budget(10_000, |c| q.start(c)).unwrap();
        h.state.borrow_mut().frames.push_back(Some(bad));
        assert!(
            h.budget(10_000, |c| q.read(&mut [0; 32], c)).is_err(),
            "{bad:?}"
        );
        assert_eq!(h.calls("copy"), 0);
        assert_eq!(h.calls("queue"), 2);
    }
}
#[test]
fn deadline_after_dequeue_or_copy_never_requeues_frame() {
    for deadline in [101, 102] {
        let h = Harness::new();
        let mut q = h.queue();
        h.budget(10_000, |c| q.start(c)).unwrap();
        {
            let mut s = h.state.borrow_mut();
            s.advance = 1;
            s.frames.push_back(Some(frame()));
        }
        let mut data = [9; 32];
        let error = h.budget(deadline, |c| q.read(&mut data, c)).unwrap_err();
        assert_eq!(error.error, PortError::DeadlineExceeded);
        assert_eq!(h.calls("queue"), 2);
        assert_eq!(data, [0; 32]);
    }
}
#[test]
fn cancellation_and_expired_budget_admit_no_device_operation() {
    for cancel in [true, false] {
        let h = Harness::new();
        h.cancelled.set(cancel);
        let mut q = h.queue();
        let error = h
            .budget(if cancel { 10_000 } else { 100 }, |c| q.start(c))
            .unwrap_err();
        assert_eq!(
            error.error,
            if cancel {
                PortError::Cancelled
            } else {
                PortError::DeadlineExceeded
            }
        );
        assert!(h.state.borrow().calls.is_empty());
    }
}
#[test]
fn stop_failure_closes_and_does_not_release_or_retry_ambiguous_queue() {
    let h = Harness::new();
    let mut q = h.queue();
    h.budget(10_000, |c| q.start(c)).unwrap();
    {
        let mut s = h.state.borrow_mut();
        s.fail_at = Some(s.calls.len());
    }
    let report = h.budget(10_000, |c| q.stop(c));
    assert!(report.stream_off.is_some());
    assert_eq!(h.calls("unmap"), 1);
    assert_eq!(h.calls("release"), 0);
    assert_eq!(h.calls("close"), 1);
    assert_eq!(h.calls("off"), 1);
}
#[test]
fn expired_cleanup_budget_still_closes_and_reports_expiry() {
    let h = Harness::new();
    let mut q = h.queue();
    h.budget(10_000, |c| q.start(c)).unwrap();
    let report = h.budget(100, |c| q.stop(c));
    assert_eq!(
        report.first_failure().unwrap().error,
        PortError::DeadlineExceeded
    );
    assert_eq!(h.calls("off"), 0);
    assert_eq!(h.calls("release"), 0);
    assert_eq!(h.calls("unmap"), 1);
    assert_eq!(h.calls("close"), 1);
}
#[test]
fn cleanup_preserves_release_and_close_errors_separately() {
    for offset in [2, 3] {
        let h = Harness::new();
        let mut q = h.queue();
        h.budget(10_000, |c| q.start(c)).unwrap();
        {
            let mut s = h.state.borrow_mut();
            s.fail_at = Some(s.calls.len() + offset);
        }
        let report = h.budget(10_000, |c| q.stop(c));
        if offset == 2 {
            assert!(report.release_buffers.is_some());
        } else {
            assert!(report.close.is_some());
        }
        assert_eq!(h.calls("close"), 1);
    }
}
#[test]
fn timestamp_masks_are_not_zero_bit_contains_and_unknown_is_not_realtime() {
    for (flags, domain, point) in [
        (0, TimestampDomain::Unknown, TimestampPoint::EndOfFrame),
        (
            TIMESTAMP_COPY,
            TimestampDomain::Unknown,
            TimestampPoint::EndOfFrame,
        ),
        (
            TIMESTAMP_MONOTONIC,
            TimestampDomain::HostMonotonic,
            TimestampPoint::EndOfFrame,
        ),
        (
            TIMESTAMP_MONOTONIC | SOURCE_SOE,
            TimestampDomain::HostMonotonic,
            TimestampPoint::StartOfExposure,
        ),
    ] {
        let result = timestamp(Dequeued { flags, ..frame() }).unwrap();
        assert_eq!(
            (result.domain, result.point, result.micros),
            (domain, point, 1_000_002)
        );
    }
    for flags in [0x6000, 0xe000, 0x20000, 0x70000] {
        assert!(timestamp(Dequeued { flags, ..frame() }).is_err());
    }
}
#[test]
fn signed_timeval_and_overflow_are_rejected() {
    for (seconds, micros) in [(-1, 0), (1, -1), (1, 1_000_000), (i64::MAX, 0)] {
        assert!(
            timestamp(Dequeued {
                seconds,
                micros,
                ..frame()
            })
            .is_err()
        );
    }
}
#[test]
fn changed_driver_timestamp_domain_faults_without_publishing_next_frame() {
    let h = Harness::new();
    let mut q = h.queue();
    h.budget(10_000, |c| q.start(c)).unwrap();
    h.state.borrow_mut().frames.extend([
        Some(frame()),
        Some(Dequeued {
            flags: 0,
            ..frame()
        }),
    ]);
    assert!(h.budget(10_000, |c| q.read(&mut [0; 32], c)).is_ok());
    assert!(h.budget(10_000, |c| q.read(&mut [0; 32], c)).is_err());
    assert_eq!(h.calls("copy"), 1);
}
#[test]
fn clock_regression_is_a_fault_before_another_operation() {
    let h = Harness::new();
    h.time.set(99);
    let mut q = h.queue();
    let error = h.budget(10_000, |c| q.start(c)).unwrap_err();
    assert_eq!(error.stage, Stage::Clock);
    assert!(h.state.borrow().calls.is_empty());
}

#[test]
fn cancellation_arriving_during_dequeue_or_copy_never_requeues_or_exports() {
    for offset in [0, 1] {
        let h = Harness::new();
        let mut q = h.queue();
        h.budget(10_000, |c| q.start(c)).unwrap();
        {
            let mut state = h.state.borrow_mut();
            state.cancel_at = Some(state.calls.len() + offset);
            state.frames.push_back(Some(frame()));
        }
        let mut bytes = [9; 32];
        assert_eq!(
            h.budget(10_000, |c| q.read(&mut bytes, c))
                .unwrap_err()
                .error,
            PortError::Cancelled
        );
        assert_eq!(bytes, [0; 32]);
        assert_eq!(h.calls("queue"), 2);
        // The production port uses a separate uncancelled cleanup budget.
        h.cancelled.set(false);
        h.budget(10_000, |c| q.stop(c));
        assert_eq!(h.calls("close"), 1);
    }
}
#[test]
fn driver_may_negotiate_fewer_bounded_buffers_without_fabricating_requested_count() {
    let h = Harness::new();
    h.state.borrow_mut().count = 3;
    let mut q = h.queue();
    q.mode.buffers = 4;
    let actual = h.budget(10_000, |c| q.start(c)).unwrap();
    assert_eq!(actual.buffers, 3);
    assert_eq!(h.calls("map"), 3);
    assert_eq!(h.calls("queue"), 3);
}
