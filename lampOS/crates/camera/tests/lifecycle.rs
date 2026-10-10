use lamp_camera::*;
use lamp_interaction::{
    AdmissionState, AdmittedInput, BootId, CaptureState, Controller, MonoTime, Permission,
};
use std::{
    cell::{Cell, RefCell},
    collections::{HashSet, VecDeque},
    rc::Rc,
};

fn t(us: u64) -> MonoTime {
    MonoTime::from_micros(us)
}
fn boot(n: u8) -> BootId {
    BootId::new([n; 16]).unwrap()
}
fn source() -> SourceId {
    SourceId::new("/dev/explicit-camera").unwrap()
}
fn config() -> CaptureConfig {
    CaptureConfig {
        source: source(),
        width: 1280,
        height: 720,
        interval: None,
        max_frame_bytes: 1024,
        buffers: 3,
    }
}
fn mode() -> NegotiatedMode {
    NegotiatedMode {
        source: source(),
        format: PixelFormat::MJPG,
        width: 1280,
        height: 720,
        interval: None,
        size_image: 512,
        buffers: 3,
    }
}
#[derive(Clone)]
struct FakeClock {
    us: Rc<Cell<u64>>,
    fail: Rc<Cell<bool>>,
    scheduled: Rc<RefCell<VecDeque<u64>>>,
}
impl FakeClock {
    fn new() -> Self {
        Self {
            us: Rc::new(Cell::new(0)),
            fail: Rc::new(Cell::new(false)),
            scheduled: Rc::new(RefCell::new(VecDeque::new())),
        }
    }
    fn set(&self, us: u64) {
        self.us.set(us);
    }
    fn advance(&self, us: u64) {
        self.us.set(self.us.get() + us);
    }
    fn now_value(&self) -> MonoTime {
        t(self.us.get())
    }
}
impl Clock for FakeClock {
    fn now(&mut self) -> Result<MonoTime, ClockError> {
        if let Some(us) = self.scheduled.borrow_mut().pop_front() {
            self.us.set(us);
        }
        if self.fail.get() {
            Err(ClockError)
        } else {
            Ok(self.now_value())
        }
    }
}
struct FrameAction {
    sequence: u32,
    bytes: Vec<u8>,
    reported_len: Option<usize>,
    source: Option<SourceId>,
    capture: Option<CaptureId>,
    timestamp: Option<DriverTimestamp>,
    elapsed: u64,
    rewind: Option<u64>,
}
impl FrameAction {
    fn new(sequence: u32) -> Self {
        Self {
            sequence,
            bytes: vec![0xff, 0xd8, (sequence % 255) as u8, 0xff, 0xd9],
            reported_len: None,
            source: None,
            capture: None,
            timestamp: None,
            elapsed: 0,
            rewind: None,
        }
    }
}
enum Action {
    Frame(Box<FrameAction>),
    Empty(u64),
    Fail(PortError),
}
struct PortState {
    starts: u32,
    stops: u32,
    reads: u32,
    active: Option<CaptureId>,
    negotiated: NegotiatedMode,
    start_error: Option<PortError>,
    stop_error: Option<PortError>,
    start_elapsed: u64,
    stop_elapsed: u64,
    actions: VecDeque<Action>,
    addresses: HashSet<usize>,
    slice_lengths: HashSet<usize>,
}
impl Default for PortState {
    fn default() -> Self {
        Self {
            starts: 0,
            stops: 0,
            reads: 0,
            active: None,
            negotiated: mode(),
            start_error: None,
            stop_error: None,
            start_elapsed: 0,
            stop_elapsed: 0,
            actions: VecDeque::new(),
            addresses: HashSet::new(),
            slice_lengths: HashSet::new(),
        }
    }
}
struct FakePort {
    state: Rc<RefCell<PortState>>,
    clock: FakeClock,
}
impl PortIo for FakePort {
    fn start(
        &mut self,
        _: CaptureConfig,
        capture: CaptureId,
        budget: OperationBudget,
    ) -> Result<NegotiatedMode, PortError> {
        assert!(budget.deadline > budget.started_at);
        let mut state = self.state.borrow_mut();
        state.starts += 1;
        state.active = Some(capture);
        self.clock.advance(state.start_elapsed);
        state.start_error.map_or(Ok(state.negotiated), Err)
    }
    fn try_frame(
        &mut self,
        destination: &mut [u8],
        budget: OperationBudget,
    ) -> Result<Option<PortFrame>, PortError> {
        assert!(budget.deadline > budget.started_at);
        let mut state = self.state.borrow_mut();
        state.reads += 1;
        state.addresses.insert(destination.as_ptr() as usize);
        state.slice_lengths.insert(destination.len());
        match state.actions.pop_front().unwrap_or(Action::Empty(0)) {
            Action::Empty(us) => {
                self.clock.advance(us);
                Ok(None)
            }
            Action::Fail(error) => Err(error),
            Action::Frame(frame) => {
                let copied = frame.bytes.len().min(destination.len());
                destination[..copied].copy_from_slice(&frame.bytes[..copied]);
                self.clock.advance(frame.elapsed);
                if let Some(us) = frame.rewind {
                    self.clock.set(us);
                }
                Ok(Some(PortFrame {
                    capture: frame.capture.or(state.active).unwrap(),
                    source: frame.source.unwrap_or_else(source),
                    bytes_used: frame.reported_len.unwrap_or(frame.bytes.len()),
                    driver_sequence: frame.sequence,
                    timestamp: frame.timestamp,
                }))
            }
        }
    }
    fn stop(&mut self, _: OperationBudget) -> Result<(), PortError> {
        let mut state = self.state.borrow_mut();
        state.stops += 1;
        state.active = None;
        self.clock.advance(state.stop_elapsed);
        state.stop_error.map_or(Ok(()), Err)
    }
}
struct Rig {
    capture: Capture<FakePort, FakeClock>,
    controller: Controller,
    port: Rc<RefCell<PortState>>,
    clock: FakeClock,
}
impl Rig {
    fn new() -> Self {
        let clock = FakeClock::new();
        let port = Rc::new(RefCell::new(PortState::default()));
        let capture = Capture::new(
            FakePort {
                state: port.clone(),
                clock: clock.clone(),
            },
            clock.clone(),
            boot(1),
            boot(2),
            config(),
        )
        .unwrap();
        Self {
            capture,
            controller: Controller::new(boot(1), t(0)),
            port,
            clock,
        }
    }
    fn refresh(&mut self) -> Result<Status, Error> {
        let snapshot = self.controller.snapshot(self.clock.now_value()).unwrap();
        self.capture.install_authority(snapshot)
    }
    fn permission(&mut self, permission: Permission) {
        self.controller
            .set_camera_permission(self.clock.now_value(), permission)
            .unwrap();
        self.refresh().unwrap();
    }
    fn started() -> Self {
        let mut rig = Self::new();
        rig.permission(Permission::Allowed);
        rig.capture.start().unwrap();
        rig
    }
    fn action(&mut self, frame: FrameAction) {
        self.port
            .borrow_mut()
            .actions
            .push_back(Action::Frame(Box::new(frame)));
    }
    fn frame(&mut self, sequence: u32) -> FrameObservation {
        self.action(FrameAction::new(sequence));
        self.capture.poll().unwrap().frame.unwrap()
    }
    fn tick_refresh(&mut self, us: u64) {
        self.clock.set(us);
        self.refresh().unwrap();
    }
}

