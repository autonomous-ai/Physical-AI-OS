use lamp_interaction::{BootId, MonoTime};
use lamp_live::{
    activity::{Activity, ObservedAudio, TurnDetector},
    privacy::PrivacyGate,
    wire::{Envelope, ReceiveOrder, decode},
};

fn time(t: u64) -> MonoTime {
    MonoTime::from_micros(t)
}
fn audio(sequence: u64) -> ObservedAudio {
    ObservedAudio {
        sequence,
        captured_at_us: sequence * 10_000,
        samples: [sequence as i16; 160],
    }
}

#[test]
fn mute_is_immediate_reopening_stable_and_missing_samples_close() {
    let mut gate = PrivacyGate::default();
    for sequence in 1..=7 {
        let t = time(sequence * 10_000);
        assert_eq!(gate.observe(sequence, t, t, false), sequence == 7);
    }
    assert!(!gate.allowed(time(120_000)));
    assert!(!gate.observe(8, time(130_000), time(130_000), false));
    assert!(!gate.observe(9, time(140_000), time(140_000), true));
    assert!(!gate.allowed(time(140_001)));
}

#[test]
fn privacy_reorder_future_and_stale_fail_closed_without_rolling_back_highwater() {
    let mut gate = PrivacyGate::default();
    for seq in 1..=7 {
        let t = time(seq * 10_000);
        gate.observe(seq, t, t, false);
    }
    assert!(gate.allowed(time(70_000)));
    assert!(!gate.observe(6, time(60_000), time(71_000), false));
    assert!(!gate.observe(8, time(100_000), time(72_000), false));
    assert!(!gate.observe(8, time(80_000), time(180_000), false));
    assert!(!gate.observe(8, time(180_000), time(180_000), false));
}

#[test]
fn retained_prefix_and_endpoint_include_every_active_frame_once() {
    let mut detector = TurnDetector::default();
    for seq in 1..=25 {
        assert!(matches!(detector.push(audio(seq), 0.0), Activity::Quiet));
    }
    for seq in 26..=30 {
        assert!(matches!(detector.push(audio(seq), 0.95), Activity::Quiet));
    }
    let Activity::Start(prefix) = detector.push(audio(31), 0.95) else {
        panic!("speech start");
    };
    assert_eq!(prefix.len(), 30);
    assert_eq!(prefix.first().unwrap().sequence, 2);
    assert_eq!(prefix.last().unwrap().sequence, 31);
    for seq in 32..=90 {
        assert!(matches!(
            detector.push(audio(seq), 0.1),
            Activity::Continue(_)
        ));
    }
    assert!(matches!(detector.push(audio(91), 0.1), Activity::End(_)));
    assert!(matches!(detector.push(audio(92), 0.1), Activity::Quiet));
}

#[test]
fn missing_audio_or_invalid_probability_is_failure_not_a_silent_endpoint() {
    let mut detector = TurnDetector::default();
    for seq in 1..=6 {
        detector.push(audio(seq), 0.99);
    }
    assert!(matches!(detector.push(audio(8), 0.99), Activity::Fault(_)));
    assert!(matches!(
        detector.push(audio(9), f32::NAN),
        Activity::Fault(_)
    ));
    assert!(matches!(detector.push(audio(10), 1.1), Activity::Fault(_)));
}

#[test]
fn ipc_replay_or_other_worker_cannot_replace_fresh_observation() {
    let boot = BootId::new([1; 16]).unwrap();
    let mut order = ReceiveOrder::new(boot);
    let packet = Envelope {
        boot,
        sequence: 3,
        sent_at_us: 100,
        payload: (),
    };
    order.accept(&packet, 101, 50).unwrap();
    assert!(order.accept(&packet, 102, 50).is_err());
    let other = Envelope {
        boot: BootId::new([2; 16]).unwrap(),
        sequence: 4,
        sent_at_us: 103,
        payload: (),
    };
    assert!(order.accept(&other, 104, 50).is_err());
    assert!(decode::<Envelope<()>>(&vec![b' '; 8193]).is_err());
}
