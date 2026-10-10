use super::*;
use crate::{
    process::SessionDirectory,
    provider_worker::{ProviderInput, ProviderOutput},
    transport::WorkerChannels,
};
use lamp_interaction::BootId;
use lamp_ipc::monotonic_us;
use std::{collections::VecDeque, thread, time::Duration};

/// Every normal counter state in this fixture is reached through public flow
/// operations. No test grants itself a prefilled 30-second reply.
#[derive(Default)]
struct Flow {
    sender: OutputSender,
    receiver: OutputReceiver,
    buffered: usize,
}

impl Flow {
    fn replenish(&mut self) {
        let sender = &mut self.sender;
        self.receiver
            .replenish_with(self.buffered, |through| sender.grant(through))
            .unwrap();
        self.invariants();
    }

    fn deliver(&mut self, samples: usize, retain: bool) {
        assert!(samples <= PACKET_SAMPLES);
        assert!(self.sender.try_send(|| Ok(())).unwrap());
        self.receiver.received().unwrap();
        if retain {
            self.buffered += samples;
        }
        self.replenish();
    }

    fn fill(&mut self) {
        assert_eq!(self.buffered, 0);
        assert_eq!(MAX_REPLY_SAMPLES % PACKET_SAMPLES, 0);
        for _ in 0..MAX_REPLY_SAMPLES / PACKET_SAMPLES {
            self.deliver(PACKET_SAMPLES, true);
        }
        assert_eq!(self.buffered, MAX_REPLY_SAMPLES);
        assert!(!self.sender.can_send());
        assert_eq!(self.receiver.received, self.receiver.granted);
    }

    fn invariants(&self) {
        assert!(self.receiver.received <= self.sender.sent);
        assert!(self.sender.sent <= self.sender.through);
        assert!(self.sender.through <= self.receiver.granted);
        let reservations = self.receiver.granted - self.receiver.received;
        assert!(reservations <= PACKET_WINDOW);
        assert!(self.buffered + reservations as usize * PACKET_SAMPLES <= MAX_REPLY_SAMPLES);
    }
}

struct Channels {
    parent: WorkerChannels,
    child: WorkerChannels,
    // Drop endpoints before their directory; no child process is started.
    _directory: SessionDirectory,
}

impl Channels {
    fn new() -> Self {
        let directory = SessionDirectory::create().unwrap();
        let parent_boot = BootId::new([91; 16]).unwrap();
        let worker_boot = BootId::new([92; 16]).unwrap();
        let mut parent =
            WorkerChannels::bind(&directory.path, "provider", parent_boot, worker_boot, false)
                .unwrap();
        let mut child =
            WorkerChannels::bind(&directory.path, "provider", parent_boot, worker_boot, true)
                .unwrap();
        parent.connect(&directory.path, "provider", false).unwrap();
        child.connect(&directory.path, "provider", true).unwrap();
        Self {
            parent,
            child,
            _directory: directory,
        }
    }

    fn replenish(&mut self, flow: &mut Flow) {
        flow.receiver
            .replenish(flow.buffered, &mut self.parent.control)
            .unwrap();
        while let Some(control) = self.child.control.receive::<Control>().unwrap() {
            let Control::ProviderOutputCapacity { through } = control else {
                panic!("unexpected control in capacity fixture");
            };
            flow.sender.grant(through).unwrap();
        }
        flow.invariants();
    }

    fn exchange(&mut self, flow: &mut Flow, event: &ProviderOutput, retain: bool) {
        let expected = serde_json::to_value(event).unwrap();
        assert!(
            flow.sender
                .try_send(|| self.child.data.send(event))
                .unwrap()
        );
        let received = self
            .parent
            .data
            .receive::<ProviderOutput>()
            .unwrap()
            .expect("locally sent packet must be present");
        assert_eq!(serde_json::to_value(&received).unwrap(), expected);
        flow.receiver.received().unwrap();
        if retain && let ProviderOutput::Audio { samples, .. } = received {
            flow.buffered += samples.len();
        }
        self.replenish(flow);
    }
}

fn audio(sequence: u64, samples: usize) -> ProviderOutput {
    ProviderOutput::Audio {
        request: 7,
        sequence,
        provider_event_at_us: monotonic_us(),
        source_frames: samples,
        source_offset: 0,
        samples: vec![(sequence % 20_000) as i16; samples],
    }
}

#[test]
fn initial_window_allows_exactly_two_packets_without_a_receipt() {
    let mut sender = OutputSender::default();
    let mut receiver = OutputReceiver::default();
    for expected in 1..=2 {
        assert!(sender.try_send(|| Ok(())).unwrap());
        assert_eq!(sender.sent, expected);
    }
    assert!(
        !sender
            .try_send(|| panic!("no credit must mean no I/O"))
            .unwrap()
    );
    assert!(sender.record_sent().is_err());
    for _ in 0..2 {
        receiver.received().unwrap();
    }
    assert!(receiver.received().is_err());
    assert_eq!((sender.sent, receiver.received), (2, 2));
}

