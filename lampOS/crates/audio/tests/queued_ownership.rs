use lamp_audio::ownership::{MAX_QUEUED_FRAMES, PlaybackOwnership, QueueError};
use lamp_interaction::{
    AdmissionState, AdmittedInput, BootId, BoundaryGuard, CameraGrant, CaptureState, Controller,
    Error, MonoTime, OutputKind, OutputPermit, Permission, TurnOwner,
};

fn t(us: u64) -> MonoTime {
    MonoTime::from_micros(us)
}

fn fixture() -> (Controller, BoundaryGuard, TurnOwner, CameraGrant) {
    let boot = BootId::new([7; 16]).unwrap();
    let mut controller = Controller::new(boot, t(0));
    controller
        .set_microphone_permission(t(0), Permission::Allowed)
        .unwrap();
    controller
        .set_camera_permission(t(0), Permission::Allowed)
        .unwrap();
    controller
        .set_capture(t(0), CaptureState::RetainingUntil(t(1_000_000)))
        .unwrap();
    controller
        .set_admission(t(0), AdmissionState::OpenUntil(t(1_000_000)))
        .unwrap();
    let owner = controller.admit(t(0), AdmittedInput::NewTurn).unwrap();
    controller.input_ended(t(0), owner).unwrap();
    let camera = controller.camera_grant(t(0)).unwrap();
    (controller, BoundaryGuard::new(boot, t(0)), owner, camera)
}

fn speech(
    controller: &mut Controller,
    owner: TurnOwner,
    camera: Option<CameraGrant>,
    at: u64,
    lease: u64,
) -> OutputPermit {
    let plan = controller
        .plan_output(t(at), owner, OutputKind::Speech, camera)
        .unwrap();
    controller.issue_output(t(at), plan, lease).unwrap()
}

fn publish(controller: &mut Controller, guard: &mut BoundaryGuard, at: u64) {
    guard
        .install(t(at), controller.snapshot(t(at)).unwrap())
        .unwrap();
}

#[test]
fn plain_audio_after_visual_audio_cannot_hide_revoked_camera_authority() {
    let (mut controller, mut guard, owner, camera) = fixture();
    let visual = speech(&mut controller, owner, Some(camera), 0, 100_000);
    let plain = speech(&mut controller, owner, None, 1, 100_000);
    publish(&mut controller, &mut guard, 2);
    let mut queued = PlaybackOwnership::new();
    queued.push(visual, 240).unwrap();
    queued.push(plain, 240).unwrap();
    queued.check(t(2), &mut guard).unwrap();
    controller
        .set_camera_permission(t(3), Permission::Denied)
        .unwrap();
    publish(&mut controller, &mut guard, 3);
    assert_eq!(queued.check(t(3), &mut guard), Err(Error::StaleCameraGrant));
    assert_eq!(queued.pending_frames(), 480);
    // Retiring only the first write removes its camera dependency.
    queued.retire_to(240);
    queued.check(t(4), &mut guard).unwrap();
    assert_eq!(queued.pending_frames(), 240);
}

#[test]
fn every_middle_entry_is_checked_not_only_the_first_or_last() {
    let (mut controller, mut guard, owner, camera) = fixture();
    let plain = speech(&mut controller, owner, None, 0, 100_000);
    let visual = speech(&mut controller, owner, Some(camera), 0, 100_000);
    let mut queued = PlaybackOwnership::new();
    queued.push(plain, 100).unwrap();
    queued.push(visual, 100).unwrap();
    queued.push(plain, 100).unwrap();
    controller
        .set_camera_permission(t(1), Permission::Denied)
        .unwrap();
    publish(&mut controller, &mut guard, 1);
    assert_eq!(queued.check(t(1), &mut guard), Err(Error::StaleCameraGrant));
    queued.retire_to(101);
    assert_eq!(queued.check(t(2), &mut guard), Err(Error::StaleCameraGrant));
    queued.retire_to(100);
    queued.check(t(3), &mut guard).unwrap();
}

