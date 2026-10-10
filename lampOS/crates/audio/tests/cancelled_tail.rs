use lamp_audio::ownership::{MAX_CANCELLED_TAIL_US, PlaybackOwnership, QueueError};
use lamp_interaction::{
    AdmissionState, AdmittedInput, BootId, BoundaryGuard, CameraGrant, CaptureState, Controller,
    Error, MonoTime, OutputKind, OutputPermit, Permission, TurnOwner, ZeroAuthority,
};

fn at(us: u64) -> MonoTime {
    MonoTime::from_micros(us)
}

struct Fixture {
    controller: Controller,
    guard: BoundaryGuard,
    permit: OutputPermit,
    microphone: ZeroAuthority,
    camera: CameraGrant,
}

impl Fixture {
    fn new(start: u64) -> Self {
        let boot = BootId::new([119; 16]).unwrap();
        let mut controller = Controller::new(boot, at(start));
        controller
            .set_microphone_permission(at(start), Permission::Allowed)
            .unwrap();
        controller
            .set_camera_permission(at(start), Permission::Allowed)
            .unwrap();
        controller
            .set_capture(
                at(start),
                CaptureState::RetainingUntil(at(start + 1_000_000)),
            )
            .unwrap();
        controller
            .set_admission(at(start), AdmissionState::OpenUntil(at(start + 1_000_000)))
            .unwrap();
        let owner = controller.admit(at(start), AdmittedInput::NewTurn).unwrap();
        controller.input_ended(at(start), owner).unwrap();
        let camera = controller.camera_grant(at(start)).unwrap();
        let plan = controller
            .plan_output(at(start), owner, OutputKind::Speech, None)
            .unwrap();
        let permit = controller.issue_output(at(start), plan, 250_000).unwrap();
        let mut guard = BoundaryGuard::new(boot, at(start));
        guard
            .install(at(start), controller.snapshot(at(start)).unwrap())
            .unwrap();
        let microphone = guard.zero_authority(at(start)).unwrap();
        Self {
            controller,
            guard,
            permit,
            microphone,
            camera,
        }
    }

    fn publish(&mut self, time: u64) {
        self.guard
            .install(at(time), self.controller.snapshot(at(time)).unwrap())
            .unwrap();
    }

    fn admit(&mut self, time: u64) -> TurnOwner {
        let owner = self
            .controller
            .admit(at(time), AdmittedInput::Interruption)
            .unwrap();
        self.publish(time);
        owner
    }

    fn queue(&self, frames: usize) -> PlaybackOwnership {
        let mut queue = PlaybackOwnership::new();
        queue
            .push_clock(self.permit, self.microphone, frames)
            .unwrap();
        queue
    }
}

#[test]
fn real_7v7_tail_keeps_epoch_cursors_and_only_actual_accepted_zeros_extend_the_clock() {
    const WRITE: u64 = 183_637_586_619;
    const ADMIT: u64 = 183_637_590_615;
    let mut f = Fixture::new(WRITE - 50_000);
    let mut q = f.queue(456);
    let successor = f.admit(ADMIT);
    q.check_clock_owned(at(ADMIT), &mut f.guard).unwrap();
    assert_eq!(q.check(at(ADMIT), &mut f.guard), Err(Error::StaleOwner));
    assert_eq!(
        f.guard.check(at(ADMIT), f.permit, OutputKind::Speech),
        Err(Error::StaleOwner)
    );
    let old_end = q.accepted_cursor();
    q.retire_to(288);
    q.check_clock_owned(at(ADMIT + 7_000), &mut f.guard)
        .unwrap();
    assert!(q.take_retired_tail().is_none());
    q.push_silence(f.microphone, 240).unwrap();
    assert_eq!(q.accepted_cursor().epoch, old_end.epoch);
    assert_eq!(
        q.push_clock(f.permit, f.microphone, 1),
        Err(QueueError::RetiringTail)
    );
    // The accepted zeros are still queued; only the fixed old range retires.
    q.retire_to(240);
    q.check_clock_owned(at(ADMIT + 20_000), &mut f.guard)
        .unwrap();
    let tail = q.take_retired_tail().unwrap();
    assert_eq!(tail.owner, f.permit.owner());
    assert_eq!(tail.superseded_by, successor);
    assert_eq!(tail.end_sample, old_end);
    assert_eq!(tail.queued_frames, 456);
    assert_eq!(tail.speech_frames, 456);
    assert_eq!(tail.started_at_us, ADMIT);
    assert_eq!(q.pending_frames(), 240);
    assert!(q.take_retired_tail().is_none());
}