#[test]
fn would_block_and_transport_failure_never_consume_send_credit() {
    let mut sender = OutputSender::default();
    assert!(
        !sender
            .try_send(|| Err(io::ErrorKind::WouldBlock.into()))
            .unwrap()
    );
    let error = sender
        .try_send(|| Err(io::ErrorKind::BrokenPipe.into()))
        .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::BrokenPipe);
    assert_eq!((sender.sent, sender.through), (0, 2));
    assert!(sender.try_send(|| Ok(())).unwrap());
    assert!(sender.try_send(|| Ok(())).unwrap());
    assert!(!sender.can_send());
}

#[test]
fn grants_are_cumulative_and_duplicates_do_not_add_capacity() {
    let mut sender = OutputSender::default();
    sender.grant(2).unwrap();
    assert!(sender.grant(1).is_err());
    assert!(sender.grant(3).is_err());
    assert_eq!((sender.sent, sender.through), (0, 2));
    assert!(sender.try_send(|| Ok(())).unwrap());
    sender.grant(3).unwrap();
    sender.grant(3).unwrap();
    assert!(sender.try_send(|| Ok(())).unwrap());
    assert!(sender.try_send(|| Ok(())).unwrap());
    sender.grant(3).unwrap();
    assert!(!sender.can_send());
    assert!(sender.grant(6).is_err());
    sender.grant(5).unwrap();
    assert!(sender.grant(4).is_err());
    assert_eq!((sender.sent, sender.through), (3, 5));
}

#[test]
fn failed_capacity_delivery_does_not_grant_and_can_be_coalesced_after_cancel() {
    let mut flow = Flow::default();
    flow.fill();
    let exhausted = flow.receiver.received;
    flow.buffered -= PACKET_SAMPLES;
    let mut attempted = None;
    flow.receiver
        .replenish_with(flow.buffered, |through| {
            attempted = Some(through);
            Err(io::ErrorKind::WouldBlock.into())
        })
        .unwrap();
    assert_eq!(attempted, Some(exhausted + 1));
    assert_eq!(flow.receiver.granted, exhausted);
    assert!(!flow.sender.can_send());
    // Cancellation frees the old reply, without resetting boot-scoped counts.
    flow.buffered = 0;
    let error = flow
        .receiver
        .replenish_with(flow.buffered, |_| Err(io::ErrorKind::BrokenPipe.into()))
        .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::BrokenPipe);
    assert_eq!(flow.receiver.granted, exhausted);
    flow.replenish();
    assert_eq!(flow.receiver.granted, exhausted + 2);
    assert_eq!(flow.receiver.received, exhausted);
    flow.deliver(PACKET_SAMPLES, false); // Late old-owner PCM is counted, not retained.
    flow.deliver(PACKET_SAMPLES, true);
    assert_eq!(flow.buffered, PACKET_SAMPLES);
    assert_eq!(flow.receiver.received, exhausted + 2);
}

#[test]
fn full_thirty_second_reply_is_reachable_without_overgrant_or_overflow() {
    let mut flow = Flow::default();
    flow.fill();
    assert_eq!(flow.sender.sent, 750);
    assert_eq!(flow.receiver.received, 750);
    assert!(
        !flow
            .sender
            .try_send(|| panic!("full reply sent another packet"))
            .unwrap()
    );
    flow.receiver
        .replenish_with(flow.buffered, |_| {
            panic!("unchanged limit must not be resent")
        })
        .unwrap();
}

#[test]
fn partial_playback_frees_credit_only_when_a_whole_worst_case_packet_fits() {
    let mut flow = Flow::default();
    flow.fill();
    for _ in 0..3 {
        flow.buffered -= 240;
        flow.replenish();
        assert!(!flow.sender.can_send());
    }
    flow.buffered -= 240;
    flow.replenish();
    assert_eq!(flow.sender.through - flow.sender.sent, 1);
    flow.deliver(PACKET_SAMPLES - 1, true);
    assert_eq!(flow.buffered, MAX_REPLY_SAMPLES - 1);
    assert!(!flow.sender.can_send());
    flow.buffered -= PACKET_SAMPLES - 1;
    flow.replenish();
    flow.deliver(1, true);
    assert_eq!(flow.buffered, MAX_REPLY_SAMPLES - PACKET_SAMPLES + 1);
    assert!(!flow.sender.can_send());
    // A one-sample packet leaves 959 samples free: no second 960-sample grant.
    flow.buffered -= 1;
    flow.replenish();
    flow.deliver(0, false); // A completion or transcript consumes one packet too.
    assert_eq!(flow.sender.through - flow.sender.sent, 1);
    flow.deliver(PACKET_SAMPLES, true);
    assert_eq!(flow.buffered, MAX_REPLY_SAMPLES);
    assert!(!flow.sender.can_send());
}