#[test]
fn partial_retirement_preserves_the_last_revoked_frame_of_the_front_write() {
    let (mut controller, mut guard, owner, camera) = fixture();
    let visual = speech(&mut controller, owner, Some(camera), 0, 100_000);
    let plain = speech(&mut controller, owner, None, 0, 100_000);
    let mut queued = PlaybackOwnership::new();
    queued.push(visual, 240).unwrap();
    queued.push(plain, 240).unwrap();
    queued.retire_to(470);
    assert_eq!(queued.pending_frames(), 470);
    controller
        .set_camera_permission(t(1), Permission::Denied)
        .unwrap();
    publish(&mut controller, &mut guard, 1);
    assert_eq!(queued.check(t(1), &mut guard), Err(Error::StaleCameraGrant));
    queued.retire_to(241);
    assert_eq!(queued.pending_frames(), 241);
    assert_eq!(queued.check(t(2), &mut guard), Err(Error::StaleCameraGrant));
    queued.retire_to(240);
    queued.check(t(3), &mut guard).unwrap();
}

#[test]
fn a_newer_valid_permit_cannot_extend_an_older_queued_deadline() {
    let (mut controller, mut guard, owner, _) = fixture();
    let old = speech(&mut controller, owner, None, 0, 50);
    let new = speech(&mut controller, owner, None, 1, 100);
    publish(&mut controller, &mut guard, 2);
    let mut queued = PlaybackOwnership::new();
    queued.push(old, 120).unwrap();
    queued.push(new, 240).unwrap();
    queued.check(t(49), &mut guard).unwrap();
    assert_eq!(queued.check(t(50), &mut guard), Err(Error::ExpiredPermit));
    queued.retire_to(240);
    queued.check(t(51), &mut guard).unwrap();
    assert_eq!(queued.check(t(101), &mut guard), Err(Error::ExpiredPermit));
}

#[test]
fn a_larger_reported_delay_never_discards_known_authority_metadata() {
    let (mut controller, mut guard, owner, camera) = fixture();
    let visual = speech(&mut controller, owner, Some(camera), 0, 100_000);
    let mut queued = PlaybackOwnership::new();
    queued.push(visual, 240).unwrap();
    queued.retire_to(480);
    assert_eq!(queued.pending_frames(), 240);
    queued.retire_to(usize::MAX);
    assert_eq!(queued.pending_frames(), 240);
    controller
        .set_camera_permission(t(1), Permission::Denied)
        .unwrap();
    publish(&mut controller, &mut guard, 1);
    assert_eq!(queued.check(t(1), &mut guard), Err(Error::StaleCameraGrant));
}

#[test]
fn invalid_pushes_leave_the_existing_queue_unchanged_and_cannot_overflow_usize() {
    let (mut controller, _, owner, _) = fixture();
    let permit = speech(&mut controller, owner, None, 0, 100_000);
    let mut queued = PlaybackOwnership::default();
    assert_eq!(queued.push(permit, 0), Err(QueueError::EmptyWrite));
    assert!(!queued.has_pending());
    queued.push(permit, MAX_QUEUED_FRAMES).unwrap();
    assert_eq!(queued.push(permit, 1), Err(QueueError::Full));
    assert_eq!(queued.push(permit, usize::MAX), Err(QueueError::Full));
    assert_eq!(queued.pending_frames(), MAX_QUEUED_FRAMES);
    queued.retire_to(480);
    queued.push(permit, 480).unwrap();
    assert_eq!(queued.pending_frames(), MAX_QUEUED_FRAMES);
}

