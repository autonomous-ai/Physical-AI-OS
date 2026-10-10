use lamp_interaction::{
    AdmissionState, AdmittedInput, BootId, BoundaryGuard, CaptureState, Controller, Error,
    MonoTime, Permission,
};

fn t(us: u64) -> MonoTime {
    MonoTime::from_micros(us)
}

#[test]
fn zero_authority_requires_fresh_privacy_but_never_invents_input_readiness() {
    let boot = BootId::new([121; 16]).unwrap();
    let mut controller = Controller::new(boot, t(0));
    let mut guard = BoundaryGuard::new(boot, t(0));
    assert_eq!(guard.zero_authority(t(0)), Err(Error::NoAuthority));
    guard
        .install(t(0), controller.snapshot(t(0)).unwrap())
        .unwrap();
    assert_eq!(guard.zero_authority(t(0)), Err(Error::PrivacyClosed));
    controller
        .set_microphone_permission(t(1), Permission::Allowed)
        .unwrap();
    let state = controller.snapshot(t(1)).unwrap();
    assert!(!state.listening_ready(t(1)));
    guard.install(t(1), state).unwrap();
    let zero = guard.zero_authority(t(1)).unwrap();
    guard.check_zero(t(2), zero).unwrap();
    assert_eq!(
        guard.check_zero(state.expires_at(), zero),
        Err(Error::ExpiredState)
    );
}

#[test]
fn session_token_survives_turns_but_never_a_coalesced_privacy_reopen() {
    let boot = BootId::new([122; 16]).unwrap();
    let mut controller = Controller::new(boot, t(0));
    controller
        .set_microphone_permission(t(0), Permission::Allowed)
        .unwrap();
    let mut guard = BoundaryGuard::new(boot, t(0));
    guard
        .install(t(0), controller.snapshot(t(0)).unwrap())
        .unwrap();
    let zero = guard.zero_authority(t(0)).unwrap();
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
    guard.check_zero(t(1), zero).unwrap();
    controller
        .set_microphone_permission(t(2), Permission::Denied)
        .unwrap();
    controller
        .set_microphone_permission(t(3), Permission::Allowed)
        .unwrap();
    guard
        .install(t(3), controller.snapshot(t(3)).unwrap())
        .unwrap();
    assert_eq!(guard.check_zero(t(3), zero), Err(Error::PrivacyClosed));
    let reopened = guard.zero_authority(t(3)).unwrap();
    assert_ne!(
        zero.microphone_generation(),
        reopened.microphone_generation()
    );
    guard.check_zero(t(3), reopened).unwrap();
    assert_eq!(
        guard.check_zero(t(2), reopened),
        Err(Error::ClockRegression)
    );
    assert_eq!(guard.zero_authority(t(4)), Err(Error::Faulted));
}

#[test]
fn zero_session_token_cannot_cross_a_controller_boot() {
    let boot = BootId::new([123; 16]).unwrap();
    let mut controller = Controller::new(boot, t(0));
    controller
        .set_microphone_permission(t(0), Permission::Allowed)
        .unwrap();
    let mut guard = BoundaryGuard::new(boot, t(0));
    guard
        .install(t(0), controller.snapshot(t(0)).unwrap())
        .unwrap();
    let token = guard.zero_authority(t(0)).unwrap();
    let mut other = BoundaryGuard::new(BootId::new([124; 16]).unwrap(), t(0));
    assert_eq!(other.check_zero(t(0), token), Err(Error::WrongBoot));
}