#[test]
fn construction_is_closed_and_cannot_start_without_fresh_permission() {
    let mut rig = Rig::new();
    assert_eq!(rig.capture.maintain().unwrap().phase, Phase::Closed);
    assert_eq!(
        rig.capture.start().unwrap_err().kind,
        ErrorKind::Authority(lamp_interaction::Error::NoAuthority)
    );
    rig.refresh().unwrap();
    assert_eq!(
        rig.capture.start().unwrap_err().kind,
        ErrorKind::Authority(lamp_interaction::Error::PrivacyClosed)
    );
    assert_eq!(rig.port.borrow().starts, 0);
    assert_eq!(rig.port.borrow().reads, 0);
}

#[test]
fn start_and_would_block_are_not_readiness_and_first_frame_keeps_original_grant() {
    let mut rig = Rig::started();
    assert_eq!(rig.capture.maintain().unwrap().phase, Phase::Acquiring);
    assert!(rig.capture.begin_delivery().unwrap().is_none());
    assert_eq!(rig.capture.poll().unwrap().status.phase, Phase::Acquiring);
    let grant = rig.controller.camera_grant(t(0)).unwrap();
    let observation = rig.frame(31);
    assert_eq!(observation.grant, grant);
    assert_eq!(observation.mode.interval, None);
    assert_eq!(rig.capture.maintain().unwrap().phase, Phase::Ready);
    let token = rig.capture.begin_delivery().unwrap().unwrap();
    let view = rig.capture.delivery_view(&token).unwrap();
    assert_eq!(view.observation, observation);
    assert_eq!(view.bytes, [0xff, 0xd8, 31, 0xff, 0xd9]);
    rig.capture.finish_delivery(token).unwrap();
    assert_eq!(rig.capture.maintain().unwrap().phase, Phase::Acquiring);
}

