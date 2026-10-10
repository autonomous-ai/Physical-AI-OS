use lamp_interaction::{
    AdmissionState, AdmittedInput, BootId, BoundaryGuard, CameraGrant, CaptureState, Controller,
    Error, MonoTime, OutputKind, OutputPermit, Permission, Snapshot, TurnOwner,
};
use serde_json::{Value, json};

fn t(us: u64) -> MonoTime {
    MonoTime::from_micros(us)
}

fn boot(value: u8) -> BootId {
    BootId::new([value; 16]).unwrap()
}

fn ready(id: u8) -> Controller {
    let mut controller = Controller::new(boot(id), t(0));
    controller
        .set_microphone_permission(t(0), Permission::Allowed)
        .unwrap();
    controller
        .set_capture(t(0), CaptureState::RetainingUntil(t(1_000_000)))
        .unwrap();
    controller
        .set_admission(t(0), AdmissionState::OpenUntil(t(1_000_000)))
        .unwrap();
    controller
}

fn answered_input(controller: &mut Controller, now: u64) -> TurnOwner {
    let owner = controller.admit(t(now), AdmittedInput::NewTurn).unwrap();
    controller.input_ended(t(now), owner).unwrap();
    owner
}

fn permit(
    controller: &mut Controller,
    now: u64,
    owner: TurnOwner,
    kind: OutputKind,
) -> OutputPermit {
    issue(controller, t(now), owner, kind, None, 100_000).unwrap()
}

fn issue(
    controller: &mut Controller,
    now: MonoTime,
    owner: TurnOwner,
    kind: OutputKind,
    camera: Option<CameraGrant>,
    lease_us: u64,
) -> Result<OutputPermit, Error> {
    let output = controller.plan_output(now, owner, kind, camera)?;
    controller.issue_output(now, output, lease_us)
}

fn install(controller: &mut Controller, guard: &mut BoundaryGuard, now: u64) -> Snapshot {
    let state = controller.snapshot(t(now)).unwrap();
    guard.install(t(now), state).unwrap();
    state
}

#[test]
fn startup_and_privacy_reopening_require_real_capture_retention_and_admission() {
    let mut controller = Controller::new(boot(1), t(0));
    let state = controller.snapshot(t(0)).unwrap();
    assert_eq!(state.microphone_permission(), Permission::Unknown);
    assert_eq!(state.camera_permission(), Permission::Unknown);
    assert!(!state.listening_ready(t(0)));
    assert_eq!(
        controller.admit(t(0), AdmittedInput::NewTurn),
        Err(Error::InputUnavailable)
    );

    controller
        .set_microphone_permission(t(1), Permission::Allowed)
        .unwrap();
    controller
        .set_admission(t(1), AdmissionState::OpenUntil(t(100_000)))
        .unwrap();
    assert!(!controller.snapshot(t(1)).unwrap().listening_ready(t(1)));
    controller
        .set_capture(t(2), CaptureState::RetainingUntil(t(100_000)))
        .unwrap();
    let state = controller.snapshot(t(2)).unwrap();
    assert!(state.listening_ready(t(2)));
    assert_eq!(state.owner(), None);
    assert!(!state.input_active());
    assert_eq!(state.playback(), None);
    // Readiness creates no social turn, speech, or mandatory gesture.
    assert!(!state.camera_allowed(t(2)));
}

#[test]
fn speech_and_capture_overlap_without_a_busy_state_dropping_the_next_input() {
    let mut controller = ready(1);
    let owner = answered_input(&mut controller, 10);
    let speech = permit(&mut controller, 11, owner, OutputKind::Speech);
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    install(&mut controller, &mut guard, 12);
    guard.check(t(13), speech, OutputKind::Speech).unwrap();
    let playback = controller.playback_started(t(13), speech).unwrap();
    let state = install(&mut controller, &mut guard, 14);
    assert!(state.listening_ready(t(14)));
    assert_eq!(state.playback(), Some(playback));
    assert!(!state.input_active());

    let next = controller
        .admit(t(15), AdmittedInput::Interruption)
        .unwrap();
    let state = install(&mut controller, &mut guard, 15);
    assert!(state.listening_ready(t(15)));
    assert!(state.input_active());
    assert_eq!(state.playback(), None);
    assert_eq!(state.owner(), Some(next));
    assert_eq!(
        guard.check(t(15), speech, OutputKind::Speech),
        Err(Error::StaleOwner)
    );
    assert_eq!(
        controller.playback_ended(t(15), playback),
        Err(Error::StalePlayback)
    );
}

