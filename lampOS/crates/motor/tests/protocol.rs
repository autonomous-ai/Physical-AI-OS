use lamp_motor::{
    Joint,
    protocol::{
        MAX_PACKET_BYTES, MAX_STATUS_PARAMETERS, ParseError, ReadOnlyRequest, ReadWindow,
        ReplyError, RequestError, StatusFlags, StatusPacket, StatusParser,
    },
};

fn status(id: u8, flags: u8, data: &[u8]) -> Vec<u8> {
    let mut bytes = vec![0xff, 0xff, id, (data.len() + 2) as u8, flags];
    bytes.extend_from_slice(data);
    let sum: u32 = bytes[2..].iter().map(|&byte| u32::from(byte)).sum();
    bytes.push(!(sum as u8));
    bytes
}

fn parse(bytes: &[u8]) -> Vec<Result<StatusPacket, ParseError>> {
    let mut events = Vec::new();
    let mut parser = StatusParser::default();
    parser.feed(bytes, |event| events.push(event));
    if let Err(error) = parser.finish() {
        events.push(Err(error));
    }
    events
}

fn packet(id: u8, flags: u8, data: &[u8]) -> StatusPacket {
    let mut events = parse(&status(id, flags, data));
    assert_eq!(events.len(), 1);
    events.remove(0).unwrap()
}

#[test]
fn five_joint_identities_are_exact() {
    let names = [
        "base_yaw",
        "base_pitch",
        "elbow_pitch",
        "wrist_roll",
        "wrist_pitch",
    ];
    for raw in 0..=u8::MAX {
        match Joint::try_from(raw) {
            Ok(joint) => {
                assert!((1..=5).contains(&raw));
                assert_eq!(joint.id(), raw);
                assert_eq!(joint.name(), names[usize::from(raw - 1)]);
                assert_eq!(Joint::ALL[joint.index()], joint);
            }
            Err(_) => assert!(!(1..=5).contains(&raw)),
        }
    }
}

#[test]
fn golden_read_only_packets_match_sts_little_endian_protocol() {
    assert_eq!(
        ReadOnlyRequest::ping(Joint::BaseYaw).bytes(),
        &[0xff, 0xff, 1, 2, 1, 0xfb]
    );
    assert_eq!(
        ReadOnlyRequest::read(Joint::BaseYaw, ReadWindow::MODEL_NUMBER).bytes(),
        &[0xff, 0xff, 1, 4, 2, 3, 2, 0xf3]
    );
    assert_eq!(
        ReadOnlyRequest::read(Joint::BaseYaw, ReadWindow::TELEMETRY).bytes(),
        &[0xff, 0xff, 1, 4, 2, 56, 15, 0xb1]
    );
    assert_eq!(
        ReadOnlyRequest::sync_read(&Joint::ALL, ReadWindow::TELEMETRY)
            .unwrap()
            .bytes(),
        &[0xff, 0xff, 0xfe, 9, 0x82, 56, 15, 1, 2, 3, 4, 5, 0x20]
    );
    for joint in Joint::ALL {
        assert_eq!(ReadOnlyRequest::ping(joint).bytes()[5], !(joint.id() + 3));
    }
}

#[test]
fn every_read_window_is_bounded_without_address_wrap() {
    for address in 0..=u8::MAX {
        for count in 0..=u8::MAX {
            let allowed =
                (1..=244).contains(&count) && u16::from(address) + u16::from(count) <= 256;
            let window = ReadWindow::new(address, count);
            assert_eq!(window.is_ok(), allowed, "{address}+{count}");
            if let Ok(window) = window {
                assert_eq!(window.address(), address);
                assert_eq!(window.byte_count(), count);
                assert_eq!(
                    &ReadOnlyRequest::read(Joint::WristPitch, window).bytes()[5..7],
                    &[address, count]
                );
            }
        }
    }
}