#[test]
fn full_queue_partial_retirement_and_repeated_admissions_cannot_renew_the_tail() {
    let mut f = Fixture::new(0);
    let mut q = PlaybackOwnership::new();
    q.push_silence(f.microphone, 120).unwrap();
    q.push_clock(f.permit, f.microphone, 720).unwrap();
    q.push_silence(f.microphone, 120).unwrap();
    let successor = f.admit(1);
    q.check_clock_owned(at(1), &mut f.guard).unwrap();
    assert_eq!(q.writable_frames(240, 960), 0);
    q.retire_to(1_200);
    assert_eq!(q.pending_frames(), 960);
    assert!(q.take_retired_tail().is_none());
    q.retire_to(721); // Keep the final frame of an old front speech entry.
    q.push_silence(f.microphone, 239).unwrap();
    f.admit(50_000);
    q.check_clock_owned(at(50_000), &mut f.guard).unwrap();
    f.publish(100_000);
    q.check_clock_owned(at(100_000), &mut f.guard).unwrap();
    assert_eq!(
        q.check_clock_owned(at(1 + MAX_CANCELLED_TAIL_US), &mut f.guard)
            .unwrap_err()
            .reason,
        Error::InvalidLease
    );
    assert_ne!(f.guard.state().unwrap().owner(), Some(successor));
    assert!(q.take_retired_tail().is_none());
    let discarded = q.clear().unwrap();
    assert_eq!(discarded.epoch, 1);
    assert!(!q.is_retiring_tail());
    assert_eq!(q.accepted_cursor().epoch, 2);
}

#[test]
fn successor_input_end_does_not_renew_or_complete_a_latched_tail() {
    let mut f = Fixture::new(0);
    let mut q = f.queue(240);
    let owner = f.admit(1);
    q.check_clock_owned(at(1), &mut f.guard).unwrap();
    f.controller.input_ended(at(2), owner).unwrap();
    f.publish(2);
    q.check_clock_owned(at(2), &mut f.guard).unwrap();
    let plan = f
        .controller
        .plan_output(at(2), owner, OutputKind::Speech, None)
        .unwrap();
    let permit = f.controller.issue_output(at(2), plan, 100_000).unwrap();
    assert_eq!(
        q.push_clock(permit, f.microphone, 240),
        Err(QueueError::RetiringTail)
    );
    q.retire_to(0);
    q.check_clock_owned(at(3), &mut f.guard).unwrap();
    q.take_retired_tail().unwrap();
    q.push_clock(permit, f.microphone, 240).unwrap();
    q.check(at(3), &mut f.guard).unwrap();
}

#[test]
fn absent_or_ended_successor_and_unrecorded_privacy_remain_strict() {
    for mode in 0..3 {
        let mut f = Fixture::new(0);
        let mut q = if mode == 2 {
            let mut q = PlaybackOwnership::new();
            q.push(f.permit, 240).unwrap();
            q
        } else {
            f.queue(240)
        };
        if mode == 0 {
            f.controller.cancel_turn(at(1), f.permit.owner()).unwrap();
        } else {
            let new = f
                .controller
                .admit(at(1), AdmittedInput::Interruption)
                .unwrap();
            if mode == 1 {
                f.controller.input_ended(at(1), new).unwrap();
            }
        }
        f.publish(1);
        assert_eq!(
            q.check_clock_owned(at(1), &mut f.guard).unwrap_err().reason,
            Error::StaleOwner
        );
        assert!(!q.is_retiring_tail());
    }
}

#[test]
fn stale_owner_must_not_mask_camera_revocation_in_any_middle_entry() {
    let mut f = Fixture::new(0);
    let plan = f
        .controller
        .plan_output(at(0), f.permit.owner(), OutputKind::Speech, Some(f.camera))
        .unwrap();
    let visual = f.controller.issue_output(at(0), plan, 250_000).unwrap();
    f.publish(0);
    let mut q = f.queue(100);
    q.push_clock(visual, f.microphone, 100).unwrap();
    q.push_clock(f.permit, f.microphone, 100).unwrap();
    f.controller
        .set_camera_permission(at(1), Permission::Denied)
        .unwrap();
    f.admit(1);
    assert_eq!(q.check(at(1), &mut f.guard), Err(Error::StaleOwner));
    assert_eq!(
        q.check_clock_owned(at(1), &mut f.guard).unwrap_err().reason,
        Error::StaleCameraGrant
    );
    assert!(!q.is_retiring_tail());
}