#[test]
fn new_turn_revokes_every_output_class_and_late_callbacks_cannot_touch_it() {
    let mut controller = ready(1);
    let old = answered_input(&mut controller, 10);
    let pending = [OutputKind::Speech, OutputKind::Light, OutputKind::Motion]
        .map(|kind| permit(&mut controller, 10, old, kind));
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    install(&mut controller, &mut guard, 11);
    let next = controller
        .admit(t(12), AdmittedInput::Interruption)
        .unwrap();
    install(&mut controller, &mut guard, 13);
    for output in pending {
        assert_eq!(
            guard.check(t(14), output, output.kind()),
            Err(Error::StaleOwner)
        );
    }
    assert_eq!(controller.cancel_turn(t(14), old), Err(Error::StaleOwner));
    assert_eq!(controller.input_ended(t(14), old), Err(Error::StaleOwner));
    assert_eq!(controller.complete_turn(t(14), old), Err(Error::StaleOwner));
    assert_eq!(
        issue(
            &mut controller,
            t(14),
            old,
            OutputKind::Speech,
            None,
            10_000
        ),
        Err(Error::StaleOwner)
    );
    assert_eq!(
        issue(
            &mut controller,
            t(14),
            next,
            OutputKind::Speech,
            None,
            10_000
        ),
        Err(Error::InputStillActive)
    );
    let cue = permit(&mut controller, 14, next, OutputKind::Light);
    let state = install(&mut controller, &mut guard, 15);
    guard.check(t(15), cue, OutputKind::Light).unwrap();
    assert_eq!(state.owner(), Some(next));
    assert!(state.input_active());
}

#[test]
fn capture_heartbeats_do_not_interrupt_speech_or_invent_a_turn() {
    let mut controller = ready(1);
    let owner = answered_input(&mut controller, 10);
    let speech = permit(&mut controller, 10, owner, OutputKind::Speech);
    let playback = controller.playback_started(t(10), speech).unwrap();
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    install(&mut controller, &mut guard, 11);
    controller
        .set_capture(t(12), CaptureState::RetainingUntil(t(1_000_012)))
        .unwrap();
    let state = install(&mut controller, &mut guard, 13);
    assert_eq!(state.owner(), Some(owner));
    assert_eq!(state.playback(), Some(playback));
    guard.check(t(13), speech, OutputKind::Speech).unwrap();
}

#[test]
fn cancellation_and_completion_are_permanent_even_when_input_remains_ready() {
    for complete in [false, true] {
        let mut controller = ready(1);
        let owner = answered_input(&mut controller, 1);
        let speech = permit(&mut controller, 1, owner, OutputKind::Speech);
        let mut guard = BoundaryGuard::new(boot(1), t(0));
        install(&mut controller, &mut guard, 2);
        if complete {
            controller.complete_turn(t(3), owner).unwrap();
        } else {
            controller.cancel_turn(t(3), owner).unwrap();
        }
        let state = install(&mut controller, &mut guard, 4);
        assert!(state.listening_ready(t(4)));
        assert_eq!(state.owner(), None);
        assert_eq!(
            guard.check(t(4), speech, OutputKind::Speech),
            Err(Error::StaleOwner)
        );
        assert_eq!(
            issue(
                &mut controller,
                t(5),
                owner,
                OutputKind::Speech,
                None,
                100_000
            ),
            Err(Error::StaleOwner)
        );
    }
}