#[test]
fn one_frame_writes_reach_the_bound_without_unbounded_metadata_growth() {
    let (mut controller, _, owner, _) = fixture();
    let permit = speech(&mut controller, owner, None, 0, 100_000);
    let mut queued = PlaybackOwnership::new();
    for _ in 0..MAX_QUEUED_FRAMES {
        queued.push(permit, 1).unwrap();
    }
    assert_eq!(queued.pending_frames(), MAX_QUEUED_FRAMES);
    assert_eq!(queued.push(permit, 1), Err(QueueError::Full));
    for remaining in (0..MAX_QUEUED_FRAMES).rev() {
        queued.retire_to(remaining);
        assert_eq!(queued.pending_frames(), remaining);
    }
    assert!(!queued.has_pending());
    for _ in 0..MAX_QUEUED_FRAMES {
        queued.push(permit, 1).unwrap();
    }
    assert_eq!(queued.pending_frames(), MAX_QUEUED_FRAMES);
}

#[test]
fn zero_delay_retires_all_metadata_and_clear_allows_bounded_reuse() {
    let (mut controller, mut guard, owner, _) = fixture();
    let permit = speech(&mut controller, owner, None, 0, 100_000);
    publish(&mut controller, &mut guard, 1);
    let mut queued = PlaybackOwnership::new();
    queued.push(permit, 240).unwrap();
    queued.push(permit, 240).unwrap();
    queued.retire_to(0);
    assert!(!queued.has_pending());
    assert_eq!(queued.pending_frames(), 0);
    guard.invalidate();
    queued.check(t(2), &mut guard).unwrap();
    queued.push(permit, 960).unwrap();
    assert_eq!(queued.check(t(3), &mut guard), Err(Error::Faulted));
    queued.clear().unwrap();
    assert_eq!(queued.pending_frames(), 0);
    assert!(!queued.has_pending());
    queued.push(permit, 960).unwrap();
}

#[test]
fn non_speech_permission_is_never_accepted_as_pcm_authority() {
    let (mut controller, _, owner, _) = fixture();
    let plan = controller
        .plan_output(t(0), owner, OutputKind::Light, None)
        .unwrap();
    let light = controller.issue_output(t(0), plan, 100_000).unwrap();
    let mut queued = PlaybackOwnership::new();
    assert_eq!(queued.push(light, 240), Err(QueueError::NotSpeech));
    assert!(!queued.has_pending());
}

#[test]
fn new_turn_metadata_cannot_mask_old_turn_audio_that_was_not_flushed() {
    let (mut controller, mut guard, old_owner, _) = fixture();
    let old = speech(&mut controller, old_owner, None, 0, 100_000);
    let new_owner = controller.admit(t(1), AdmittedInput::Interruption).unwrap();
    controller.input_ended(t(1), new_owner).unwrap();
    let new = speech(&mut controller, new_owner, None, 1, 100_000);
    publish(&mut controller, &mut guard, 2);
    let mut queued = PlaybackOwnership::new();
    queued.push(old, 240).unwrap();
    queued.push(new, 240).unwrap();
    assert_eq!(queued.check(t(2), &mut guard), Err(Error::StaleOwner));
    queued.retire_to(240);
    queued.check(t(3), &mut guard).unwrap();
}

#[test]
fn consumption_between_poll_and_write_cannot_overrun_ownership_credit() {
    let (mut controller, mut guard, owner, camera) = fixture();
    let visual = speech(&mut controller, owner, Some(camera), 0, 100_000);
    let plain = speech(&mut controller, owner, None, 0, 100_000);
    let mut queued = PlaybackOwnership::new();
    queued.push(visual, 240).unwrap();
    queued.push(plain, 660).unwrap();

    // The poll sees 900 outstanding frames and 60 writable frames. The device
    // consumes another 180 before writei, so it could now accept a full 240.
    // Previously that accepted write overflowed metadata before the next poll.
    queued.retire_to(900);
    assert_eq!(queued.push(plain, 240), Err(QueueError::Full));
    let offered = queued.writable_frames(240, 60);
    assert_eq!(offered, 60);
    queued.push(plain, offered).unwrap();
    assert_eq!(queued.pending_frames(), MAX_QUEUED_FRAMES);

    // A fresh delay then proves those 180 frames were consumed. Partial writes
    // retain the old camera dependency until its very last frame leaves.
    queued.retire_to(780);
    controller
        .set_camera_permission(t(1), Permission::Denied)
        .unwrap();
    publish(&mut controller, &mut guard, 1);
    assert_eq!(queued.check(t(1), &mut guard), Err(Error::StaleCameraGrant));
    queued.retire_to(721);
    assert_eq!(queued.check(t(2), &mut guard), Err(Error::StaleCameraGrant));
    queued.retire_to(720);
    queued.check(t(3), &mut guard).unwrap();
    let offered = queued.writable_frames(240, 240);
    assert_eq!(offered, 240);
    queued.push(plain, offered).unwrap();
    assert_eq!(queued.pending_frames(), MAX_QUEUED_FRAMES);
}

