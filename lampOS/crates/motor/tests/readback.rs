use lamp_motor::{
    Joint,
    protocol::{MatchedReply, ReadOnlyRequest, ReadWindow, StatusParser},
    readback::DecodeError,
    units::UnitError,
};

fn reply(request: ReadOnlyRequest, id: u8, data: &[u8]) -> MatchedReply {
    let mut bytes = vec![0xff, 0xff, id, (data.len() + 2) as u8, 0];
    bytes.extend(data);
    let sum: u32 = bytes[2..].iter().map(|&byte| u32::from(byte)).sum();
    bytes.push(!(sum as u8));
    let mut tracker = request.track_replies();
    let mut result = None;
    let mut parser = StatusParser::default();
    parser.feed(&bytes, |event| {
        result = Some(tracker.accept(event).unwrap())
    });
    parser.finish().unwrap();
    tracker.finish().unwrap();
    result.unwrap()
}

fn read(window: ReadWindow, data: &[u8]) -> MatchedReply {
    reply(ReadOnlyRequest::read(Joint::WristPitch, window), 5, data)
}

#[test]
fn register_decoding_requires_matching_request_address_not_only_length() {
    let ping = reply(ReadOnlyRequest::ping(Joint::WristPitch), 5, &[]);
    assert!(matches!(
        ping.model_number_raw(),
        Err(DecodeError::WrongWindow { .. })
    ));
    let model = read(ReadWindow::MODEL_NUMBER, &[9, 3]);
    assert_eq!(model.model_number_raw().unwrap(), 777);
    assert!(model.telemetry().is_err());
    let wrong_word = read(ReadWindow::new(56, 2).unwrap(), &[9, 3]);
    assert!(wrong_word.model_number_raw().is_err());
    let wrong_block = read(ReadWindow::new(55, 15).unwrap(), &[0; 15]);
    assert!(wrong_block.telemetry().is_err());
}

#[test]
fn telemetry_fields_use_little_endian_offsets_and_preserve_raw_scales() {
    let bytes = [
        0x00, 0x08, 0x01, 0x80, 0xff, 0x87, 0x78, 0x2a, 0xa1, 0x80, 0x02, 0xb2, 0xc3, 0x34, 0x12,
    ];
    let telemetry = read(ReadWindow::TELEMETRY, &bytes).telemetry().unwrap();
    assert_eq!(telemetry.joint(), Joint::WristPitch);
    assert_eq!(telemetry.bytes(), &bytes);
    assert_eq!(telemetry.position().raw(), 2048);
    assert_eq!(telemetry.velocity().word(), 0x8001);
    assert_eq!(telemetry.velocity().signed_raw(), -1);
    assert_eq!(telemetry.load().word(), 0x87ff);
    assert_eq!(telemetry.load().magnitude_raw(), 1023);
    assert!(telemetry.load().direction_bit());
    assert_eq!(telemetry.load().uninterpreted_bits(), 0x8000);
    assert_eq!(telemetry.voltage_raw(), 120);
    assert_eq!(telemetry.temperature_raw(), 42);
    assert_eq!(telemetry.status_raw(), 0x80);
    assert_eq!(telemetry.moving_raw(), 2);
    assert_eq!(telemetry.current_raw(), 0x1234);
}

#[test]
fn signed_velocity_extremes_keep_sign_magnitude_and_negative_zero_semantics() {
    for (word, value) in [
        (0u16, 0),
        (1, 1),
        (0x7fff, 32767),
        (0x8000, 0),
        (0x8001, -1),
        (0xffff, -32767),
    ] {
        let mut bytes = [0; 15];
        bytes[2..4].copy_from_slice(&word.to_le_bytes());
        let velocity = read(ReadWindow::TELEMETRY, &bytes)
            .telemetry()
            .unwrap()
            .velocity();
        assert_eq!(velocity.word(), word);
        assert_eq!(velocity.signed_raw(), value);
    }
}

#[test]
fn out_of_range_raw_position_is_kept_for_diagnosis_not_normalized() {
    let mut bytes = [0; 15];
    bytes[..2].copy_from_slice(&0xffffu16.to_le_bytes());
    let position = read(ReadWindow::TELEMETRY, &bytes)
        .telemetry()
        .unwrap()
        .position();
    assert_eq!(position.raw(), 65535);
    assert!(position.single_turn_counts().is_err());
    let limits = read(ReadWindow::POSITION_LIMITS, &[0xff, 0xff, 0, 0])
        .position_limits()
        .unwrap();
    assert_eq!(limits.min().raw(), 65535);
    assert_eq!(limits.max().raw(), 0);
}

#[test]
fn homing_register_validates_reserved_bits_and_preserves_mode_for_checking() {
    let registers = read(ReadWindow::HOMING_AND_MODE, &[0x01, 0x08, 3])
        .homing_and_mode()
        .unwrap();
    assert_eq!(registers.joint(), Joint::WristPitch);
    assert_eq!(registers.homing_word(), 0x0801);
    assert_eq!(registers.homing_offset().get(), -1);
    assert_eq!(registers.operating_mode_raw(), 3);
    assert_eq!(
        read(ReadWindow::HOMING_AND_MODE, &[0, 0x10, 0]).homing_and_mode(),
        Err(DecodeError::Units(UnitError::HomingReservedBits(0x1000)))
    );
}