#[test]
fn old_duplicate_and_wrong_boot_authority_do_not_close_newer_valid_capture() {
    let mut rig = Rig::started();
    let old = rig.controller.snapshot(t(0)).unwrap();
    rig.capture.install_authority(old).unwrap();
    rig.tick_refresh(10);
    rig.frame(1);
    for _ in 0..3 {
        assert_eq!(
            rig.capture.install_authority(old).unwrap_err().kind,
            ErrorKind::Authority(lamp_interaction::Error::StaleSnapshot)
        );
        assert_eq!(rig.capture.maintain().unwrap().phase, Phase::Ready);
    }
    let mut other = Controller::new(boot(8), t(10));
    assert_eq!(
        rig.capture
            .install_authority(other.snapshot(t(10)).unwrap())
            .unwrap_err()
            .kind,
        ErrorKind::Authority(lamp_interaction::Error::WrongBoot)
    );
    assert_eq!(rig.port.borrow().stops, 0);
}

#[test]
fn conversation_transition_preserves_camera_capture_and_grant() {
    let mut rig = Rig::started();
    let original = rig.frame(1);
    rig.controller
        .set_microphone_permission(t(1), Permission::Allowed)
        .unwrap();
    rig.controller
        .set_capture(t(1), CaptureState::RetainingUntil(t(100_000)))
        .unwrap();
    rig.controller
        .set_admission(t(1), AdmissionState::OpenUntil(t(100_000)))
        .unwrap();
    rig.controller.admit(t(1), AdmittedInput::NewTurn).unwrap();
    rig.tick_refresh(1);
    let next = rig.frame(2);
    assert_eq!(next.id.capture, original.id.capture);
    assert_eq!(next.grant, original.grant);
    assert_eq!(rig.port.borrow().starts, 1);
    assert_eq!(rig.port.borrow().stops, 0);
}

#[test]
fn privacy_clears_pending_and_inflight_without_automatic_reopen() {
    let mut rig = Rig::started();
    rig.frame(1);
    let old = rig.capture.begin_delivery().unwrap().unwrap();
    rig.frame(2);
    rig.permission(Permission::Denied);
    assert_eq!(rig.capture.counters().invalidated, 2);
    assert_eq!(rig.capture.maintain().unwrap().phase, Phase::Closed);
    assert_eq!(
        rig.capture.delivery_view(&old).unwrap_err().kind,
        ErrorKind::StaleDelivery
    );
    rig.permission(Permission::Allowed);
    assert_eq!(rig.capture.maintain().unwrap().phase, Phase::Closed);
    assert_eq!(rig.port.borrow().starts, 1);
    assert_eq!(rig.port.borrow().stops, 1);
    assert!(rig.capture.begin_delivery().unwrap().is_none());
}

