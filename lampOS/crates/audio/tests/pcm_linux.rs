#![cfg(target_os = "linux")]
use lamp_audio::{RENDER_SAMPLES, pcm::Speaker};
use lamp_interaction::{
    AdmissionState, AdmittedInput, BootId, CaptureState, Controller, MonoTime, OutputKind,
    Permission,
};
use lamp_ipc::monotonic_us;
fn now() -> MonoTime {
    MonoTime::from_micros(monotonic_us())
}

#[test]
fn null_device_checks_ownership_at_the_alsa_write_and_flushes_on_cancellation() {
    // ALSA null is an installed software plugin. This never addresses a real speaker.
    let boot = BootId::new([42; 16]).unwrap();
    let mut speaker = Speaker::open("null", boot).unwrap();
    let mut controller = Controller::new(boot, now());
    controller
        .set_microphone_permission(now(), Permission::Allowed)
        .unwrap();
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
    let owner = controller.admit(now(), AdmittedInput::NewTurn).unwrap();
    controller.input_ended(now(), owner).unwrap();
    let output = controller
        .plan_output(now(), owner, OutputKind::Speech, None)
        .unwrap();
    let permit = controller.issue_output(now(), output, 100_000).unwrap();
    speaker
        .install(controller.snapshot(now()).unwrap())
        .unwrap();
    assert_eq!(
        speaker
            .try_write(permit, &[100; RENDER_SAMPLES])
            .unwrap()
            .frames,
        RENDER_SAMPLES
    );
    controller.cancel_turn(now(), owner).unwrap();
    speaker
        .install(controller.snapshot(now()).unwrap())
        .unwrap();
    assert!(!speaker.is_active());
    assert!(speaker.try_write(permit, &[100; RENDER_SAMPLES]).is_err());
}

#[test]
fn null_device_starts_disarmed_and_stays_disarmed_after_transport_failure() {
    let boot = BootId::new([43; 16]).unwrap();
    let mut speaker = Speaker::open("null", boot).unwrap();
    assert!(!speaker.is_active());
    speaker.fault().unwrap();
    let mut controller = Controller::new(boot, now());
    assert!(
        speaker
            .install(controller.snapshot(now()).unwrap())
            .is_err()
    );
}

#[test]
fn zero_clock_requires_explicit_prime_start_and_privacy_without_speech_authority() {
    let boot = BootId::new([49; 16]).unwrap();
    let mut speaker = Speaker::open_clock("null", boot).unwrap();
    assert!(speaker.zero_authority().is_err());
    let mut controller = Controller::new(boot, now());
    controller
        .set_microphone_permission(now(), Permission::Allowed)
        .unwrap();
    let snapshot = controller.snapshot(now()).unwrap();
    assert!(!snapshot.listening_ready(now()));
    speaker.install(snapshot).unwrap();
    let token = speaker.zero_authority().unwrap();
    assert!(speaker.start_clock(token).is_err());
    assert!(speaker.try_write_silence(token, 0).is_err());
    assert!(speaker.try_write_silence(token, 241).is_err());
    let first = speaker.try_write_silence(token, 240).unwrap();
    assert_eq!(first.frames, 240);
    assert_eq!(first.first_sample.frame, 0);
    assert_eq!(first.end_sample.frame, 240);
    assert!(!speaker.clock_started());
    assert!(!speaker.speech_retired(first.end_sample));
    assert!(speaker.start_clock(token).is_err());
    let second = speaker.try_write_silence(token, 240).unwrap();
    assert_eq!(second.first_sample, first.end_sample);
    assert_eq!(speaker.queued_range().accepted_through, 480);
    speaker.start_clock(token).unwrap();
    assert!(speaker.clock_started());
    assert!(speaker.begin_finish().is_err());
    speaker.stop().unwrap();
    assert!(!speaker.clock_started());
    assert!(!speaker.speech_retired(second.end_sample));
    assert_eq!(
        speaker.last_discard().unwrap().epoch,
        first.first_sample.epoch
    );
    controller
        .set_microphone_permission(now(), Permission::Denied)
        .unwrap();
    speaker
        .install(controller.snapshot(now()).unwrap())
        .unwrap();
    assert!(speaker.try_write_silence(token, 240).is_err());
}