#[test]
fn downstream_delay_with_writable_ring_space_preserves_old_authority() {
    let (mut controller, mut guard, owner, camera) = fixture();
    let visual = speech(&mut controller, owner, Some(camera), 0, 100_000);
    let plain = speech(&mut controller, owner, None, 0, 100_000);
    let mut queued = PlaybackOwnership::new();
    queued.push(visual, 240).unwrap();
    queued.push(plain, 720).unwrap();

    // Available frames and total I/O delay are not complements: the driver can
    // have space while older frames remain downstream. Space is not retirement.
    queued.retire_to(1_200);
    assert_eq!(queued.writable_frames(240, 480), 0);
    assert_eq!(queued.pending_frames(), MAX_QUEUED_FRAMES);
    controller
        .set_camera_permission(t(1), Permission::Denied)
        .unwrap();
    publish(&mut controller, &mut guard, 1);
    assert_eq!(queued.check(t(1), &mut guard), Err(Error::StaleCameraGrant));

    queued.retire_to(959);
    assert_eq!(queued.writable_frames(240, 480), 1);
    queued.push(plain, 1).unwrap();
    assert_eq!(queued.check(t(2), &mut guard), Err(Error::StaleCameraGrant));
    queued.retire_to(721);
    queued.check(t(3), &mut guard).unwrap();
}

#[test]
fn bounded_writes_progress_under_repeated_poll_write_consumption_races() {
    let (mut controller, _, owner, _) = fixture();
    let permit = speech(&mut controller, owner, None, 0, 100_000);
    let mut queued = PlaybackOwnership::new();
    let mut device_queued: usize = 0;
    let mut accepted = 0;
    let mut partial_writes = 0;
    let mut blocked = 0;
    for tick in 0..512 {
        queued.retire_to(device_queued);
        let available_at_poll = MAX_QUEUED_FRAMES - device_queued;
        // Irregular hardware progress after the poll used to let writei accept
        // more than the stale ledger could represent. Include stalled ticks.
        device_queued = device_queued.saturating_sub([0, 17, 0, 119, 0, 31][tick % 6]);
        let offered = queued.writable_frames(240, available_at_poll);
        if offered == 0 {
            blocked += 1;
        } else {
            assert!(offered <= MAX_QUEUED_FRAMES - device_queued);
            queued.push(permit, offered).unwrap();
            device_queued += offered;
            accepted += offered;
            partial_writes += usize::from(offered < 240);
        }
        assert!(queued.pending_frames() <= MAX_QUEUED_FRAMES);
        assert!(queued.pending_frames() >= device_queued);
        queued.retire_to(device_queued);
    }
    assert!(accepted > MAX_QUEUED_FRAMES * 10);
    assert!(partial_writes > 0);
    assert!(blocked > 0);
    queued.retire_to(0);
    assert!(!queued.has_pending());
    assert_eq!(queued.writable_frames(240, 0), 0);
    assert_eq!(queued.writable_frames(0, usize::MAX), 0);
    assert_eq!(
        queued.writable_frames(usize::MAX, usize::MAX),
        MAX_QUEUED_FRAMES
    );
}