#[test]
fn invalid_buffer_accounting_cannot_emit_or_revoke_a_grant() {
    let mut receiver = OutputReceiver::default();
    assert!(
        receiver
            .replenish_with(MAX_REPLY_SAMPLES + 1, |_| panic!(
                "invalid size reached I/O"
            ))
            .is_err()
    );
    // This unexplained occupancy cannot coexist with the two initial promises.
    assert!(
        receiver
            .replenish_with(MAX_REPLY_SAMPLES, |_| panic!(
                "grant regression reached I/O"
            ))
            .is_err()
    );
    assert_eq!((receiver.received, receiver.granted), (0, 2));
}

#[test]
fn counter_exhaustion_fails_before_wrap_or_extra_transport_work() {
    // Only exhaustion fixtures use direct counter setup; normal capacity tests
    // above reach their full buffer by exchanging legal packets.
    let mut sender = OutputSender {
        sent: u64::MAX - 2,
        through: u64::MAX,
    };
    sender.grant(u64::MAX).unwrap();
    assert!(sender.try_send(|| Ok(())).unwrap());
    assert!(sender.grant(u64::MAX).is_err());
    assert!(sender.try_send(|| Ok(())).unwrap());
    assert_eq!(sender.sent, u64::MAX);
    assert!(
        !sender
            .try_send(|| panic!("exhausted counter reached I/O"))
            .unwrap()
    );
    assert!(sender.record_sent().is_err());
    assert!(sender.grant(u64::MAX).is_err());

    let mut receiver = OutputReceiver {
        received: u64::MAX - 1,
        granted: u64::MAX,
    };
    assert!(
        receiver
            .replenish_with(0, |_| panic!("overflowing grant reached I/O"))
            .is_err()
    );
    assert_eq!(receiver.received, u64::MAX - 1);
    receiver.received().unwrap();
    assert!(receiver.received().is_err());
    assert!(
        receiver
            .replenish_with(0, |_| panic!("exhausted grant reached I/O"))
            .is_err()
    );
    assert_eq!((receiver.received, receiver.granted), (u64::MAX, u64::MAX));
}

#[test]
fn varied_packet_sizes_and_consumer_schedules_preserve_all_reservations() {
    let mut flow = Flow::default();
    flow.fill();
    let mut flight = VecDeque::new();
    let mut random = 0x7162_abcdu32;
    let mut accepted = 0;
    let mut refused = 0;
    let mut discarded = 0;
    let mut cancellations = 0;
    for tick in 0..8_192 {
        random ^= random << 13;
        random ^= random >> 17;
        random ^= random << 5;
        match random % 5 {
            0 | 1 => {
                let samples = [0, 1, 239, 240, 959, 960][(random as usize >> 8) % 6];
                let retained = random & 0x10000 != 0;
                let blocked = random & 0x80 != 0;
                let prior = flow.sender.sent;
                let sent = flow
                    .sender
                    .try_send(|| {
                        if blocked {
                            Err(io::ErrorKind::WouldBlock.into())
                        } else {
                            flight.push_back((samples, retained));
                            Ok(())
                        }
                    })
                    .unwrap();
                if sent {
                    accepted += 1;
                    assert_eq!(flow.sender.sent, prior + 1);
                } else {
                    refused += 1;
                    assert_eq!(flow.sender.sent, prior);
                }
            }
            2 => {
                if let Some((samples, retained)) = flight.pop_front() {
                    flow.receiver.received().unwrap();
                    if retained {
                        flow.buffered += samples;
                    } else {
                        discarded += 1;
                    }
                }
            }
            3 => flow.buffered = flow.buffered.saturating_sub((random as usize >> 8) % 481),
            _ => {
                if tick % 17 == 0 {
                    flow.buffered = 0;
                    cancellations += 1;
                }
            }
        }
        let producer = &mut flow.sender;
        flow.receiver
            .replenish_with(flow.buffered, |through| {
                if random & 0x40 != 0 {
                    Err(io::ErrorKind::WouldBlock.into())
                } else {
                    producer.grant(through)
                }
            })
            .unwrap();
        flow.invariants();
        assert_eq!(
            flight.len() as u64,
            flow.sender.sent - flow.receiver.received
        );
        let owed_samples: usize = flight
            .iter()
            .filter(|(_, retained)| *retained)
            .map(|(samples, _)| samples)
            .sum();
        assert!(flow.buffered + owed_samples <= MAX_REPLY_SAMPLES);
    }
    assert!(accepted > 100 && refused > 100 && discarded > 100 && cancellations > 10);
    while let Some((samples, retained)) = flight.pop_front() {
        flow.receiver.received().unwrap();
        if retained {
            flow.buffered += samples;
        }
    }
    flow.replenish();
    assert_eq!(flow.sender.sent, flow.receiver.received);
}

