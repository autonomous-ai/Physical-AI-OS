use lamp_motor::{
    Joint,
    calibration::{CalibrationError, CalibrationRecord, DriveMode, UnitCalibration, UnitId},
    protocol::{MatchedReply, ReadOnlyRequest, ReadWindow, StatusParser},
    units::{EncoderCounts, EncoderWord, HomingOffset, NormalizedPosition, UnitError},
};

fn records() -> [CalibrationRecord; 5] {
    Joint::ALL.map(|joint| CalibrationRecord {
        joint,
        id: u16::from(joint.id()),
        drive_mode: 0,
        homing_offset: 333,
        range_min: 1000,
        range_max: 3000,
    })
}

fn unit() -> UnitId {
    UnitId::new("synthetic-unit-a").unwrap()
}

fn matched(joint: Joint, window: ReadWindow, data: &[u8]) -> MatchedReply {
    let mut bytes = vec![0xff, 0xff, joint.id(), (data.len() + 2) as u8, 0];
    bytes.extend(data);
    let sum: u32 = bytes[2..].iter().map(|&byte| u32::from(byte)).sum();
    bytes.push(!(sum as u8));
    let mut tracker = ReadOnlyRequest::read(joint, window).track_replies();
    let mut result = None;
    let mut parser = StatusParser::default();
    parser.feed(&bytes, |event| {
        result = Some(tracker.accept(event).unwrap())
    });
    parser.finish().unwrap();
    tracker.finish().unwrap();
    result.unwrap()
}

#[test]
fn raw_register_words_do_not_silently_become_single_turn_counts() {
    for value in 0..=4095 {
        assert_eq!(EncoderCounts::new(value).unwrap().get(), value as u16);
        assert_eq!(
            EncoderWord::from_raw(value as u16)
                .single_turn_counts()
                .unwrap()
                .get(),
            value as u16
        );
    }
    for value in [-1, 4096, 65535, i32::MIN, i32::MAX] {
        assert_eq!(
            EncoderCounts::new(value),
            Err(UnitError::EncoderOutOfRange(value))
        );
    }
    assert_eq!(EncoderWord::from_raw(0xffff).raw(), 0xffff);
    assert!(EncoderWord::from_raw(0xffff).single_turn_counts().is_err());
}

#[test]
fn normalized_positions_reject_nonfinite_and_out_of_span_input() {
    for value in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        assert_eq!(
            NormalizedPosition::new(value),
            Err(UnitError::NormalizedNotFinite)
        );
    }
    for value in [-100.000_000_01, 100.000_000_01, f64::MAX, f64::MIN] {
        assert_eq!(
            NormalizedPosition::new(value),
            Err(UnitError::NormalizedOutOfRange)
        );
    }
    for value in [-100.0, -0.0, 0.0, 0.001, 100.0] {
        assert_eq!(NormalizedPosition::new(value).unwrap().get(), value);
    }
}

#[test]
fn every_homing_value_roundtrips_sign_magnitude_not_twos_complement() {
    for value in -2047..=2047 {
        let offset = HomingOffset::new(value).unwrap();
        assert_eq!(
            HomingOffset::from_register(offset.register_word())
                .unwrap()
                .get(),
            value as i16
        );
    }
    assert_eq!(HomingOffset::new(-1).unwrap().register_word(), 0x0801);
    assert_eq!(HomingOffset::from_register(0x0fff).unwrap().get(), -2047);
    assert_eq!(HomingOffset::from_register(0x0800).unwrap().get(), 0);
    assert_eq!(
        HomingOffset::from_register(0x0800).unwrap().register_word(),
        0
    );
    for value in [-2048, 2048, i32::MIN, i32::MAX] {
        assert_eq!(
            HomingOffset::new(value),
            Err(UnitError::HomingOutOfRange(value))
        );
    }
    for word in 0x1000..=u16::MAX {
        assert_eq!(
            HomingOffset::from_register(word),
            Err(UnitError::HomingReservedBits(word))
        );
    }
}