#[test]
fn delayed_or_duplicate_snapshots_cannot_restore_an_old_owner() {
    let mut controller = ready(1);
    let old = answered_input(&mut controller, 1);
    let speech = permit(&mut controller, 1, old, OutputKind::Speech);
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    let previous = install(&mut controller, &mut guard, 2);
    let next = controller.admit(t(3), AdmittedInput::Interruption).unwrap();
    let cue = permit(&mut controller, 3, next, OutputKind::Light);
    let current = install(&mut controller, &mut guard, 4);
    assert_eq!(guard.install(t(5), previous), Err(Error::StaleSnapshot));
    assert_eq!(guard.install(t(5), current), Err(Error::StaleSnapshot));
    assert_eq!(guard.state(), Some(current));
    assert!(!guard.is_faulted());
    assert_eq!(
        guard.check(t(5), speech, OutputKind::Speech),
        Err(Error::StaleOwner)
    );
    guard.check(t(5), cue, OutputKind::Light).unwrap();
}

#[test]
fn restart_requires_a_new_boot_and_never_auto_rebinds_output_authority() {
    let mut old = ready(1);
    let old_owner = answered_input(&mut old, 1);
    let old_permit = permit(&mut old, 1, old_owner, OutputKind::Speech);
    let mut new = ready(2);
    let new_owner = answered_input(&mut new, 1);
    let new_permit = permit(&mut new, 1, new_owner, OutputKind::Speech);
    assert_eq!(old_owner.turn(), new_owner.turn());
    assert_eq!(old_owner.generation(), new_owner.generation());
    assert_ne!(old_owner, new_owner);
    let old_state = old.snapshot(t(2)).unwrap();
    let new_state = new.snapshot(t(2)).unwrap();
    let mut old_guard = BoundaryGuard::new(boot(1), t(0));
    old_guard.install(t(2), old_state).unwrap();
    assert_eq!(old_guard.install(t(2), new_state), Err(Error::WrongBoot));
    let mut new_guard = BoundaryGuard::new(boot(2), t(0));
    new_guard.install(t(2), new_state).unwrap();
    assert_eq!(
        new_guard.check(t(3), old_permit, OutputKind::Speech),
        Err(Error::WrongBoot)
    );
    assert_eq!(new_guard.install(t(3), old_state), Err(Error::WrongBoot));
    new_guard
        .check(t(3), new_permit, OutputKind::Speech)
        .unwrap();
}

#[test]
fn final_boundary_rejects_wrong_actuator_future_revision_and_exact_expiry() {
    let mut controller = ready(1);
    let owner = answered_input(&mut controller, 10);
    let speech = issue(&mut controller, t(10), owner, OutputKind::Speech, None, 100).unwrap();
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    assert_eq!(
        guard.check(t(10), speech, OutputKind::Speech),
        Err(Error::NoAuthority)
    );
    install(&mut controller, &mut guard, 11);
    assert_eq!(
        guard.check(t(12), speech, OutputKind::Motion),
        Err(Error::WrongOutputKind)
    );
    let mut wire = serde_json::to_value(speech).unwrap();
    wire["revision"] = json!(u64::MAX);
    let requires_future: OutputPermit = serde_json::from_value(wire).unwrap();
    assert_eq!(
        guard.check(t(12), requires_future, OutputKind::Speech),
        Err(Error::StateTooOld)
    );
    guard.check(t(109), speech, OutputKind::Speech).unwrap();
    assert_eq!(
        guard.check(t(110), speech, OutputKind::Speech),
        Err(Error::ExpiredPermit)
    );
    assert_eq!(
        guard.check(t(111), speech, OutputKind::Speech),
        Err(Error::ExpiredPermit)
    );
}

#[test]
fn an_output_permit_cannot_outlive_a_stalled_control_path() {
    let mut controller = ready(1);
    let owner = answered_input(&mut controller, 10);
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    let state = install(&mut controller, &mut guard, 20);
    let later = permit(&mut controller, 200_000, owner, OutputKind::Speech);
    assert!(later.expires_at() > state.expires_at());
    guard.check(t(250_019), later, OutputKind::Speech).unwrap();
    assert_eq!(
        guard.check(t(250_020), later, OutputKind::Speech),
        Err(Error::ExpiredState)
    );
}

