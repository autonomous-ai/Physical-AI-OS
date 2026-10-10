#![cfg(target_os = "linux")]
//! These tests address only ALSA's software `null` plugin, never a microphone.
use lamp_audio::{
    CAPTURE_SAMPLES,
    capture::{CaptureConfig, Discontinuity, ErrorKind, Microphone},
};
use lamp_interaction::{
    AdmissionState, AdmittedInput, BootId, CaptureState, Controller, MonoTime, Permission,
};
use lamp_ipc::monotonic_us;

fn now() -> MonoTime {
    MonoTime::from_micros(monotonic_us())
}

fn permitted(id: u8) -> (Microphone, Controller) {
    let boot = BootId::new([id; 16]).unwrap();
    let mut microphone = Microphone::new("null", boot, CaptureConfig::default()).unwrap();
    let mut controller = Controller::new(boot, now());
    controller
        .set_microphone_permission(now(), Permission::Allowed)
        .unwrap();
    let snapshot = controller.snapshot(now()).unwrap();
    assert!(!snapshot.listening_ready(now()), "open is not readiness");
    microphone.install(snapshot).unwrap();
    (microphone, controller)
}

#[test]
fn construction_and_authority_installation_never_open_a_device() {
    let boot = BootId::new([51; 16]).unwrap();
    // A nonexistent alias proves construction does not attempt snd_pcm_open.
    let mut microphone =
        Microphone::new("lamp_test_no_device_exists", boot, CaptureConfig::default()).unwrap();
    assert!(!microphone.is_open());
    assert!(matches!(
        microphone.start().unwrap_err().kind,
        ErrorKind::NotPermitted
    ));
    let mut controller = Controller::new(boot, now());
    microphone
        .install(controller.snapshot(now()).unwrap())
        .unwrap();
    assert!(!microphone.is_open());
    assert!(microphone.start().is_err());
    controller
        .set_microphone_permission(now(), Permission::Allowed)
        .unwrap();
    microphone
        .install(controller.snapshot(now()).unwrap())
        .unwrap();
    assert!(!microphone.is_open());
    // Deliberately do not start that nonexistent device.
    for bad in ["", "null\0hidden", "null\n"] {
        assert!(Microphone::new(bad, boot, CaptureConfig::default()).is_err());
    }
}

#[test]
fn prepare_does_not_start_or_consume_opening_frames_and_privacy_still_closes_it() {
    let (mut microphone, mut controller) = permitted(59);
    let prepared = microphone.prepare().unwrap();
    assert!(microphone.is_open());
    assert!(!microphone.is_running());
    assert_eq!(
        microphone.try_read().unwrap_err().kind,
        ErrorKind::NotStarted
    );
    let started = microphone.start_prepared().unwrap();
    assert_eq!(started.epoch, prepared.epoch);
    assert_eq!(started.prepared_at_us, prepared.prepared_at_us);
    assert!(prepared.prepared_at_us <= started.capture_started_at_us);
    assert!(microphone.is_running());
    assert_eq!(microphone.try_read().unwrap().unwrap().sequence, 1);
    microphone.revoke().unwrap();
    microphone.reset().unwrap();
    microphone
        .install(controller.snapshot(now()).unwrap())
        .unwrap();
    microphone.prepare().unwrap();
    controller
        .set_microphone_permission(now(), Permission::Denied)
        .unwrap();
    assert!(
        microphone
            .install(controller.snapshot(now()).unwrap())
            .unwrap()
            .closed
    );
    assert!(!microphone.is_running());
    assert!(!microphone.is_open());
    assert!(microphone.start_prepared().is_err());
}

#[test]
fn null_capture_reports_negotiation_and_read_times_without_claiming_acquisition() {
    let (mut microphone, _controller) = permitted(52);
    let start = microphone.start().unwrap();
    assert_eq!(start.format.rate, 16_000);
    assert_eq!(start.format.channels, 1);
    assert!((1..=160).contains(&start.format.period_frames));
    assert!((320..=640).contains(&start.format.buffer_frames));
    assert!(start.open_started_at_us <= start.capture_started_at_us);
    assert!(start.capture_started_at_us <= start.start_completed_at_us);
    let first = microphone
        .try_read()
        .unwrap()
        .expect("null PCM supplies synthetic frames");
    assert_eq!(first.samples.len(), CAPTURE_SAMPLES);
    assert_eq!(first.epoch, start.epoch);
    assert_eq!(first.sequence, 1);
    assert_eq!(first.microphone_generation, start.microphone_generation);
    assert!(first.timing.first_read_started_at_us <= first.timing.last_read_started_at_us);
    assert!(first.timing.last_read_started_at_us <= first.timing.read_completed_at_us);
    assert!(first.timing.read_completed_at_us <= first.timing.status_observed_at_us);
    let second = microphone.try_read().unwrap().unwrap();
    assert_eq!(second.sequence, 2);
    microphone.revoke().unwrap();
    assert!(!microphone.is_open());
    assert_eq!(
        microphone.reset_required(),
        Some(Discontinuity::PrivacyRevoked)
    );
}

