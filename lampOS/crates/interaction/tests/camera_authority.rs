use lamp_interaction::{
    AdmissionState, AdmittedInput, BootId, BoundaryGuard, CaptureState, Controller, Error,
    MonoTime, Permission,
};
fn t(us: u64) -> MonoTime {
    MonoTime::from_micros(us)
}
fn boot(n: u8) -> BootId {
    BootId::new([n; 16]).unwrap()
}

#[test]
fn camera_grant_needs_current_camera_authority_but_not_audio_readiness() {
    let mut controller = Controller::new(boot(41), t(0));
    let mut guard = BoundaryGuard::new(boot(41), t(0));
    assert_eq!(guard.current_camera_grant(t(0)), Err(Error::NoAuthority));
    guard
        .install(t(0), controller.snapshot(t(0)).unwrap())
        .unwrap();
    assert_eq!(guard.current_camera_grant(t(0)), Err(Error::PrivacyClosed));
    controller
        .set_camera_permission(t(1), Permission::Allowed)
        .unwrap();
    let snapshot = controller.snapshot(t(1)).unwrap();
    assert!(!snapshot.listening_ready(t(1)));
    guard.install(t(1), snapshot).unwrap();
    assert_eq!(
        guard.current_camera_grant(t(1)).unwrap(),
        controller.camera_grant(t(1)).unwrap()
    );
    assert_eq!(
        guard.current_camera_grant(snapshot.expires_at()),
        Err(Error::ExpiredState)
    );
}

#[test]
fn ordinary_turn_preserves_grant_but_coalesced_privacy_reopen_changes_it() {
    let mut controller = Controller::new(boot(42), t(0));
    controller
        .set_camera_permission(t(0), Permission::Allowed)
        .unwrap();
    let mut guard = BoundaryGuard::new(boot(42), t(0));
    let old_snapshot = controller.snapshot(t(0)).unwrap();
    guard.install(t(0), old_snapshot).unwrap();
    let original = guard.current_camera_grant(t(0)).unwrap();
    controller
        .set_microphone_permission(t(1), Permission::Allowed)
        .unwrap();
    controller
        .set_capture(t(1), CaptureState::RetainingUntil(t(100_000)))
        .unwrap();
    controller
        .set_admission(t(1), AdmissionState::OpenUntil(t(100_000)))
        .unwrap();
    controller.admit(t(1), AdmittedInput::NewTurn).unwrap();
    guard
        .install(t(1), controller.snapshot(t(1)).unwrap())
        .unwrap();
    assert_eq!(guard.current_camera_grant(t(1)).unwrap(), original);
    controller
        .set_camera_permission(t(2), Permission::Denied)
        .unwrap();
    controller
        .set_camera_permission(t(3), Permission::Allowed)
        .unwrap();
    guard
        .install(t(3), controller.snapshot(t(3)).unwrap())
        .unwrap();
    let reopened = guard.current_camera_grant(t(3)).unwrap();
    assert_ne!(reopened, original);
    assert_eq!(guard.install(t(4), old_snapshot), Err(Error::StaleSnapshot));
    assert_eq!(guard.current_camera_grant(t(4)).unwrap(), reopened);
    guard.invalidate();
    assert_eq!(guard.current_camera_grant(t(5)), Err(Error::Faulted));
}

#[test]
fn camera_grant_clock_regression_latches_fault() {
    let mut controller = Controller::new(boot(43), t(0));
    controller
        .set_camera_permission(t(0), Permission::Allowed)
        .unwrap();
    let mut guard = BoundaryGuard::new(boot(43), t(0));
    guard
        .install(t(0), controller.snapshot(t(0)).unwrap())
        .unwrap();
    guard.current_camera_grant(t(2)).unwrap();
    assert_eq!(
        guard.current_camera_grant(t(1)),
        Err(Error::ClockRegression)
    );
    assert_eq!(guard.current_camera_grant(t(3)), Err(Error::Faulted));
}