#[test]
fn capture_expiry_revokes_the_turn_permanently_but_keeps_camera_permission() {
    let mut controller = ready(1);
    controller
        .set_camera_permission(t(1), Permission::Allowed)
        .unwrap();
    let camera = controller.camera_grant(t(1)).unwrap();
    controller
        .set_capture(t(1), CaptureState::RetainingUntil(t(100)))
        .unwrap();
    let owner = answered_input(&mut controller, 2);
    let speech = permit(&mut controller, 3, owner, OutputKind::Speech);
    assert_eq!(speech.expires_at(), t(100));
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    let state = install(&mut controller, &mut guard, 4);
    assert_eq!(state.expires_at(), t(100));
    assert!(!state.listening_ready(t(100)));
    assert_eq!(
        guard.check(t(100), speech, OutputKind::Speech),
        Err(Error::ExpiredState)
    );
    // A heartbeat arriving at the deadline is too late to save the old turn.
    controller
        .set_capture(t(100), CaptureState::RetainingUntil(t(200_000)))
        .unwrap();
    let state = install(&mut controller, &mut guard, 101);
    assert!(state.listening_ready(t(101)));
    assert_eq!(state.owner(), None);
    assert_eq!(state.camera_permission(), Permission::Allowed);
    assert_eq!(controller.camera_grant(t(101)).unwrap(), camera);
    assert_eq!(
        guard.check(t(101), speech, OutputKind::Speech),
        Err(Error::ExpiredPermit)
    );
    assert_eq!(
        issue(
            &mut controller,
            t(102),
            owner,
            OutputKind::Speech,
            None,
            1_000
        ),
        Err(Error::StaleOwner)
    );
}

#[test]
fn invalid_or_closed_admission_and_capture_fail_closed_without_altering_camera() {
    for close_capture in [false, true] {
        let mut controller = ready(1);
        controller
            .set_camera_permission(t(1), Permission::Allowed)
            .unwrap();
        let owner = answered_input(&mut controller, 1);
        let result = if close_capture {
            controller.set_capture(t(2), CaptureState::RetainingUntil(t(1_000_003)))
        } else {
            controller.set_admission(t(2), AdmissionState::OpenUntil(t(2)))
        };
        assert_eq!(result, Err(Error::InvalidLease));
        let state = controller.snapshot(t(3)).unwrap();
        assert_eq!(state.owner(), None);
        assert!(!state.listening_ready(t(3)));
        assert!(state.camera_allowed(t(3)));
        assert_eq!(controller.input_ended(t(3), owner), Err(Error::StaleOwner));
    }
}

#[test]
fn microphone_unknown_denied_and_reopened_never_revive_old_output() {
    for privacy in [Permission::Denied, Permission::Unknown] {
        let mut controller = ready(1);
        controller
            .set_camera_permission(t(1), Permission::Allowed)
            .unwrap();
        let owner = answered_input(&mut controller, 1);
        let speech = permit(&mut controller, 1, owner, OutputKind::Speech);
        controller.set_microphone_permission(t(2), privacy).unwrap();
        let state = controller.snapshot(t(3)).unwrap();
        assert_eq!(state.microphone_permission(), privacy);
        assert!(state.camera_allowed(t(3)));
        assert!(!state.listening_ready(t(3)));
        assert_eq!(
            controller.set_capture(t(4), CaptureState::RetainingUntil(t(100_000))),
            Err(Error::PrivacyClosed)
        );
        controller
            .set_microphone_permission(t(5), Permission::Allowed)
            .unwrap();
        assert!(!controller.snapshot(t(5)).unwrap().listening_ready(t(5)));
        controller
            .set_capture(t(6), CaptureState::RetainingUntil(t(100_000)))
            .unwrap();
        let mut guard = BoundaryGuard::new(boot(1), t(0));
        let state = install(&mut controller, &mut guard, 7);
        assert!(state.listening_ready(t(7)));
        assert_eq!(
            guard.check(t(7), speech, OutputKind::Speech),
            Err(Error::StaleOwner)
        );
    }
}