#[test]
fn every_provider_event_consumes_one_credit_including_repeated_ready_and_old_audio() {
    let mut channels = Channels::new();
    let mut flow = Flow::default();
    let events = [
        ProviderOutput::Ready { setup_us: 1 },
        ProviderOutput::Started {
            request: 7,
            waiting_for_barrier: false,
        },
        audio(1, PACKET_SAMPLES),
        ProviderOutput::Transcript {
            request: Some(7),
            text: "reply".into(),
            finished: false,
        },
        ProviderOutput::Transcript {
            request: None,
            text: "input".into(),
            finished: true,
        },
        ProviderOutput::GenerationComplete { request: 7 },
        ProviderOutput::Interrupted { request: 7 },
        ProviderOutput::TurnComplete {
            request: 7,
            idle: false,
        },
        ProviderOutput::TurnComplete {
            request: 7,
            idle: true,
        },
        ProviderOutput::Ready { setup_us: 2 },
        audio(2, 1),
    ];
    for (index, event) in events.iter().enumerate() {
        channels.exchange(&mut flow, event, false);
        let received = index as u64 + 1;
        assert_eq!(
            (flow.sender.sent, flow.receiver.received),
            (received, received)
        );
        assert_eq!(flow.receiver.granted, received + 2);
        assert_eq!(flow.buffered, 0);
    }
}

#[test]
fn zero_credit_pause_does_not_age_unsent_audio_or_block_reverse_input_and_control() {
    let mut channels = Channels::new();
    let mut flow = Flow::default();
    for sequence in 1..=MAX_REPLY_SAMPLES / PACKET_SAMPLES {
        channels.exchange(&mut flow, &audio(sequence as u64, PACKET_SAMPLES), true);
    }
    assert_eq!(flow.buffered, MAX_REPLY_SAMPLES);
    assert_eq!(flow.receiver.received, 750);
    assert!(!flow.sender.can_send());
    assert!(
        channels
            .parent
            .data
            .receive::<ProviderOutput>()
            .unwrap()
            .is_none()
    );
    let next = audio(751, PACKET_SAMPLES);
    let held_at_us = monotonic_us();
    assert!(
        !flow
            .sender
            .try_send(|| panic!("zero credit sent queued audio"))
            .unwrap()
    );
    // This is an intentional application pause, not a retry loop or a latency
    // assertion. Nothing is sitting unread in either IPC direction.
    thread::sleep(Duration::from_millis(120));
    assert!(monotonic_us() - held_at_us >= 100_000);
    assert!(
        channels
            .parent
            .data
            .receive::<ProviderOutput>()
            .unwrap()
            .is_none()
    );

    // The flow counter cannot obstruct the independent urgent channel, nor the
    // opposite microphone direction. Worker authority processing is tested by
    // provider_worker; this test exercises the real transport routes only.
    channels.parent.control.send(Control::Stop).unwrap();
    let input = ProviderInput::Audio {
        request: 8,
        privacy_generation: 1,
        sequence: 1,
        read_completed_at_us: monotonic_us(),
        samples: vec![123; 160],
    };
    channels.parent.data.send(&input).unwrap();
    assert!(matches!(
        channels.child.control.receive::<Control>().unwrap(),
        Some(Control::Stop)
    ));
    assert_eq!(
        serde_json::to_value(
            channels
                .child
                .data
                .receive::<ProviderInput>()
                .unwrap()
                .unwrap()
        )
        .unwrap(),
        serde_json::to_value(input).unwrap()
    );
    assert_eq!((flow.sender.sent, flow.receiver.received), (750, 750));

    flow.buffered -= PACKET_SAMPLES;
    channels.replenish(&mut flow);
    channels.exchange(&mut flow, &next, true);
    assert_eq!(flow.receiver.received, 751);
    assert_eq!(flow.buffered, MAX_REPLY_SAMPLES);
    assert!(!flow.sender.can_send());
}

#[test]
fn output_already_sent_to_ipc_still_expires_without_a_fake_receipt() {
    let mut channels = Channels::new();
    let mut flow = Flow::default();
    let event = audio(1, PACKET_SAMPLES);
    assert!(
        flow.sender
            .try_send(|| channels.child.data.send(&event))
            .unwrap()
    );
    thread::sleep(Duration::from_millis(120));
    let error = channels
        .parent
        .data
        .receive::<ProviderOutput>()
        .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    // A rejected envelope must never be acknowledged by the output receiver.
    assert_eq!(
        (
            flow.sender.sent,
            flow.receiver.received,
            flow.receiver.granted
        ),
        (1, 0, 2)
    );
}