#[test]
fn stale_or_duplicate_privacy_packets_preserve_newer_capture() {
    let boot = BootId::new([53; 16]).unwrap();
    let mut microphone = Microphone::new("null", boot, CaptureConfig::default()).unwrap();
    let mut controller = Controller::new(boot, now());
    controller
        .set_microphone_permission(now(), Permission::Denied)
        .unwrap();
    let old = controller.snapshot(now()).unwrap();
    controller
        .set_microphone_permission(now(), Permission::Allowed)
        .unwrap();
    let current = controller.snapshot(now()).unwrap();
    microphone.install(current).unwrap();
    microphone.start().unwrap();
    let epoch = microphone.epoch();
    assert!(microphone.install(old).is_err());
    assert!(microphone.install(current).is_err());
    assert!(microphone.is_open());
    assert_eq!(microphone.epoch(), epoch);
    assert!(microphone.try_read().unwrap().is_some());
    microphone.revoke().unwrap();
}

#[test]
fn local_privacy_revoke_needs_new_authority_and_explicit_reset_and_start() {
    let (mut microphone, mut controller) = permitted(54);
    let first = microphone.start().unwrap();
    microphone.revoke().unwrap();
    microphone.reset().unwrap();
    assert!(
        microphone.start().is_err(),
        "cached Allowed cannot undo local revoke"
    );
    microphone
        .install(controller.snapshot(now()).unwrap())
        .unwrap();
    assert!(!microphone.is_open());
    let second = microphone.start().unwrap();
    assert_eq!(second.epoch, first.epoch + 1);
    assert_eq!(microphone.try_read().unwrap().unwrap().sequence, 1);
    controller
        .set_microphone_permission(now(), Permission::Denied)
        .unwrap();
    let update = microphone
        .install(controller.snapshot(now()).unwrap())
        .unwrap();
    assert!(update.closed);
    assert!(!microphone.is_open());
    controller
        .set_microphone_permission(now(), Permission::Allowed)
        .unwrap();
    microphone
        .install(controller.snapshot(now()).unwrap())
        .unwrap();
    assert!(matches!(
        microphone.start().unwrap_err().kind,
        ErrorKind::ResetRequired(Discontinuity::PrivacyRevoked)
    ));
}

#[test]
fn coalesced_privacy_changes_close_capture_but_new_conversation_does_not() {
    let (mut microphone, mut controller) = permitted(55);
    microphone.start().unwrap();
    controller
        .set_capture(
            now(),
            CaptureState::RetainingUntil(now().checked_add(500_000).unwrap()),
        )
        .unwrap();
    controller
        .set_admission(
            now(),
            AdmissionState::OpenUntil(now().checked_add(500_000).unwrap()),
        )
        .unwrap();
    controller.admit(now(), AdmittedInput::NewTurn).unwrap();
    assert!(
        !microphone
            .install(controller.snapshot(now()).unwrap())
            .unwrap()
            .closed
    );
    assert!(microphone.is_open());
    // Only publish after both edges: permission looks unchanged, lineage differs.
    controller
        .set_microphone_permission(now(), Permission::Denied)
        .unwrap();
    controller
        .set_microphone_permission(now(), Permission::Allowed)
        .unwrap();
    assert!(
        microphone
            .install(controller.snapshot(now()).unwrap())
            .unwrap()
            .closed
    );
    assert!(!microphone.is_open());
    assert!(microphone.start().is_err());
    microphone.reset().unwrap();
    microphone.start().unwrap();
    microphone.revoke().unwrap();
}

#[test]
fn expiry_closes_even_without_new_audio_and_control_loss_cannot_be_reset() {
    let (mut microphone, mut controller) = permitted(56);
    microphone.start().unwrap();
    std::thread::sleep(std::time::Duration::from_millis(260));
    assert!(matches!(
        microphone.maintain().unwrap_err().kind,
        ErrorKind::Discontinuity(Discontinuity::AuthorityExpired)
    ));
    assert!(!microphone.is_open());
    microphone
        .install(controller.snapshot(now()).unwrap())
        .unwrap();
    microphone.reset().unwrap();
    microphone.start().unwrap();
    microphone.control_lost().unwrap();
    assert!(!microphone.is_open());
    assert!(microphone.reset().is_err());
    assert!(
        microphone
            .install(controller.snapshot(now()).unwrap())
            .is_err()
    );
    assert!(microphone.start().is_err());
}