#[test]
fn sync_read_rejects_empty_duplicate_or_excessive_ids() {
    assert!(matches!(
        ReadOnlyRequest::sync_read(&[], ReadWindow::TELEMETRY),
        Err(RequestError::InvalidJointCount(0))
    ));
    assert!(matches!(
        ReadOnlyRequest::sync_read(&[Joint::BaseYaw, Joint::BaseYaw], ReadWindow::TELEMETRY),
        Err(RequestError::DuplicateJoint(Joint::BaseYaw))
    ));
    assert!(matches!(
        ReadOnlyRequest::sync_read(&[Joint::BaseYaw; 6], ReadWindow::TELEMETRY),
        Err(RequestError::InvalidJointCount(6))
    ));
    let request = ReadOnlyRequest::sync_read(
        &[Joint::WristPitch, Joint::BaseYaw],
        ReadWindow::MODEL_NUMBER,
    )
    .unwrap();
    assert_eq!(&request.bytes()[7..9], &[5, 1]);
    assert_eq!(request.bytes().len(), 10);
    assert_eq!(request.bytes()[3], 6);
}

#[test]
fn golden_status_survives_every_split_and_empty_feed() {
    let bytes = [0xff, 0xff, 1, 4, 0, 9, 3, 0xee];
    for split in 0..=bytes.len() {
        let mut parser = StatusParser::default();
        let mut events = Vec::new();
        parser.feed(&bytes[..split], |event| events.push(event));
        parser.feed(&[], |event| events.push(event));
        parser.feed(&bytes[split..], |event| events.push(event));
        assert_eq!(parser.finish(), Ok(()));
        assert_eq!(events.len(), 1);
        let frame = events.remove(0).unwrap();
        assert_eq!(frame.raw_id(), 1);
        assert_eq!(frame.flags().bits(), 0);
        assert_eq!(frame.parameters(), &[9, 3]);
    }
}

#[test]
fn maximum_status_survives_all_chunk_sizes_and_wrapping_checksum() {
    let payload: Vec<u8> = (0..MAX_STATUS_PARAMETERS).map(|n| n as u8).collect();
    let bytes = status(5, 0, &payload);
    assert_eq!(bytes.len(), MAX_PACKET_BYTES);
    for chunk_size in 1..=bytes.len() {
        let mut parser = StatusParser::default();
        let mut events = Vec::new();
        for chunk in bytes.chunks(chunk_size) {
            parser.feed(chunk, |event| events.push(event));
            assert!(parser.buffered_bytes() < MAX_PACKET_BYTES);
        }
        assert_eq!(parser.finish(), Ok(()));
        assert_eq!(events.len(), 1);
        assert_eq!(events.remove(0).unwrap().parameters(), &payload);
    }
}

#[test]
fn bytewise_multiservo_replies_complete_in_any_arrival_rotation() {
    for offset in 0..5 {
        let request = ReadOnlyRequest::sync_read(&Joint::ALL, ReadWindow::MODEL_NUMBER).unwrap();
        let mut tracker = request.track_replies();
        let mut parser = StatusParser::default();
        let mut received = Vec::new();
        for index in 0..5 {
            let joint = Joint::ALL[(index + offset) % 5];
            for byte in status(joint.id(), 0, &[9, 3]) {
                parser.feed(&[byte], |event| {
                    let reply = tracker.accept(event).unwrap();
                    assert_eq!(reply.model_number_raw().unwrap(), 777);
                    received.push(reply.joint());
                });
            }
        }
        assert_eq!(received.len(), 5);
        assert_eq!(tracker.pending_joints().count(), 0);
        assert!(tracker.is_complete());
        assert_eq!(parser.finish(), Ok(()));
        assert_eq!(tracker.finish(), Ok(()));
    }
}

#[test]
fn concatenated_frames_and_noise_are_not_lost() {
    let mut bytes = vec![0, 0xff, 0, 0x55, 0xaa];
    for joint in Joint::ALL {
        bytes.extend(status(joint.id(), 0, &[]));
    }
    let mut parser = StatusParser::default();
    let mut packets = Vec::new();
    let report = parser.feed(&bytes, |event| packets.push(event.unwrap()));
    assert_eq!(report.packets, 5);
    assert_eq!(report.discarded_bytes, 5);
    assert_eq!(report.malformed, 0);
    assert_eq!(
        packets.iter().map(StatusPacket::raw_id).collect::<Vec<_>>(),
        vec![1, 2, 3, 4, 5]
    );
    assert_eq!(parser.finish(), Ok(()));
}