#[test]
fn coalesced_close_reopen_invalidates_original_grant_and_requires_explicit_start() {
    let mut rig = Rig::started();
    let old = rig.frame(1);
    let token = rig.capture.begin_delivery().unwrap().unwrap();
    rig.controller
        .set_camera_permission(t(1), Permission::Denied)
        .unwrap();
    rig.controller
        .set_camera_permission(t(2), Permission::Allowed)
        .unwrap();
    rig.tick_refresh(2);
    assert_eq!(rig.capture.maintain().unwrap().phase, Phase::Closed);
    let started = rig.capture.start().unwrap();
    assert!(started.capture.epoch > old.id.capture.epoch);
    let new = rig.frame(1);
    assert_ne!(new.grant, old.grant);
    let new_token = rig.capture.begin_delivery().unwrap().unwrap();
    assert_eq!(
        rig.capture.finish_delivery(token).unwrap_err().kind,
        ErrorKind::StaleDelivery
    );
    assert_eq!(
        rig.capture.delivery_view(&new_token).unwrap().observation,
        new
    );
}

#[test]
fn expiry_during_read_cannot_publish_or_relabel_bytes() {
    let mut rig = Rig::started();
    rig.clock.set(249_000);
    let mut frame = FrameAction::new(1);
    frame.elapsed = 2_000;
    rig.action(frame);
    assert_eq!(
        rig.capture.poll().unwrap_err().kind,
        ErrorKind::Authority(lamp_interaction::Error::ExpiredState)
    );
    assert_eq!(rig.capture.counters().frames_received, 0);
    assert_eq!(rig.port.borrow().stops, 1);
    assert_eq!(
        rig.capture.begin_delivery().unwrap_err().kind,
        ErrorKind::Faulted
    );
}

#[test]
fn fresh_heartbeat_after_control_gap_cannot_revive_active_capture() {
    let mut rig = Rig::started();
    rig.frame(1);
    rig.clock.set(250_000);
    assert_eq!(
        rig.refresh().unwrap_err().kind,
        ErrorKind::Authority(lamp_interaction::Error::ExpiredState)
    );
    assert_eq!(rig.capture.counters().invalidated, 1);
    assert_eq!(rig.port.borrow().stops, 1);
}

#[test]
fn first_frame_port_fault_is_terminal_and_cleanup_is_attempted_once() {
    let mut rig = Rig::started();
    rig.port
        .borrow_mut()
        .actions
        .push_back(Action::Fail(PortError::Disconnected));
    assert_eq!(
        rig.capture.poll().unwrap_err().kind,
        ErrorKind::Port(PortError::Disconnected)
    );
    assert_eq!(rig.capture.counters().frames_received, 0);
    assert_eq!(rig.capture.start().unwrap_err().kind, ErrorKind::Faulted);
    assert_eq!(rig.capture.maintain().unwrap_err().kind, ErrorKind::Faulted);
    assert_eq!(rig.port.borrow().stops, 1);
}

#[test]
fn latest_only_storage_replaces_pending_while_delivery_does_not_block_capture() {
    let mut rig = Rig::started();
    rig.frame(1);
    let in_flight = rig.capture.begin_delivery().unwrap().unwrap();
    for sequence in 2..=1000 {
        rig.frame(sequence);
    }
    assert_eq!(rig.capture.counters().frames_received, 1000);
    assert_eq!(rig.capture.counters().pending_replaced, 998);
    assert_eq!(
        rig.capture
            .delivery_view(&in_flight)
            .unwrap()
            .observation
            .driver_sequence,
        1
    );
    assert_eq!(
        rig.capture.begin_delivery().unwrap_err().kind,
        ErrorKind::DeliveryBusy
    );
    rig.capture.finish_delivery(in_flight).unwrap();
    let newest = rig.capture.begin_delivery().unwrap().unwrap();
    assert_eq!(
        rig.capture
            .delivery_view(&newest)
            .unwrap()
            .observation
            .driver_sequence,
        1000
    );
    // Destination addresses come from the three construction-time slots only.
    assert!(rig.port.borrow().addresses.len() <= 3);
    assert_eq!(rig.port.borrow().slice_lengths, HashSet::from([512]));
}