#[test]
fn discard_evidence_keeps_failed_owner_and_reports_any_other_queued_owner() {
    let (mut controller, mut guard, old_owner, _) = fixture();
    let old = speech(&mut controller, old_owner, None, 0, 100_000);
    let new_owner = controller.admit(t(1), AdmittedInput::Interruption).unwrap();
    controller.input_ended(t(1), new_owner).unwrap();
    let new = speech(&mut controller, new_owner, None, 1, 100_000);
    publish(&mut controller, &mut guard, 2);
    let mut queued = PlaybackOwnership::new();
    queued.push(old, 120).unwrap();
    let failure = queued.check_owned(t(2), &mut guard).unwrap_err();
    assert_eq!(failure.owner, old_owner);
    assert_eq!(failure.reason, Error::StaleOwner);
    assert_eq!(failure.queued_frames, 120);
    assert!(!failure.other_owners);
    queued.push(new, 240).unwrap();
    let failure = queued.check_owned(t(3), &mut guard).unwrap_err();
    assert_eq!(failure.owner, old_owner);
    assert_eq!(failure.queued_frames, 360);
    assert!(failure.other_owners);
    assert_eq!(
        queued.pending_frames(),
        360,
        "reporting is not physical discard"
    );
}

#[test]
fn silence_and_speech_share_one_bound_and_only_normal_retirement_completes_a_cursor() {
    let (mut controller, mut guard, owner, _) = fixture();
    let permit = speech(&mut controller, owner, None, 0, 100_000);
    publish(&mut controller, &mut guard, 0);
    let zero = guard.zero_authority(t(0)).unwrap();
    let mut queued = PlaybackOwnership::new();
    queued.push_silence(zero, 240).unwrap();
    queued.push(permit, 240).unwrap();
    let final_speech = queued.accepted_cursor();
    queued.push_silence(zero, 480).unwrap();
    assert_eq!(queued.pending_frames(), MAX_QUEUED_FRAMES);
    assert_eq!(queued.push_silence(zero, 1), Err(QueueError::Full));
    assert_eq!(queued.pending_speech_frames(), 240);
    queued.retire_to(481);
    assert!(!queued.is_retired(final_speech));
    queued.retire_to(480);
    assert!(queued.is_retired(final_speech));
    assert_eq!(queued.pending_speech_frames(), 0);
    assert!(
        queued.has_pending(),
        "continuous zeros do not prevent speech completion"
    );
    let discarded = queued.clear().unwrap();
    assert_eq!(discarded.retired_through, final_speech.frame);
    assert_eq!(discarded.accepted_through, 960);
    queued.push_silence(zero, 960).unwrap();
    queued.retire_to(0);
    assert!(
        !queued.is_retired(final_speech),
        "a reset cannot complete an old cursor"
    );
}

#[test]
fn zero_frames_cannot_mask_revoked_speech_and_privacy_is_checked_without_a_turn() {
    let (mut controller, mut guard, owner, _) = fixture();
    let permit = speech(&mut controller, owner, None, 0, 100_000);
    publish(&mut controller, &mut guard, 0);
    let zero = guard.zero_authority(t(0)).unwrap();
    let mut queued = PlaybackOwnership::new();
    queued.push_silence(zero, 240).unwrap();
    queued.push(permit, 240).unwrap();
    queued.push_silence(zero, 240).unwrap();
    controller.cancel_turn(t(1), owner).unwrap();
    publish(&mut controller, &mut guard, 1);
    let loss = queued.check_owned(t(1), &mut guard).unwrap_err();
    assert_eq!(loss.owner, owner);
    assert_eq!(loss.reason, Error::StaleOwner);
    assert!(!loss.other_owners, "silence does not invent a speech owner");
    assert_eq!(
        loss.range.accepted_through - loss.range.retired_through,
        720
    );
    queued.check_silence(t(1), &mut guard).unwrap();
    queued.clear().unwrap();
    queued.push_silence(zero, 240).unwrap();
    controller
        .set_microphone_permission(t(2), Permission::Denied)
        .unwrap();
    publish(&mut controller, &mut guard, 2);
    assert_eq!(queued.check(t(2), &mut guard), Err(Error::PrivacyClosed));
}