#[test]
fn every_invalid_length_is_rejected_and_parser_resynchronizes() {
    for length in (0..=u8::MAX).filter(|length| !(2..=246).contains(length)) {
        let mut bytes = vec![0xff, 0xff, 1, length];
        bytes.extend(status(2, 0, &[]));
        let events = parse(&bytes);
        assert!(events.contains(&Err(ParseError::InvalidLength(length))));
        assert!(
            events
                .iter()
                .any(|event| event.as_ref().is_ok_and(|frame| frame.raw_id() == 2))
        );
    }
}

#[test]
fn invalid_broadcast_status_and_high_status_bit_are_explicit() {
    for id in [0xfe, 0xff] {
        assert!(parse(&status(id, 0, &[])).contains(&Err(ParseError::InvalidId(id))));
    }
    for flags in 0x80..=u8::MAX {
        assert!(parse(&status(1, flags, &[])).contains(&Err(ParseError::InvalidStatusByte(flags))));
    }
    // Non-Lamp unicast IDs remain visible in the framing layer for diagnostics.
    assert_eq!(packet(253, 0, &[]).raw_id(), 253);
}

#[test]
fn every_single_bit_payload_corruption_is_detected() {
    let good = status(
        1,
        0,
        &[
            9, 3, 44, 55, 66, 77, 88, 99, 110, 121, 132, 143, 154, 165, 176,
        ],
    );
    for index in 5..good.len() {
        for bit in 0..8 {
            let mut corrupt = good.clone();
            corrupt[index] ^= 1 << bit;
            let events = parse(&corrupt);
            assert!(
                events
                    .iter()
                    .any(|event| matches!(event, Err(ParseError::Checksum { .. })))
            );
            assert!(events.iter().all(Result::is_err));
        }
    }
}

#[test]
fn resynchronization_retains_complete_frame_inside_corrupt_candidate() {
    let inner = status(1, 0, &[]);
    let mut outer = status(2, 0, &inner);
    *outer.last_mut().unwrap() ^= 1;
    let events = parse(&outer);
    assert!(matches!(events[0], Err(ParseError::Checksum { .. })));
    assert!(
        events
            .iter()
            .any(|event| event.as_ref().is_ok_and(|frame| frame.raw_id() == 1))
    );
    // A recovered packet must not make the damaged transaction successful.
    let mut tracker = ReadOnlyRequest::ping(Joint::BaseYaw).track_replies();
    for event in events {
        assert!(tracker.accept(event).is_err());
    }
    assert!(matches!(tracker.finish(), Err(ReplyError::Malformed(_))));
}

#[test]
fn every_partial_prefix_is_reported_and_discarded_at_deadline() {
    let bytes = status(1, 0, &[9, 3]);
    for prefix in 1..bytes.len() {
        let mut parser = StatusParser::default();
        parser.feed(&bytes[..prefix], |_| {
            panic!("partial packet emitted an event")
        });
        assert_eq!(
            parser.finish(),
            Err(ParseError::Truncated {
                buffered: prefix,
                expected: (prefix >= 4).then_some(bytes.len())
            })
        );
        assert_eq!(parser.buffered_bytes(), 0);
        let mut count = 0;
        parser.feed(&bytes, |event| {
            assert!(event.is_ok());
            count += 1;
        });
        assert_eq!(count, 1);
    }
}

#[test]
fn false_long_length_requires_deadline_not_guessing_payload_headers() {
    let mut parser = StatusParser::default();
    parser.feed(&[0xff, 0xff, 1, 246, 0], |_| panic!("unexpected event"));
    parser.feed(&status(2, 0, &[]), |_| {
        panic!("payload bytes cannot be assumed to be a new header")
    });
    assert_eq!(
        parser.finish(),
        Err(ParseError::Truncated {
            buffered: 11,
            expected: Some(250)
        })
    );
    assert_eq!(parser.buffered_bytes(), 0);
}