#[test]
fn camera_privacy_only_revokes_camera_derived_work_and_reopening_needs_a_new_grant() {
    let mut controller = ready(1);
    controller
        .set_camera_permission(t(1), Permission::Allowed)
        .unwrap();
    let camera = controller.camera_grant(t(1)).unwrap();
    let owner = answered_input(&mut controller, 1);
    let visual = issue(
        &mut controller,
        t(2),
        owner,
        OutputKind::Speech,
        Some(camera),
        100_000,
    )
    .unwrap();
    let ordinary = permit(&mut controller, 2, owner, OutputKind::Speech);
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    install(&mut controller, &mut guard, 3);
    guard.check(t(3), visual, OutputKind::Speech).unwrap();
    controller
        .set_camera_permission(t(4), Permission::Denied)
        .unwrap();
    let state = install(&mut controller, &mut guard, 5);
    assert!(state.listening_ready(t(5)));
    assert_eq!(state.owner(), Some(owner));
    assert_eq!(
        guard.check(t(5), visual, OutputKind::Speech),
        Err(Error::StaleCameraGrant)
    );
    guard.check(t(5), ordinary, OutputKind::Speech).unwrap();
    controller
        .set_camera_permission(t(6), Permission::Allowed)
        .unwrap();
    install(&mut controller, &mut guard, 7);
    assert_eq!(
        guard.check(t(7), visual, OutputKind::Speech),
        Err(Error::StaleCameraGrant)
    );
    assert_eq!(
        issue(
            &mut controller,
            t(7),
            owner,
            OutputKind::Speech,
            Some(camera),
            100_000
        ),
        Err(Error::StaleCameraGrant)
    );
    let new_camera = controller.camera_grant(t(8)).unwrap();
    assert_ne!(camera, new_camera);
    let fresh = issue(
        &mut controller,
        t(8),
        owner,
        OutputKind::Speech,
        Some(new_camera),
        100_000,
    )
    .unwrap();
    install(&mut controller, &mut guard, 9);
    guard.check(t(9), fresh, OutputKind::Speech).unwrap();
}

#[test]
fn same_turn_delayed_listening_and_speaking_cues_cannot_override_the_new_phase() {
    let mut controller = ready(1);
    let owner = controller.admit(t(1), AdmittedInput::NewTurn).unwrap();
    let listening = permit(&mut controller, 1, owner, OutputKind::Light);
    let nod = permit(&mut controller, 1, owner, OutputKind::Motion);
    controller.input_ended(t(2), owner).unwrap();
    let awaiting = permit(&mut controller, 2, owner, OutputKind::Light);
    let speech = permit(&mut controller, 2, owner, OutputKind::Speech);
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    install(&mut controller, &mut guard, 3);
    assert_eq!(
        guard.check(t(3), listening, OutputKind::Light),
        Err(Error::StalePresentation)
    );
    assert_eq!(
        guard.check(t(3), nod, OutputKind::Motion),
        Err(Error::StalePresentation)
    );
    guard.check(t(3), awaiting, OutputKind::Light).unwrap();
    let playback = controller.playback_started(t(4), speech).unwrap();
    let speaking = permit(&mut controller, 4, owner, OutputKind::Light);
    install(&mut controller, &mut guard, 5);
    assert_eq!(
        guard.check(t(5), awaiting, OutputKind::Light),
        Err(Error::StalePresentation)
    );
    guard.check(t(5), speech, OutputKind::Speech).unwrap();
    guard.check(t(5), speaking, OutputKind::Light).unwrap();
    controller.playback_ended(t(6), playback).unwrap();
    install(&mut controller, &mut guard, 7);
    assert_eq!(
        guard.check(t(7), speaking, OutputKind::Light),
        Err(Error::StalePresentation)
    );
}