#[test]
fn latched_tail_preserves_camera_dependency_and_exact_original_permit_expiry() {
    for camera in [false, true] {
        let mut f = Fixture::new(0);
        let plan = f
            .controller
            .plan_output(
                at(0),
                f.permit.owner(),
                OutputKind::Speech,
                camera.then_some(f.camera),
            )
            .unwrap();
        f.permit = f.controller.issue_output(at(0), plan, 50).unwrap();
        f.publish(0);
        let mut q = f.queue(240);
        f.admit(1);
        q.check_clock_owned(at(1), &mut f.guard).unwrap();
        let (when, error) = if camera {
            f.controller
                .set_camera_permission(at(2), Permission::Denied)
                .unwrap();
            f.publish(2);
            (2, Error::StaleCameraGrant)
        } else {
            (50, Error::ExpiredPermit)
        };
        assert_eq!(
            q.check_clock_owned(at(when), &mut f.guard)
                .unwrap_err()
                .reason,
            error
        );
    }
}

#[test]
fn privacy_close_reopen_and_new_owner_cannot_rehabilitate_accepted_audio() {
    for already_retiring in [false, true] {
        let mut f = Fixture::new(0);
        let mut q = f.queue(240);
        if already_retiring {
            f.admit(1);
            q.check_clock_owned(at(1), &mut f.guard).unwrap();
        }
        f.controller
            .set_microphone_permission(at(2), Permission::Denied)
            .unwrap();
        f.controller
            .set_microphone_permission(at(3), Permission::Allowed)
            .unwrap();
        f.controller
            .set_capture(at(3), CaptureState::RetainingUntil(at(1_000_000)))
            .unwrap();
        f.admit(3);
        assert_eq!(
            q.check_clock_owned(at(3), &mut f.guard).unwrap_err().reason,
            Error::PrivacyClosed
        );
    }
}

#[test]
fn unknown_privacy_input_loss_state_expiry_and_control_fault_override_retirement() {
    for mode in 0..5 {
        let mut f = Fixture::new(0);
        let mut q = f.queue(240);
        f.admit(1);
        q.check_clock_owned(at(1), &mut f.guard).unwrap();
        let (when, expected) = match mode {
            0 => {
                f.controller
                    .set_microphone_permission(at(2), Permission::Unknown)
                    .unwrap();
                f.publish(2);
                (2, Error::PrivacyClosed)
            }
            1 => {
                f.controller
                    .set_admission(at(2), AdmissionState::Unavailable)
                    .unwrap();
                f.publish(2);
                (2, Error::InputUnavailable)
            }
            2 => {
                let snapshot = f.controller.snapshot(at(2)).unwrap();
                let mut value = serde_json::to_value(snapshot).unwrap();
                value["expires_at"] = serde_json::json!(3);
                f.guard
                    .install(at(2), serde_json::from_value(value).unwrap())
                    .unwrap();
                (3, Error::ExpiredState)
            }
            3 => {
                f.guard.invalidate();
                (2, Error::Faulted)
            }
            _ => (0, Error::ClockRegression),
        };
        assert_eq!(
            q.check_clock_owned(at(when), &mut f.guard)
                .unwrap_err()
                .reason,
            expected
        );
        assert!(q.take_retired_tail().is_none());
    }
}

#[test]
fn mixed_owners_cannot_start_retirement_and_stale_packets_do_not_mutate_newer_ledger() {
    let mut f = Fixture::new(0);
    let mut q = f.queue(120);
    let new = f.admit(1);
    f.controller.input_ended(at(2), new).unwrap();
    let plan = f
        .controller
        .plan_output(at(2), new, OutputKind::Speech, None)
        .unwrap();
    let permit = f.controller.issue_output(at(2), plan, 100_000).unwrap();
    f.publish(2);
    q.push_clock(permit, f.microphone, 240).unwrap();
    f.admit(3);
    assert!(
        q.check_clock_owned(at(3), &mut f.guard)
            .unwrap_err()
            .other_owners
    );
    assert!(!q.is_retiring_tail());
    q.clear().unwrap();
    assert!(q.take_retired_tail().is_none());
    // Strict write checking rejects stale PCM without changing accepted cursors.
    let before = q.queued_range();
    assert_eq!(
        f.guard.check(at(3), permit, OutputKind::Speech),
        Err(Error::StaleOwner)
    );
    assert_eq!(q.queued_range(), before);
}