#[test]
fn arbitrary_byte_streams_retain_bounded_storage_and_can_be_reset() {
    let mut parser = StatusParser::default();
    let mut seed = 0x1234_5678u32;
    for _ in 0..200_000 {
        seed = seed.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
        parser.feed(&[(seed >> 24) as u8], |_| {});
        assert!(parser.buffered_bytes() < MAX_PACKET_BYTES);
    }
    parser.feed(&[0xff; 2048], |_| {});
    assert!(parser.buffered_bytes() < MAX_PACKET_BYTES);
    parser.reset();
    assert_eq!(parser.finish(), Ok(()));
    assert_eq!(parse(&status(1, 0, &[])).len(), 1);
}

#[test]
fn all_nonzero_servo_flags_fail_even_if_error_reply_has_no_read_data() {
    for flags in 1..=0x7f {
        let mut tracker =
            ReadOnlyRequest::read(Joint::BaseYaw, ReadWindow::TELEMETRY).track_replies();
        let error = tracker.accept(Ok(packet(1, flags, &[]))).unwrap_err();
        let ReplyError::ServoFault {
            joint,
            flags: parsed,
        } = error
        else {
            panic!("lost device error")
        };
        assert_eq!(joint, Joint::BaseYaw);
        assert_eq!(parsed.bits(), flags);
        assert_eq!(parsed.unknown_bits(), flags & !StatusFlags::KNOWN_MASK);
        assert_eq!(tracker.accept(Ok(packet(1, 0, &[0; 15]))), Err(error));
        assert!(!tracker.is_complete());
        assert_eq!(tracker.finish(), Err(error));
    }
}

#[test]
fn duplicate_and_unrequested_ids_latch_failure() {
    let mut tracker = ReadOnlyRequest::ping(Joint::BaseYaw).track_replies();
    tracker.accept(Ok(packet(1, 0, &[]))).unwrap();
    assert!(tracker.is_complete());
    assert_eq!(
        tracker.accept(Ok(packet(1, 0, &[]))),
        Err(ReplyError::Duplicate(Joint::BaseYaw))
    );
    assert!(!tracker.is_complete());
    for id in [0, 2, 42, 253] {
        let mut tracker = ReadOnlyRequest::ping(Joint::BaseYaw).track_replies();
        assert_eq!(
            tracker.accept(Ok(packet(id, 0, &[]))),
            Err(ReplyError::UnexpectedId(id))
        );
        assert_eq!(
            tracker.accept(Ok(packet(1, 0, &[]))),
            Err(ReplyError::UnexpectedId(id))
        );
    }
}

#[test]
fn wrong_parameter_count_and_missing_joint_are_not_success() {
    for count in [0, 1, 3, 244] {
        let mut tracker =
            ReadOnlyRequest::read(Joint::WristPitch, ReadWindow::MODEL_NUMBER).track_replies();
        assert_eq!(
            tracker.accept(Ok(packet(5, 0, &vec![0; count]))),
            Err(ReplyError::ParameterCount {
                joint: Joint::WristPitch,
                expected: 2,
                received: count
            })
        );
    }
    let mut tracker = ReadOnlyRequest::sync_read(&Joint::ALL, ReadWindow::MODEL_NUMBER)
        .unwrap()
        .track_replies();
    for id in [1, 2, 4, 5] {
        tracker.accept(Ok(packet(id, 0, &[9, 3]))).unwrap();
    }
    assert_eq!(
        tracker.pending_joints().collect::<Vec<_>>(),
        vec![Joint::ElbowPitch]
    );
    assert_eq!(
        tracker.finish(),
        Err(ReplyError::MissingReplies {
            joint_mask: 0b00100
        })
    );
}

#[test]
fn parser_truncation_can_latch_an_otherwise_complete_transaction_failed() {
    let mut tracker = ReadOnlyRequest::ping(Joint::BaseYaw).track_replies();
    let mut parser = StatusParser::default();
    parser.feed(&status(1, 0, &[]), |event| {
        tracker.accept(event).unwrap();
    });
    parser.feed(&[0xff], |_| panic!("incomplete header"));
    let error = parser.finish().unwrap_err();
    assert_eq!(
        tracker.accept(Err(error)),
        Err(ReplyError::Malformed(error))
    );
    assert_eq!(tracker.finish(), Err(ReplyError::Malformed(error)));
}