#[test]
fn a_delayed_playback_end_cannot_clear_a_new_occurrence_in_the_same_turn() {
    let mut controller = ready(1);
    let owner = answered_input(&mut controller, 1);
    let speech = permit(&mut controller, 1, owner, OutputKind::Speech);
    let first = controller.playback_started(t(2), speech).unwrap();
    assert_eq!(
        controller.playback_started(t(2), speech),
        Err(Error::AlreadyPlaying)
    );
    controller.playback_ended(t(3), first).unwrap();
    let second = controller.playback_started(t(4), speech).unwrap();
    assert_ne!(first, second);
    assert_eq!(
        controller.playback_ended(t(5), first),
        Err(Error::StalePlayback)
    );
    assert_eq!(controller.snapshot(t(6)).unwrap().playback(), Some(second));
}

#[test]
fn regressing_receipt_time_faults_authority_instead_of_rejuvenating_old_permits() {
    let mut controller = ready(1);
    let owner = answered_input(&mut controller, 100);
    let speech = permit(&mut controller, 100, owner, OutputKind::Speech);
    let state = controller.snapshot(t(101)).unwrap();
    assert_eq!(
        controller.input_ended(t(100), owner),
        Err(Error::ClockRegression)
    );
    assert!(controller.is_faulted());
    assert_eq!(controller.snapshot(t(102)), Err(Error::Faulted));
    assert_eq!(controller.camera_grant(t(102)), Err(Error::Faulted));
    let mut guard = BoundaryGuard::new(boot(1), t(101));
    guard.install(t(101), state).unwrap();
    guard.check(t(102), speech, OutputKind::Speech).unwrap();
    assert_eq!(
        guard.check(t(101), speech, OutputKind::Speech),
        Err(Error::ClockRegression)
    );
    assert!(guard.is_faulted());
    assert_eq!(guard.install(t(103), state), Err(Error::Faulted));
    assert_eq!(
        guard.check(t(103), speech, OutputKind::Speech),
        Err(Error::Faulted)
    );
}

#[test]
fn strict_wire_shapes_reject_unknown_states_fields_zero_ids_and_bad_permits() {
    let mut controller = ready(1);
    let owner = answered_input(&mut controller, 1);
    let speech = permit(&mut controller, 1, owner, OutputKind::Speech);
    let state = controller.snapshot(t(2)).unwrap();
    let valid = serde_json::to_value(state).unwrap();
    let mut unknown_enum = valid.clone();
    unknown_enum["microphone"] = json!("probably_ready");
    assert!(serde_json::from_value::<Snapshot>(unknown_enum).is_err());
    let mut unknown_field = valid.clone();
    unknown_field["ignore_privacy"] = json!(true);
    assert!(serde_json::from_value::<Snapshot>(unknown_field).is_err());
    let mut zero_boot = valid;
    zero_boot["boot"] = serde_json::to_value([0_u8; 16]).unwrap();
    assert!(serde_json::from_value::<Snapshot>(zero_boot).is_err());
    let mut zero_turn = serde_json::to_value(speech).unwrap();
    zero_turn["owner"]["turn"] = json!(0);
    assert!(serde_json::from_value::<OutputPermit>(zero_turn).is_err());
    let mut bad_permit = serde_json::to_value(speech).unwrap();
    bad_permit["expires_at"] = bad_permit["issued_at"].clone();
    let bad_permit = serde_json::from_value(bad_permit).unwrap();
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    guard.install(t(2), state).unwrap();
    assert_eq!(
        guard.check(t(3), bad_permit, OutputKind::Speech),
        Err(Error::MalformedState)
    );
    // The parser calls this if a trusted control packet could not be decoded.
    guard.invalidate();
    assert_eq!(
        guard.check(t(4), speech, OutputKind::Speech),
        Err(Error::Faulted)
    );
}