#[test]
fn stale_packets_cannot_stop_newer_valid_playback() {
    let boot = BootId::new([44; 16]).unwrap();
    let mut speaker = Speaker::open("null", boot).unwrap();
    let mut controller = Controller::new(boot, now());
    controller
        .set_microphone_permission(now(), Permission::Allowed)
        .unwrap();
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
    let old_owner = controller.admit(now(), AdmittedInput::NewTurn).unwrap();
    controller.input_ended(now(), old_owner).unwrap();
    let old_output = controller
        .plan_output(now(), old_owner, OutputKind::Speech, None)
        .unwrap();
    let old_permit = controller.issue_output(now(), old_output, 100_000).unwrap();
    let old_state = controller.snapshot(now()).unwrap();
    speaker.install(old_state).unwrap();
    speaker
        .try_write(old_permit, &[100; RENDER_SAMPLES])
        .unwrap();
    let current_owner = controller
        .admit(now(), AdmittedInput::Interruption)
        .unwrap();
    controller.input_ended(now(), current_owner).unwrap();
    let output = controller
        .plan_output(now(), current_owner, OutputKind::Speech, None)
        .unwrap();
    let permit = controller.issue_output(now(), output, 100_000).unwrap();
    let current_state = controller.snapshot(now()).unwrap();
    speaker.install(current_state).unwrap();
    speaker.try_write(permit, &[200; RENDER_SAMPLES]).unwrap();
    let flushes = speaker.flush_count();
    assert!(
        speaker
            .try_write(old_permit, &[100; RENDER_SAMPLES])
            .is_err()
    );
    assert_eq!(
        speaker.flush_count(),
        flushes,
        "stale PCM flushed newer playback"
    );
    assert!(speaker.install(old_state).is_err());
    assert_eq!(
        speaker.flush_count(),
        flushes,
        "stale control flushed newer playback"
    );
    assert!(speaker.install(current_state).is_err());
    assert_eq!(
        speaker.flush_count(),
        flushes,
        "duplicate control flushed newer playback"
    );
    assert!(speaker.try_write(permit, &[200; RENDER_SAMPLES]).is_ok());
}

#[test]
fn null_completion_is_observed_without_waiting_for_a_permit_to_expire() {
    let boot = BootId::new([45; 16]).unwrap();
    let mut speaker = Speaker::open("null", boot).unwrap();
    let state = speaker.poll().unwrap();
    assert_eq!(state.queued_frames, 0);
    assert_eq!(state.tracked_authorized_frames, 0);
    assert!(!speaker.is_active());
    assert!(speaker.last_poll().is_some());
}

fn playable_null(
    id: u8,
) -> (
    Speaker,
    Controller,
    lamp_interaction::TurnOwner,
    lamp_interaction::OutputPermit,
) {
    let boot = BootId::new([id; 16]).unwrap();
    let mut speaker = Speaker::open("null", boot).unwrap();
    let mut controller = Controller::new(boot, now());
    controller
        .set_microphone_permission(now(), Permission::Allowed)
        .unwrap();
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
    let owner = controller.admit(now(), AdmittedInput::NewTurn).unwrap();
    controller.input_ended(now(), owner).unwrap();
    let output = controller
        .plan_output(now(), owner, OutputKind::Speech, None)
        .unwrap();
    let permit = controller.issue_output(now(), output, 100_000).unwrap();
    speaker
        .install(controller.snapshot(now()).unwrap())
        .unwrap();
    (speaker, controller, owner, permit)
}

#[test]
fn declared_null_finish_prepares_for_another_generation_in_the_same_turn() {
    let (mut speaker, mut controller, owner, permit) = playable_null(46);
    let written = speaker
        .try_write_final(permit, &[31; RENDER_SAMPLES])
        .unwrap();
    assert_eq!(written.frames, RENDER_SAMPLES);
    assert!(speaker.is_finishing());
    assert!(speaker.begin_finish().unwrap());
    assert!(!speaker.is_finishing());
    assert!(!speaker.is_active());
    assert_eq!(speaker.poll().unwrap().queued_frames, 0);
    let second_output = controller
        .plan_output(now(), owner, OutputKind::Speech, None)
        .unwrap();
    let second = controller
        .issue_output(now(), second_output, 100_000)
        .unwrap();
    speaker
        .install(controller.snapshot(now()).unwrap())
        .unwrap();
    assert_eq!(
        speaker
            .try_write_final(second, &[32; RENDER_SAMPLES])
            .unwrap()
            .frames,
        RENDER_SAMPLES
    );
    assert!(speaker.poll_finish().unwrap());
}

#[test]
fn explicit_stop_preempts_a_declared_finish_and_keeps_new_output_usable() {
    let (mut speaker, mut controller, owner, permit) = playable_null(47);
    speaker
        .try_write_final(permit, &[41; RENDER_SAMPLES])
        .unwrap();
    let flushes = speaker.flush_count();
    speaker.stop().unwrap();
    assert!(!speaker.is_finishing());
    assert_eq!(speaker.flush_count(), flushes + 1);
    controller.cancel_turn(now(), owner).unwrap();
    speaker
        .install(controller.snapshot(now()).unwrap())
        .unwrap();
    assert!(
        speaker
            .try_write_final(permit, &[42; RENDER_SAMPLES])
            .is_err()
    );
    let new_owner = controller.admit(now(), AdmittedInput::NewTurn).unwrap();
    controller.input_ended(now(), new_owner).unwrap();
    let output = controller
        .plan_output(now(), new_owner, OutputKind::Speech, None)
        .unwrap();
    let new_permit = controller.issue_output(now(), output, 100_000).unwrap();
    speaker
        .install(controller.snapshot(now()).unwrap())
        .unwrap();
    assert_eq!(
        speaker
            .try_write_final(new_permit, &[43; RENDER_SAMPLES])
            .unwrap()
            .frames,
        RENDER_SAMPLES
    );
    assert!(speaker.begin_finish().unwrap());
}
