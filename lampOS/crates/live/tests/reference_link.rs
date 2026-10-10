use lamp_live::{
    process::{SessionDirectory, new_boot},
    transport::ReferenceChannels,
    wire::ReferenceControl,
};
use std::io;

#[test]
fn direct_control_remains_usable_when_actual_render_socket_is_full() {
    let dir = SessionDirectory::create().unwrap();
    let speaker_boot = new_boot().unwrap();
    let capture_boot = new_boot().unwrap();
    let mut capture =
        ReferenceChannels::bind(&dir.path, "capture", capture_boot, speaker_boot).unwrap();
    assert!(
        !capture.connect_step().unwrap(),
        "missing peer never blocks the worker"
    );
    let mut speaker =
        ReferenceChannels::bind(&dir.path, "speaker", speaker_boot, capture_boot).unwrap();
    assert!(speaker.connect_step().unwrap());
    assert!(capture.connect_step().unwrap());
    let bulk = vec![0u8; 2000];
    let mut blocked = false;
    for _ in 0..256 {
        match speaker.data.send(&bulk) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                blocked = true;
                break;
            }
            Err(error) => panic!("unexpected IPC error: {error}"),
        }
    }
    assert!(blocked, "bounded real socket must expose backpressure");
    speaker
        .control
        .send(ReferenceControl::Stopped {
            playback_epoch: 2,
            observed_at_us: lamp_ipc::monotonic_us(),
        })
        .unwrap();
    assert!(matches!(
        capture.control.receive::<ReferenceControl>().unwrap(),
        Some(ReferenceControl::Stopped {
            playback_epoch: 2,
            ..
        })
    ));
    capture
        .control
        .send(ReferenceControl::ReferencePrimed {
            playback_epoch: 3,
            analysed_through: 480,
        })
        .unwrap();
    assert!(matches!(
        speaker.control.receive::<ReferenceControl>().unwrap(),
        Some(ReferenceControl::ReferencePrimed {
            playback_epoch: 3,
            ..
        })
    ));
}

#[test]
fn trusted_reference_peer_boot_cannot_be_learned_from_incoming_messages() {
    let dir = SessionDirectory::create().unwrap();
    let actual = new_boot().unwrap();
    let expected = new_boot().unwrap();
    let capture_boot = new_boot().unwrap();
    let mut capture =
        ReferenceChannels::bind(&dir.path, "capture", capture_boot, expected).unwrap();
    let mut speaker = ReferenceChannels::bind(&dir.path, "speaker", actual, capture_boot).unwrap();
    capture.connect_step().unwrap();
    speaker.connect_step().unwrap();
    speaker
        .control
        .send(ReferenceControl::Stopped {
            playback_epoch: 1,
            observed_at_us: lamp_ipc::monotonic_us(),
        })
        .unwrap();
    assert_eq!(
        capture
            .control
            .receive::<ReferenceControl>()
            .unwrap_err()
            .kind(),
        io::ErrorKind::InvalidData
    );
}

#[test]
fn terminal_notice_survives_capture_closing_first_without_relaxing_live_sends() {
    let dir = SessionDirectory::create().unwrap();
    let speaker_boot = new_boot().unwrap();
    let capture_boot = new_boot().unwrap();
    let mut capture =
        ReferenceChannels::bind(&dir.path, "capture", capture_boot, speaker_boot).unwrap();
    let mut speaker =
        ReferenceChannels::bind(&dir.path, "speaker", speaker_boot, capture_boot).unwrap();
    assert!(capture.connect_step().unwrap());
    assert!(speaker.connect_step().unwrap());
    drop(capture);
    assert!(!dir.path.join("ref.c.capture").exists());
    assert!(!dir.path.join("ref.d.capture").exists());

    // This is the optional final notice after successful local hardware closure,
    // not a live reference send or a claim that the peer acknowledged shutdown.
    speaker.notify_stopped_after_local_close(2);
    assert!(
        speaker
            .control
            .send(ReferenceControl::ReferencePrimed {
                playback_epoch: 2,
                analysed_through: 480,
            })
            .is_err(),
        "a closed peer must still fail ordinary live control delivery"
    );
    assert!(
        speaker.data.send([0_u8; 4]).is_err(),
        "a closed peer must still fail ordinary live data delivery"
    );
}