#[test]
fn malformed_known_state_is_not_authority_even_if_it_deserializes() {
    let mut controller = ready(1);
    let owner = answered_input(&mut controller, 1);
    let speech = permit(&mut controller, 1, owner, OutputKind::Speech);
    let state = controller.snapshot(t(2)).unwrap();
    let mut malformed = serde_json::to_value(state).unwrap();
    malformed["microphone"] = json!("denied");
    let malformed: Snapshot = serde_json::from_value(malformed).unwrap();
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    guard.install(t(2), state).unwrap();
    assert_eq!(guard.install(t(3), malformed), Err(Error::MalformedState));
    assert!(guard.is_faulted());
    assert_eq!(
        guard.check(t(3), speech, OutputKind::Speech),
        Err(Error::Faulted)
    );
}

#[test]
fn a_higher_packet_revision_cannot_hide_regressing_generation_or_phase() {
    let mut controller = ready(1);
    let first = answered_input(&mut controller, 1);
    let old_state = controller.snapshot(t(2)).unwrap();
    controller.cancel_turn(t(3), first).unwrap();
    answered_input(&mut controller, 4);
    let new_state = controller.snapshot(t(5)).unwrap();
    let mut older = serde_json::to_value(old_state).unwrap();
    older["revision"] = json!(new_state.revision() + 1);
    older["issued_at"] = json!(5);
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    guard.install(t(5), new_state).unwrap();
    assert_eq!(
        guard.install(t(6), serde_json::from_value(older).unwrap()),
        Err(Error::StateRegression)
    );
    assert!(guard.is_faulted());
}

#[test]
fn camera_privacy_lineage_cannot_change_without_a_new_generation() {
    let mut controller = ready(1);
    let state = controller.snapshot(t(1)).unwrap();
    let mut fake = serde_json::to_value(state).unwrap();
    fake["revision"] = json!(state.revision() + 1);
    fake["camera"] = json!("allowed");
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    guard.install(t(1), state).unwrap();
    assert_eq!(
        guard.install(t(2), serde_json::from_value(fake).unwrap()),
        Err(Error::StateRegression)
    );
}

#[test]
fn permits_and_snapshots_round_trip_without_exposing_any_mutable_authority() {
    let mut controller = ready(1);
    let owner = answered_input(&mut controller, 1);
    let speech = permit(&mut controller, 1, owner, OutputKind::Speech);
    let state = controller.snapshot(t(2)).unwrap();
    let restored_permit = serde_json::from_slice(&serde_json::to_vec(&speech).unwrap()).unwrap();
    let restored_state = serde_json::from_slice(&serde_json::to_vec(&state).unwrap()).unwrap();
    assert_eq!(speech, restored_permit);
    assert_eq!(state, restored_state);
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    guard.install(t(2), restored_state).unwrap();
    guard
        .check(t(3), restored_permit, OutputKind::Speech)
        .unwrap();
}

#[test]
fn repeated_interruption_and_stale_delivery_do_not_resurrect_any_previous_turn() {
    let mut controller = ready(1);
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    let mut old_packets = std::collections::VecDeque::with_capacity(32);
    for turn in 0..1_000_u64 {
        let now = turn * 10 + 1;
        let owner = controller
            .admit(t(now), AdmittedInput::Interruption)
            .unwrap();
        controller.input_ended(t(now), owner).unwrap();
        let speech = permit(&mut controller, now, owner, OutputKind::Speech);
        install(&mut controller, &mut guard, now + 1);
        for &old in &old_packets {
            assert_eq!(
                guard.check(t(now + 1), old, OutputKind::Speech),
                Err(Error::StaleOwner)
            );
        }
        guard.check(t(now + 1), speech, OutputKind::Speech).unwrap();
        if old_packets.len() == 32 {
            old_packets.pop_front();
        }
        old_packets.push_back(speech);
    }
}

#[test]
fn future_state_and_future_output_cannot_be_executed_early() {
    let mut controller = ready(1);
    let owner = answered_input(&mut controller, 100);
    let speech = permit(&mut controller, 100, owner, OutputKind::Speech);
    let state = controller.snapshot(t(101)).unwrap();
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    assert_eq!(guard.install(t(99), state), Err(Error::FutureState));
    guard.install(t(101), state).unwrap();
    let mut future: Value = serde_json::to_value(speech).unwrap();
    future["issued_at"] = json!(200);
    future["expires_at"] = json!(300);
    assert_eq!(
        guard.check(
            t(102),
            serde_json::from_value(future).unwrap(),
            OutputKind::Speech
        ),
        Err(Error::FuturePermit)
    );
}