#[test]
fn expired_delivery_does_not_erase_a_newer_pending_frame() {
    let mut rig = Rig::started();
    rig.frame(1);
    let old = rig.capture.begin_delivery().unwrap().unwrap();
    rig.tick_refresh(200_000);
    rig.frame(2);
    rig.tick_refresh(250_000);
    assert_eq!(
        rig.capture.delivery_view(&old).unwrap_err().kind,
        ErrorKind::StaleDelivery
    );
    assert_eq!(rig.capture.counters().aged_out, 1);
    let new = rig.capture.begin_delivery().unwrap().unwrap();
    assert_eq!(
        rig.capture
            .delivery_view(&new)
            .unwrap()
            .observation
            .driver_sequence,
        2
    );
    assert_eq!(rig.port.borrow().stops, 0);
}

#[test]
fn no_fresh_retained_frame_removes_readiness_before_capture_silence_fault() {
    let mut rig = Rig::started();
    rig.frame(1);
    rig.tick_refresh(200_000);
    rig.tick_refresh(250_000);
    assert_eq!(rig.capture.maintain().unwrap().phase, Phase::Acquiring);
    assert!(rig.capture.begin_delivery().unwrap().is_none());
    for us in [450_000, 650_000, 850_000] {
        rig.tick_refresh(us);
    }
    rig.clock.set(1_000_000);
    assert_eq!(
        rig.refresh().unwrap_err().kind,
        ErrorKind::FrameSilenceTimeout
    );
}

#[test]
fn first_frame_deadline_is_enforced_while_authority_keeps_refreshing() {
    let mut rig = Rig::started();
    for us in (200_000..2_000_000).step_by(200_000) {
        rig.tick_refresh(us);
        rig.capture.poll().unwrap();
    }
    rig.clock.set(2_000_000);
    assert_eq!(
        rig.refresh().unwrap_err().kind,
        ErrorKind::FirstFrameTimeout
    );
    assert_eq!(rig.port.borrow().stops, 1);
}

#[test]
fn frame_arriving_after_first_deadline_is_not_accepted() {
    let mut rig = Rig::started();
    for us in (200_000..2_000_000).step_by(200_000) {
        rig.tick_refresh(us);
    }
    rig.tick_refresh(1_999_000);
    let mut frame = FrameAction::new(1);
    frame.elapsed = 2_000;
    rig.action(frame);
    assert_eq!(
        rig.capture.poll().unwrap_err().kind,
        ErrorKind::FirstFrameTimeout
    );
    assert_eq!(rig.capture.counters().frames_received, 0);
}

#[test]
fn read_overrun_and_clock_regression_fail_closed_without_retry() {
    for (elapsed, rewind, expected) in [
        (READ_BUDGET_US, None, ErrorKind::ReadBudgetExceeded),
        (0, Some(0), ErrorKind::ClockRegression),
    ] {
        let mut rig = Rig::started();
        rig.tick_refresh(1);
        let mut frame = FrameAction::new(1);
        frame.elapsed = elapsed;
        frame.rewind = rewind;
        rig.action(frame);
        assert_eq!(rig.capture.poll().unwrap_err().kind, expected);
        assert_eq!(rig.port.borrow().reads, 1);
        assert_eq!(rig.port.borrow().stops, 1);
    }
}

#[test]
fn failed_cleanup_is_reported_and_never_retried_implicitly() {
    let mut rig = Rig::started();
    rig.frame(1);
    rig.port.borrow_mut().stop_error = Some(PortError::Io(5));
    let error = rig.capture.stop().unwrap_err();
    assert_eq!(error.kind, ErrorKind::Port(PortError::Io(5)));
    assert_eq!(error.cleanup.unwrap().port, Some(PortError::Io(5)));
    assert_eq!(rig.capture.counters().invalidated, 1);
    assert_eq!(rig.capture.stop().unwrap_err().kind, ErrorKind::Faulted);
    assert_eq!(rig.port.borrow().stops, 1);
}