#[test]
fn terminal_notice_reaches_capture_when_speaker_closes_first() {
    let dir = SessionDirectory::create().unwrap();
    let speaker_boot = new_boot().unwrap();
    let capture_boot = new_boot().unwrap();
    let mut capture =
        ReferenceChannels::bind(&dir.path, "capture", capture_boot, speaker_boot).unwrap();
    let mut speaker =
        ReferenceChannels::bind(&dir.path, "speaker", speaker_boot, capture_boot).unwrap();
    assert!(capture.connect_step().unwrap());
    assert!(speaker.connect_step().unwrap());
    let earliest = lamp_ipc::monotonic_us();
    speaker.notify_stopped_after_local_close(7);
    drop(speaker);
    let latest = lamp_ipc::monotonic_us();
    match capture.control.receive::<ReferenceControl>().unwrap() {
        Some(ReferenceControl::Stopped {
            playback_epoch,
            observed_at_us,
        }) => {
            assert_eq!(playback_epoch, 7);
            assert!((earliest..=latest).contains(&observed_at_us));
        }
        other => panic!("expected one terminal notice, got {other:?}"),
    }
    match capture.control.receive::<ReferenceControl>() {
        Ok(None) => {}
        // Darwin reports peer closure after the queued terminal notice is read.
        Err(error) if error.kind() == io::ErrorKind::ConnectionReset => {}
        other => panic!("expected no second notice from the closed peer, got {other:?}"),
    }
}

#[test]
fn terminal_notice_never_connects_an_unconnected_control_socket() {
    let dir = SessionDirectory::create().unwrap();
    let speaker_boot = new_boot().unwrap();
    let capture_boot = new_boot().unwrap();
    let mut capture =
        ReferenceChannels::bind(&dir.path, "capture", capture_boot, speaker_boot).unwrap();
    let mut speaker =
        ReferenceChannels::bind(&dir.path, "speaker", speaker_boot, capture_boot).unwrap();
    // The peer paths exist, so an accidental connect_step would succeed.
    speaker.notify_stopped_after_local_close(2);
    assert_eq!(
        speaker
            .control
            .send(ReferenceControl::ReferencePrimed {
                playback_epoch: 2,
                analysed_through: 480,
            })
            .unwrap_err()
            .kind(),
        io::ErrorKind::NotConnected
    );
    assert!(capture.connect_step().unwrap());
    assert!(speaker.connect_step().unwrap());
    assert!(
        capture
            .control
            .receive::<ReferenceControl>()
            .unwrap()
            .is_none()
    );
}

#[test]
fn terminal_notice_uses_an_existing_control_connection_without_connecting_data() {
    let dir = SessionDirectory::create().unwrap();
    let speaker_boot = new_boot().unwrap();
    let capture_boot = new_boot().unwrap();
    let mut capture_control = lamp_live::transport::Channel::bind(
        &dir.path.join("ref.c.capture"),
        capture_boot,
        speaker_boot,
        100_000,
    )
    .unwrap();
    let mut speaker =
        ReferenceChannels::bind(&dir.path, "speaker", speaker_boot, capture_boot).unwrap();
    assert!(!speaker.connect_step().unwrap());
    capture_control
        .connect(&dir.path.join("ref.c.speaker"))
        .unwrap();
    speaker.notify_stopped_after_local_close(3);
    assert!(matches!(
        capture_control.receive::<ReferenceControl>().unwrap(),
        Some(ReferenceControl::Stopped {
            playback_epoch: 3,
            ..
        })
    ));
    assert!(
        capture_control
            .receive::<ReferenceControl>()
            .unwrap()
            .is_none()
    );
    assert_eq!(
        speaker.data.send([0_u8; 4]).unwrap_err().kind(),
        io::ErrorKind::NotConnected
    );
}