#[test]
fn delayed_preparation_cannot_renew_a_cancelled_turn_phase_or_camera_plan() {
    let mut controller = ready(1);
    controller
        .set_camera_permission(t(1), Permission::Allowed)
        .unwrap();
    let camera = controller.camera_grant(t(1)).unwrap();
    let owner = controller.admit(t(1), AdmittedInput::NewTurn).unwrap();
    let hearing_cue = controller
        .plan_output(t(1), owner, OutputKind::Light, None)
        .unwrap();
    let speech = controller
        .plan_output(t(1), owner, OutputKind::Speech, None)
        .unwrap();
    let visual = controller
        .plan_output(t(1), owner, OutputKind::Speech, Some(camera))
        .unwrap();
    assert_eq!(
        controller.issue_output(t(1), speech, 1_000),
        Err(Error::InputStillActive)
    );
    controller.input_ended(t(2), owner).unwrap();
    assert_eq!(
        controller.issue_output(t(2), hearing_cue, 1_000),
        Err(Error::StalePresentation)
    );
    controller.issue_output(t(2), speech, 1_000).unwrap();
    controller
        .set_camera_permission(t(3), Permission::Denied)
        .unwrap();
    controller
        .set_camera_permission(t(4), Permission::Allowed)
        .unwrap();
    assert_eq!(
        controller.issue_output(t(5), visual, 1_000),
        Err(Error::StaleCameraGrant)
    );
    controller.issue_output(t(5), speech, 1_000).unwrap();
    controller.admit(t(6), AdmittedInput::Interruption).unwrap();
    assert_eq!(
        controller.issue_output(t(7), speech, 1_000),
        Err(Error::StaleOwner)
    );
}

#[test]
fn capture_distinguishes_coalesced_privacy_edges_from_ordinary_turns() {
    let mut controller = ready(1);
    let before = controller.snapshot(t(1)).unwrap();
    let mut guard = BoundaryGuard::new(boot(1), t(0));
    guard.install(t(1), before).unwrap();
    let first = answered_input(&mut controller, 2);
    controller.cancel_turn(t(3), first).unwrap();
    let _next = answered_input(&mut controller, 4);
    let ordinary = controller.snapshot(t(5)).unwrap();
    assert_eq!(
        ordinary.microphone_generation(),
        before.microphone_generation()
    );
    assert_ne!(ordinary.generation(), before.generation());
    guard.install(t(5), ordinary).unwrap();

    controller
        .set_microphone_permission(t(6), Permission::Denied)
        .unwrap();
    controller
        .set_microphone_permission(t(7), Permission::Allowed)
        .unwrap();
    let reopened = controller.snapshot(t(8)).unwrap();
    assert_eq!(
        reopened.microphone_permission(),
        before.microphone_permission()
    );
    assert_eq!(
        reopened.microphone_generation(),
        before.microphone_generation() + 2
    );
    assert!(!reopened.listening_ready(t(8)));
    // Only the latest snapshot reaches capture; the lost edge remains detectable.
    guard.install(t(8), reopened).unwrap();
}

#[test]
fn capture_privacy_lineage_cannot_regress_or_hide_a_permission_change() {
    for regressed in [false, true] {
        let mut controller = ready(1);
        let mut guard = BoundaryGuard::new(boot(1), t(0));
        let old = install(&mut controller, &mut guard, 1);
        controller
            .set_microphone_permission(t(2), Permission::Denied)
            .unwrap();
        let mut wire = serde_json::to_value(controller.snapshot(t(3)).unwrap()).unwrap();
        wire["microphone_generation"] = json!(old.microphone_generation() - u64::from(regressed));
        let malformed = serde_json::from_value(wire).unwrap();
        assert_eq!(guard.install(t(3), malformed), Err(Error::StateRegression));
        assert!(guard.is_faulted());
    }
}