#[test]
fn slow_cleanup_reports_observed_overrun_after_invalidating_data() {
    let mut rig = Rig::started();
    rig.frame(1);
    rig.port.borrow_mut().stop_elapsed = STOP_BUDGET_US;
    let error = rig.capture.stop().unwrap_err();
    assert_eq!(error.kind, ErrorKind::StopBudgetExceeded);
    assert!(error.cleanup.unwrap().budget_exceeded);
    assert_eq!(rig.capture.counters().invalidated, 1);
}

#[test]
fn explicit_stop_is_idempotent_and_restart_changes_capture_epoch() {
    let mut rig = Rig::started();
    let first = rig.frame(1);
    rig.capture.stop().unwrap();
    rig.capture.stop().unwrap();
    assert_eq!(rig.port.borrow().stops, 1);
    let second = rig.capture.start().unwrap();
    assert_ne!(second.capture, first.id.capture);
    assert_eq!(rig.capture.maintain().unwrap().phase, Phase::Acquiring);
}

#[test]
fn control_loss_invalidates_frames_and_fault_cannot_be_recovered_by_snapshot() {
    let mut rig = Rig::started();
    rig.frame(1);
    rig.capture.control_lost();
    assert_eq!(rig.capture.counters().invalidated, 1);
    assert_eq!(rig.refresh().unwrap_err().kind, ErrorKind::Faulted);
    assert_eq!(rig.port.borrow().stops, 1);
}

#[test]
fn clock_failure_erases_retained_data_even_if_cleanup_clock_also_fails() {
    let mut rig = Rig::started();
    rig.frame(1);
    rig.clock.fail.set(true);
    let error = rig.capture.maintain().unwrap_err();
    assert_eq!(error.kind, ErrorKind::ClockUnavailable);
    assert_eq!(
        error.cleanup.unwrap().clock,
        Some(ErrorKind::ClockUnavailable)
    );
    assert_eq!(rig.capture.counters().invalidated, 1);
    assert_eq!(rig.port.borrow().stops, 1);
}

#[test]
fn old_capture_wrong_source_oversized_and_invalid_envelope_are_terminal() {
    for case in 0..5 {
        let mut rig = Rig::started();
        let mut frame = FrameAction::new(1);
        let expected = match case {
            0 => {
                frame.capture = Some(CaptureId {
                    worker: boot(9),
                    epoch: std::num::NonZeroU64::MIN,
                });
                ErrorKind::OldCapture
            }
            1 => {
                frame.source = Some(SourceId::new("/dev/other-camera").unwrap());
                ErrorKind::WrongSource
            }
            2 => {
                frame.reported_len = Some(513);
                ErrorKind::InvalidFrameSize
            }
            3 => {
                frame.reported_len = Some(0);
                ErrorKind::InvalidFrameSize
            }
            _ => {
                frame.bytes[0] = 0;
                ErrorKind::InvalidJpegEnvelope
            }
        };
        rig.action(frame);
        assert_eq!(rig.capture.poll().unwrap_err().kind, expected);
        assert_eq!(rig.capture.counters().frames_received, 0);
        assert_eq!(rig.port.borrow().stops, 1);
    }
}

#[test]
fn duplicate_and_backwards_driver_sequence_are_not_fresh_frames() {
    for seq in [10, 9, 10_u32.wrapping_add(1 << 31)] {
        let mut rig = Rig::started();
        rig.frame(10);
        rig.action(FrameAction::new(seq));
        assert_eq!(
            rig.capture.poll().unwrap_err().kind,
            ErrorKind::OldDriverSequence
        );
        assert_eq!(rig.capture.counters().frames_received, 1);
    }
}

#[test]
fn driver_sequence_wrap_is_forward_and_gaps_are_counted() {
    let mut rig = Rig::started();
    rig.frame(u32::MAX - 1);
    rig.frame(1);
    assert_eq!(rig.capture.counters().driver_sequence_gaps, 2);
    assert_eq!(rig.capture.counters().frames_received, 2);
}