#[test]
fn calibration_is_explicitly_bound_to_a_unit_and_five_unique_joints() {
    let mut input = records();
    input.reverse();
    let calibration = UnitCalibration::new(unit(), input).unwrap();
    assert!(matches!(
        calibration.bind(&UnitId::new("synthetic-unit-b").unwrap()),
        Err(CalibrationError::UnitIdentityMismatch)
    ));
    let bound = calibration.bind(&unit()).unwrap();
    for joint in Joint::ALL {
        assert_eq!(bound.joint(joint).joint(), joint);
    }
    input[0] = input[1];
    assert!(matches!(
        UnitCalibration::new(unit(), input),
        Err(CalibrationError::DuplicateJoint(Joint::WristRoll))
    ));
}

#[test]
fn invalid_or_unbounded_unit_identifiers_are_rejected() {
    for id in ["", "a/b", "a b", "a\n", "a\0b", "é"] {
        assert!(UnitId::new(id).is_err());
    }
    assert!(UnitId::new(&"a".repeat(65)).is_err());
    assert_eq!(
        UnitId::new(&"a".repeat(64)).unwrap().as_str(),
        "a".repeat(64)
    );
    assert_eq!(
        UnitId::new("lamp:unit_4ace-test.1").unwrap().as_str(),
        "lamp:unit_4ace-test.1"
    );
}

#[test]
fn identity_sign_offset_and_span_validation_cannot_use_fallbacks() {
    let invalid = [
        CalibrationRecord {
            id: 5,
            ..records()[0]
        },
        CalibrationRecord {
            drive_mode: 2,
            ..records()[0]
        },
        CalibrationRecord {
            drive_mode: 255,
            ..records()[0]
        },
        CalibrationRecord {
            homing_offset: -2048,
            ..records()[0]
        },
        CalibrationRecord {
            homing_offset: 2048,
            ..records()[0]
        },
        CalibrationRecord {
            range_min: -1,
            ..records()[0]
        },
        CalibrationRecord {
            range_max: 4096,
            ..records()[0]
        },
        CalibrationRecord {
            range_min: 3000,
            ..records()[0]
        },
        CalibrationRecord {
            range_min: 3001,
            ..records()[0]
        },
    ];
    for record in invalid {
        let mut input = records();
        input[0] = record;
        assert!(UnitCalibration::new(unit(), input).is_err(), "{record:?}");
    }
}

#[test]
fn midpoint_and_endpoints_are_dimensionless_and_do_not_apply_homing_twice() {
    for homing_offset in [-2047, -333, 0, 333, 2047] {
        let input = records().map(|record| CalibrationRecord {
            homing_offset,
            ..record
        });
        let calibration = UnitCalibration::new(unit(), input).unwrap();
        let joint = calibration.bind(&unit()).unwrap().joint(Joint::BaseYaw);
        assert_eq!(joint.homing_offset().get(), homing_offset as i16);
        for (count, normalized) in [
            (1000, -100.0),
            (1500, -50.0),
            (2000, 0.0),
            (2500, 50.0),
            (3000, 100.0),
        ] {
            assert_eq!(
                joint
                    .normalize(EncoderCounts::new(count).unwrap())
                    .unwrap()
                    .get(),
                normalized
            );
            assert_eq!(
                joint
                    .denormalize(NormalizedPosition::new(normalized).unwrap())
                    .get(),
                count as u16
            );
        }
    }
}

#[test]
fn drive_mode_reverses_the_normalized_sign_in_both_directions() {
    let input = records().map(|record| CalibrationRecord {
        drive_mode: 1,
        ..record
    });
    let calibration = UnitCalibration::new(unit(), input).unwrap();
    for joint in Joint::ALL {
        let joint = calibration.bind(&unit()).unwrap().joint(joint);
        assert_eq!(joint.drive_mode(), DriveMode::Reversed);
        assert_eq!(
            joint
                .normalize(EncoderCounts::new(1000).unwrap())
                .unwrap()
                .get(),
            100.0
        );
        assert_eq!(
            joint
                .normalize(EncoderCounts::new(3000).unwrap())
                .unwrap()
                .get(),
            -100.0
        );
        assert_eq!(
            joint
                .denormalize(NormalizedPosition::new(25.0).unwrap())
                .get(),
            1750
        );
    }
}

