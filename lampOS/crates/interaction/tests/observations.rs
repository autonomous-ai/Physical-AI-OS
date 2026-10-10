use lamp_interaction::{
    BootId, Confidence, Error, Freshness, MonoTime, Observation, ObservationTracker,
};
use std::num::NonZeroU64;

fn boot(value: u8) -> BootId {
    BootId::new([value; 16]).unwrap()
}
fn t(us: u64) -> MonoTime {
    MonoTime::from_micros(us)
}
fn observation(source: u8, sequence: u64, at: u64, confidence: Confidence) -> Observation {
    Observation::new(
        boot(source),
        NonZeroU64::new(sequence).unwrap(),
        t(at),
        confidence,
    )
}

#[test]
fn fresh_stale_unknown_and_uncertain_are_distinct_without_reusing_old_good_evidence() {
    let mut tracker = ObservationTracker::new(boot(1));
    assert_eq!(tracker.freshness(t(0), 1_000, 800), Ok(Freshness::Missing));
    tracker
        .record(
            t(10),
            observation(1, 1, 5, Confidence::per_mille(950).unwrap()),
        )
        .unwrap();
    assert_eq!(
        tracker.freshness(t(15), 1_000, 800),
        Ok(Freshness::Fresh {
            age_us: 10,
            confidence: 950
        })
    );
    assert_eq!(
        tracker.freshness(t(1_005), 1_000, 800),
        Ok(Freshness::Fresh {
            age_us: 1_000,
            confidence: 950
        })
    );
    assert_eq!(
        tracker.freshness(t(1_006), 1_000, 800),
        Ok(Freshness::Stale { age_us: 1_001 })
    );
    tracker
        .record(t(2_000), observation(1, 2, 1_999, Confidence::UNKNOWN))
        .unwrap();
    assert_eq!(
        tracker.freshness(t(2_001), 1_000, 800),
        Ok(Freshness::Unknown { age_us: 2 })
    );
    tracker
        .record(
            t(2_010),
            observation(1, 3, 2_009, Confidence::per_mille(300).unwrap()),
        )
        .unwrap();
    assert_eq!(
        tracker.freshness(t(2_011), 1_000, 800),
        Ok(Freshness::Uncertain {
            age_us: 2,
            confidence: 300
        })
    );
}

#[test]
fn old_sequences_regressing_acquisition_and_wrong_worker_do_not_overwrite_new_evidence() {
    let mut tracker = ObservationTracker::new(boot(1));
    let current = observation(1, 10, 100, Confidence::per_mille(900).unwrap());
    tracker.record(t(101), current).unwrap();
    assert_eq!(
        tracker.record(t(102), current),
        Err(Error::OutOfOrderObservation)
    );
    assert_eq!(
        tracker.record(t(103), observation(1, 9, 102, Confidence::UNKNOWN)),
        Err(Error::OutOfOrderObservation)
    );
    assert_eq!(
        tracker.record(t(104), observation(1, 11, 99, Confidence::UNKNOWN)),
        Err(Error::OutOfOrderObservation)
    );
    assert_eq!(
        tracker.record(t(105), observation(2, 11, 105, Confidence::UNKNOWN)),
        Err(Error::WrongObservationSource)
    );
    assert_eq!(
        tracker.record(t(106), observation(1, 11, 107, Confidence::UNKNOWN)),
        Err(Error::FutureObservation)
    );
    assert_eq!(tracker.latest(), Some(current));
    let mut restarted = ObservationTracker::new(boot(2));
    assert_eq!(
        restarted.record(t(107), current),
        Err(Error::WrongObservationSource)
    );
    assert_eq!(restarted.latest(), None);
}

#[test]
fn invalid_confidence_zero_sequence_and_unbounded_wire_shapes_are_rejected() {
    assert_eq!(Confidence::per_mille(1_001), Err(Error::InvalidConfidence));
    assert!(serde_json::from_str::<Confidence>("1001").is_err());
    let value = observation(1, 1, 10, Confidence::UNKNOWN);
    let mut wire = serde_json::to_value(value).unwrap();
    wire["sequence"] = serde_json::json!(0);
    assert!(serde_json::from_value::<Observation>(wire).is_err());
    let mut tracker = ObservationTracker::new(boot(1));
    assert_eq!(
        tracker.freshness(t(10), 0, 0),
        Err(Error::InvalidFreshnessPolicy)
    );
    assert_eq!(
        tracker.freshness(t(10), 1, 1_001),
        Err(Error::InvalidFreshnessPolicy)
    );
}

#[test]
fn a_regressing_observation_clock_cannot_rejuvenate_stale_evidence() {
    let mut tracker = ObservationTracker::new(boot(1));
    tracker
        .record(
            t(1),
            observation(1, 1, 1, Confidence::per_mille(900).unwrap()),
        )
        .unwrap();
    assert_eq!(
        tracker.freshness(t(100), 10, 800),
        Ok(Freshness::Stale { age_us: 99 })
    );
    assert_eq!(
        tracker.freshness(t(2), 10, 800),
        Err(Error::ClockRegression)
    );
    assert_eq!(tracker.latest(), None);
    assert_eq!(
        tracker.record(t(101), observation(1, 2, 101, Confidence::UNKNOWN)),
        Err(Error::Faulted)
    );
    assert_eq!(tracker.freshness(t(102), 10, 800), Err(Error::Faulted));
}