#[test]
fn timestamp_domain_and_point_are_retained_without_invented_exposure_freshness() {
    let mut rig = Rig::started();
    let timestamp = DriverTimestamp {
        micros: u64::MAX,
        domain: TimestampDomain::Unknown,
        point: TimestampPoint::Unknown,
    };
    let mut frame = FrameAction::new(1);
    frame.timestamp = Some(timestamp);
    frame.elapsed = 900;
    rig.action(frame);
    let report = rig.capture.poll().unwrap();
    let observation = report.frame.unwrap();
    assert_eq!(observation.driver_timestamp, Some(timestamp));
    assert_eq!(
        observation.dequeue,
        CallTiming {
            started_at: t(0),
            completed_at: t(900)
        }
    );
    assert_eq!(observation.dequeue_age_us(t(1900)), Some(1000));
    assert_eq!(report.completed_at, t(900));
}

#[test]
fn future_old_and_regressing_monotonic_driver_timestamps_are_rejected() {
    for value in [50, 101] {
        let mut rig = Rig::new();
        rig.clock.set(100);
        rig.permission(Permission::Allowed);
        rig.capture.start().unwrap();
        let mut frame = FrameAction::new(1);
        frame.timestamp = Some(DriverTimestamp {
            micros: value,
            domain: TimestampDomain::HostMonotonic,
            point: TimestampPoint::StartOfExposure,
        });
        rig.action(frame);
        assert_eq!(
            rig.capture.poll().unwrap_err().kind,
            ErrorKind::InvalidDriverTimestamp
        );
    }
    let mut rig = Rig::started();
    rig.tick_refresh(100);
    for (seq, timestamp) in [(1, 100), (2, 99)] {
        let mut frame = FrameAction::new(seq);
        frame.timestamp = Some(DriverTimestamp {
            micros: timestamp,
            domain: TimestampDomain::HostMonotonic,
            point: TimestampPoint::EndOfFrame,
        });
        rig.action(frame);
        if seq == 1 {
            rig.capture.poll().unwrap();
        } else {
            assert_eq!(
                rig.capture.poll().unwrap_err().kind,
                ErrorKind::InvalidDriverTimestamp
            );
        }
    }
}

#[test]
fn realtime_clock_jump_is_recorded_not_misclassified_as_host_age() {
    let mut rig = Rig::started();
    for (seq, timestamp) in [(1, 500), (2, 1)] {
        let mut frame = FrameAction::new(seq);
        frame.timestamp = Some(DriverTimestamp {
            micros: timestamp,
            domain: TimestampDomain::Realtime,
            point: TimestampPoint::Unknown,
        });
        rig.action(frame);
        rig.capture.poll().unwrap();
    }
    assert_eq!(rig.capture.counters().frames_received, 2);
}

#[test]
fn incompatible_negotiation_never_marks_open_capture_ready() {
    for case in 0..7 {
        let mut rig = Rig::new();
        rig.permission(Permission::Allowed);
        let mut actual = mode();
        match case {
            0 => actual.format = PixelFormat(*b"YUYV"),
            1 => actual.width = 640,
            2 => actual.height = 480,
            3 => actual.source = SourceId::new("wrong").unwrap(),
            4 => actual.size_image = 1025,
            5 => actual.buffers = 1,
            _ => actual.buffers = 4,
        }
        rig.port.borrow_mut().negotiated = actual;
        assert_eq!(
            rig.capture.start().unwrap_err().kind,
            ErrorKind::IncompatibleNegotiation
        );
        assert_eq!(rig.port.borrow().stops, 1);
    }
}

#[test]
fn start_failure_or_observed_overrun_runs_one_cleanup_attempt() {
    for case in 0..2 {
        let mut rig = Rig::new();
        rig.permission(Permission::Allowed);
        let expected = if case == 0 {
            rig.port.borrow_mut().start_error = Some(PortError::Unsupported);
            ErrorKind::Port(PortError::Unsupported)
        } else {
            rig.port.borrow_mut().start_elapsed = START_BUDGET_US;
            ErrorKind::StartBudgetExceeded
        };
        assert_eq!(rig.capture.start().unwrap_err().kind, expected);
        assert_eq!(rig.port.borrow().starts, 1);
        assert_eq!(rig.port.borrow().stops, 1);
    }
}