#[test]
fn observed_counts_outside_calibration_are_faults_not_clipped_endpoints() {
    let calibration = UnitCalibration::new(unit(), records()).unwrap();
    let joint = calibration.bind(&unit()).unwrap().joint(Joint::BaseYaw);
    for count in [0, 999, 3001, 4095] {
        assert!(matches!(
            joint.normalize(EncoderCounts::new(count).unwrap()),
            Err(CalibrationError::PositionOutsideSpan { .. })
        ));
    }
}

#[test]
fn full_and_narrow_spans_are_monotonic_and_quantization_is_bounded() {
    for (min, max) in [(0, 4095), (1000, 1001), (2047, 4095), (1, 4094)] {
        for drive_mode in [0, 1] {
            let input = records().map(|record| CalibrationRecord {
                range_min: min,
                range_max: max,
                drive_mode,
                ..record
            });
            let calibration = UnitCalibration::new(unit(), input).unwrap();
            let joint = calibration.bind(&unit()).unwrap().joint(Joint::ElbowPitch);
            let mut previous = None;
            for count in min..=max {
                let normalized = joint.normalize(EncoderCounts::new(count).unwrap()).unwrap();
                if let Some(previous) = previous {
                    if drive_mode == 0 {
                        assert!(normalized.get() > previous);
                    } else {
                        assert!(normalized.get() < previous);
                    }
                }
                previous = Some(normalized.get());
                let roundtrip = i32::from(joint.denormalize(normalized).get());
                assert!((roundtrip - count).abs() <= 1);
                assert!((min..=max).contains(&roundtrip));
            }
        }
    }
    // The SDK truncates a positive fractional count rather than rounding it.
    let input = records().map(|record| CalibrationRecord {
        range_min: 0,
        range_max: 4095,
        ..record
    });
    let calibration = UnitCalibration::new(unit(), input).unwrap();
    assert_eq!(
        calibration
            .bind(&unit())
            .unwrap()
            .joint(Joint::BaseYaw)
            .denormalize(NormalizedPosition::new(0.0).unwrap())
            .get(),
        2047
    );
}

#[test]
fn readback_comparison_checks_joint_mode_span_and_homing_without_writes() {
    let calibration = UnitCalibration::new(unit(), records()).unwrap();
    let joint = calibration.bind(&unit()).unwrap().joint(Joint::BaseYaw);
    let limits = |id, bytes: &[u8]| {
        matched(id, ReadWindow::POSITION_LIMITS, bytes)
            .position_limits()
            .unwrap()
    };
    let homing = |id, bytes: &[u8]| {
        matched(id, ReadWindow::HOMING_AND_MODE, bytes)
            .homing_and_mode()
            .unwrap()
    };
    let good_limits = limits(Joint::BaseYaw, &[0xe8, 3, 0xb8, 11]);
    let good_homing = homing(Joint::BaseYaw, &[0x4d, 1, 0]);
    assert_eq!(joint.compare_registers(good_limits, good_homing), Ok(()));
    assert!(matches!(
        joint.compare_registers(limits(Joint::BasePitch, &[0xe8, 3, 0xb8, 11]), good_homing),
        Err(CalibrationError::ReadbackJoint { .. })
    ));
    assert!(matches!(
        joint.compare_registers(good_limits, homing(Joint::WristPitch, &[0x4d, 1, 0])),
        Err(CalibrationError::ReadbackJoint { .. })
    ));
    assert!(matches!(
        joint.compare_registers(good_limits, homing(Joint::BaseYaw, &[0x4d, 1, 1])),
        Err(CalibrationError::OperatingMode { .. })
    ));
    assert!(matches!(
        joint.compare_registers(limits(Joint::BaseYaw, &[0xe9, 3, 0xb8, 11]), good_homing),
        Err(CalibrationError::ReadbackSpan { .. })
    ));
    assert!(matches!(
        joint.compare_registers(good_limits, homing(Joint::BaseYaw, &[0x4c, 1, 0])),
        Err(CalibrationError::ReadbackHoming { .. })
    ));
}