#[test]
fn config_source_interval_and_frame_bounds_are_validated_before_port_open() {
    for invalid in [
        "",
        "contains space",
        "contains\nnewline",
        "\\invalid",
        &"x".repeat(129),
    ] {
        assert!(SourceId::new(invalid).is_err());
    }
    assert_eq!(SourceId::new(&"x".repeat(128)).unwrap().as_str().len(), 128);
    assert!(FrameInterval::new(0, 30).is_err());
    assert!(FrameInterval::new(1, 0).is_err());
    for case in 0..7 {
        let mut cfg = config();
        match case {
            0 => cfg.width = 0,
            1 => cfg.width = 1281,
            2 => cfg.height = 721,
            3 => cfg.max_frame_bytes = MAX_FRAME_BYTES + 1,
            4 => cfg.max_frame_bytes = 3,
            5 => cfg.buffers = 1,
            _ => cfg.buffers = 5,
        }
        assert!(cfg.validate().is_err());
    }
}

#[test]
fn explicit_rate_must_be_reported_and_match_as_a_rational() {
    for actual in [
        None,
        Some(FrameInterval::new(1, 29).unwrap()),
        Some(FrameInterval::new(2, 60).unwrap()),
    ] {
        let clock = FakeClock::new();
        let port = Rc::new(RefCell::new(PortState::default()));
        port.borrow_mut().negotiated.interval = actual;
        let mut cfg = config();
        cfg.interval = Some(FrameInterval::new(1, 30).unwrap());
        let mut capture = Capture::new(
            FakePort {
                state: port,
                clock: clock.clone(),
            },
            clock,
            boot(1),
            boot(2),
            cfg,
        )
        .unwrap();
        let mut controller = Controller::new(boot(1), t(0));
        controller
            .set_camera_permission(t(0), Permission::Allowed)
            .unwrap();
        capture
            .install_authority(controller.snapshot(t(0)).unwrap())
            .unwrap();
        if actual == Some(FrameInterval::new(2, 60).unwrap()) {
            assert!(capture.start().is_ok());
        } else {
            assert_eq!(
                capture.start().unwrap_err().kind,
                ErrorKind::IncompatibleNegotiation
            );
        }
    }
}

#[test]
fn fresh_pre_read_check_prevents_using_time_from_before_maintenance() {
    let mut rig = Rig::started();
    rig.action(FrameAction::new(1));
    rig.clock.scheduled.borrow_mut().extend([0, 250_000]);
    assert_eq!(
        rig.capture.poll().unwrap_err().kind,
        ErrorKind::Authority(lamp_interaction::Error::ExpiredState)
    );
    assert_eq!(rig.port.borrow().reads, 0);
    assert_eq!(rig.port.borrow().stops, 1);
}

#[test]
fn expiry_during_retention_work_invalidates_the_frame_before_publication() {
    let mut rig = Rig::started();
    rig.action(FrameAction::new(1));
    rig.clock
        .scheduled
        .borrow_mut()
        .extend([0, 0, 1_000, 250_000]);
    assert_eq!(
        rig.capture.poll().unwrap_err().kind,
        ErrorKind::Authority(lamp_interaction::Error::ExpiredState)
    );
    assert_eq!(rig.capture.counters().invalidated, 1);
    assert_eq!(rig.port.borrow().stops, 1);
}

#[test]
fn delivery_has_a_fresh_check_after_maintenance_before_exposing_bytes() {
    let mut rig = Rig::started();
    rig.frame(1);
    let token = rig.capture.begin_delivery().unwrap().unwrap();
    rig.clock.scheduled.borrow_mut().extend([0, 250_000]);
    assert_eq!(
        rig.capture.delivery_view(&token).unwrap_err().kind,
        ErrorKind::Authority(lamp_interaction::Error::ExpiredState)
    );
    assert_eq!(rig.capture.counters().invalidated, 1);
    assert_eq!(rig.port.borrow().stops, 1);
}
