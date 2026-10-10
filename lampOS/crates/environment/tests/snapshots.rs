use lamp_environment::*;
use lamp_interaction::{BootId, Confidence, MonoTime};

fn time(us: u64) -> MonoTime {
    MonoTime::from_micros(us)
}
fn boot(id: u8) -> BootId {
    BootId::new([id; 16]).unwrap()
}
fn config(sensor: Sensor, id: u8, stale: u64) -> SourceConfig {
    SourceConfig::new(sensor, boot(id), stale).unwrap()
}
fn co2(value: f64) -> Readings {
    Readings {
        co2: Some(Co2Ppm::new(value).unwrap()),
        ..Readings::default()
    }
}
fn sen55() -> Readings {
    Readings {
        temperature: Some(Celsius::new(23.25).unwrap()),
        humidity: Some(RelativeHumidityPercent::new(54.5).unwrap()),
        pm1: Some(MicrogramsPerCubicMeter::new(1.1).unwrap()),
        pm2_5: Some(MicrogramsPerCubicMeter::new(2.5).unwrap()),
        pm4: Some(MicrogramsPerCubicMeter::new(4.0).unwrap()),
        pm10: Some(MicrogramsPerCubicMeter::new(10.0).unwrap()),
        voc: Some(VocIndex::new(112.3).unwrap()),
        nox: Some(NoxIndex::new(1.4).unwrap()),
        ..Readings::default()
    }
}
fn sen63c() -> Readings {
    Readings {
        voc: None,
        nox: None,
        ..sen55()
    }
}
fn report(sensor: Sensor, id: u8, seq: u64, acquired: u64, event: Event) -> Report {
    Report::new(sensor, boot(id), seq, time(acquired), event).unwrap()
}
fn sample(sensor: Sensor, id: u8, seq: u64, acquired: u64, readings: Readings) -> Report {
    report(
        sensor,
        id,
        seq,
        acquired,
        Event::Sample {
            readings,
            device_status: if sensor == Sensor::Scd41 {
                DeviceStatus::NotReported
            } else {
                DeviceStatus::Reported(0)
            },
        },
    )
}
fn one(sensor: Sensor) -> Environment {
    Environment::new(&[config(sensor, 1, 100)]).unwrap()
}

#[test]
fn explicit_profiles_reject_duplicate_conflicts_and_unbounded_configuration() {
    let mut disabled = Environment::new(&[]).unwrap();
    let snapshot = disabled.snapshot(time(0)).unwrap();
    assert_eq!(snapshot.readiness, Readiness::Disabled);
    assert!(
        snapshot
            .fields
            .iter()
            .all(|f| f.availability == Availability::NotConfigured)
    );
    assert_eq!(
        SourceConfig::new(Sensor::Scd41, boot(1), 0),
        Err(Error::InvalidFreshnessWindow)
    );
    let scd = config(Sensor::Scd41, 1, 15_000_000);
    let sen55 = config(Sensor::Sen55, 2, 5_000_000);
    let sen63 = config(Sensor::Sen63c, 3, 5_000_000);
    assert_eq!(
        Environment::new(&[scd, scd]).unwrap_err(),
        Error::DuplicateSource
    );
    assert_eq!(
        Environment::new(&[scd, sen63]).unwrap_err(),
        Error::ConflictingField(Field::Co2)
    );
    assert_eq!(
        Environment::new(&[sen55, sen63]).unwrap_err(),
        Error::ConflictingField(Field::Temperature)
    );
    assert_eq!(
        Environment::new(&[scd; 4]).unwrap_err(),
        Error::TooManySources
    );
    assert!(Environment::new(&[scd, sen55]).is_ok());
}

#[test]
fn all_nine_typed_values_have_independent_source_and_acquisition_provenance() {
    let mut cache =
        Environment::new(&[config(Sensor::Scd41, 1, 100), config(Sensor::Sen55, 2, 100)]).unwrap();
    cache
        .ingest(time(15), sample(Sensor::Scd41, 1, 1, 10, co2(612.0)))
        .unwrap();
    cache
        .ingest(time(22), sample(Sensor::Sen55, 2, 8, 20, sen55()))
        .unwrap();
    let snapshot = cache.snapshot(time(30)).unwrap();
    assert_eq!(snapshot.readiness, Readiness::Ready);
    assert_eq!(snapshot.field(Field::Co2).value, co2(612.0).get(Field::Co2));
    let co2_sample = snapshot.field(Field::Co2).sample.unwrap();
    assert_eq!(
        (
            co2_sample.observation.acquired_at(),
            co2_sample.received_at,
            co2_sample.age_us
        ),
        (time(10), time(15), 20)
    );
    for field in Field::ALL.into_iter().filter(|f| *f != Field::Co2) {
        let item = snapshot.field(field);
        assert_eq!(item.value, sen55().get(field));
        assert_eq!(item.source, Some(Sensor::Sen55));
        let provenance = item.sample.unwrap();
        assert_eq!(provenance.observation.source_boot(), boot(2));
        assert_eq!(provenance.observation.sequence(), 8);
        assert_eq!(provenance.observation.confidence(), Confidence::UNKNOWN);
        assert_eq!(provenance.received_at, time(22));
        assert_eq!(provenance.age_us, 10);
    }
}

#[test]
fn sen63c_warmup_preserves_usable_fields_and_does_not_backdate_co2_continuity() {
    let mut cache = one(Sensor::Sen63c);
    cache
        .ingest(time(0), sample(Sensor::Sen63c, 1, 1, 0, sen63c()))
        .unwrap();
    let warmup = cache.snapshot(time(5)).unwrap();
    assert_eq!(warmup.readiness, Readiness::Partial);
    assert_eq!(
        warmup
            .fields
            .iter()
            .filter(|f| f.availability == Availability::Ready)
            .count(),
        6
    );
    assert_eq!(
        warmup.field(Field::Co2).availability,
        Availability::Unavailable
    );
    assert_eq!(
        warmup.field(Field::Voc).availability,
        Availability::NotConfigured
    );
    let available = Readings {
        co2: co2(500.0).co2,
        ..sen63c()
    };
    cache
        .ingest(time(50), sample(Sensor::Sen63c, 1, 2, 50, available))
        .unwrap();
    let ready = cache.snapshot(time(55)).unwrap();
    assert_eq!(ready.readiness, Readiness::Ready);
    assert_eq!(ready.field(Field::Co2).observed_continuity_us, Some(0));
    assert_eq!(
        ready.field(Field::Temperature).observed_continuity_us,
        Some(50)
    );
    cache
        .ingest(time(60), sample(Sensor::Sen63c, 1, 3, 60, sen63c()))
        .unwrap();
    let absent = cache.snapshot(time(60)).unwrap();
    assert_eq!(absent.field(Field::Co2).value, None);
    assert_eq!(absent.field(Field::Co2).observed_continuity_us, None);
    assert_eq!(
        absent.field(Field::Temperature).observed_continuity_us,
        Some(60)
    );
}

#[test]
fn empty_sample_never_becomes_ready_and_new_absence_replaces_previous_good_data() {
    let mut cache = one(Sensor::Scd41);
    cache
        .ingest(time(10), sample(Sensor::Scd41, 1, 1, 10, co2(600.0)))
        .unwrap();
    cache
        .ingest(
            time(20),
            sample(Sensor::Scd41, 1, 2, 20, Readings::default()),
        )
        .unwrap();
    let snapshot = cache.snapshot(time(20)).unwrap();
    assert_eq!(snapshot.readiness, Readiness::Unavailable);
    assert_eq!(
        snapshot.field(Field::Co2).availability,
        Availability::Unavailable
    );
    assert!(snapshot.field(Field::Co2).value.is_none());
}

#[test]
fn late_receipt_and_not_ready_polls_cannot_renew_freshness_or_observed_continuity() {
    let mut cache = one(Sensor::Scd41);
    cache
        .ingest(time(20), sample(Sensor::Scd41, 1, 1, 10, co2(600.0)))
        .unwrap();
    cache
        .ingest(time(70), sample(Sensor::Scd41, 1, 2, 60, co2(610.0)))
        .unwrap();
    cache
        .ingest(
            time(150),
            report(Sensor::Scd41, 1, 3, 150, Event::NoNewData),
        )
        .unwrap();
    let current = cache.snapshot(time(160)).unwrap();
    assert_eq!(current.field(Field::Co2).availability, Availability::Ready);
    assert_eq!(current.field(Field::Co2).observed_continuity_us, Some(50));
    assert_eq!(
        current.field(Field::Co2).sample.unwrap().received_at,
        time(70)
    );
    let stale = cache.snapshot(time(161)).unwrap();
    assert_eq!(stale.field(Field::Co2).availability, Availability::Stale);
    assert_eq!(stale.field(Field::Co2).value, None);
    assert_eq!(stale.field(Field::Co2).observed_continuity_us, None);
    cache
        .ingest(time(200), sample(Sensor::Scd41, 1, 4, 200, co2(620.0)))
        .unwrap();
    assert_eq!(
        cache
            .snapshot(time(200))
            .unwrap()
            .field(Field::Co2)
            .observed_continuity_us,
        Some(0)
    );
    let mut delayed = one(Sensor::Scd41);
    delayed
        .ingest(time(500), sample(Sensor::Scd41, 1, 1, 1, co2(700.0)))
        .unwrap();
    assert_eq!(
        delayed
            .snapshot(time(500))
            .unwrap()
            .field(Field::Co2)
            .availability,
        Availability::Stale
    );
}

#[test]
fn missing_status_and_every_nonzero_status_fault_cannot_expose_present_values() {
    for sensor in [Sensor::Sen55, Sensor::Sen63c] {
        let readings = if sensor == Sensor::Sen55 {
            sen55()
        } else {
            sen63c()
        };
        for (status, expected) in [
            (DeviceStatus::NotReported, SourceFault::StatusUnavailable),
            (DeviceStatus::Reported(1), SourceFault::DeviceStatus(1)),
            (
                DeviceStatus::Reported(u32::MAX),
                SourceFault::DeviceStatus(u32::MAX),
            ),
        ] {
            let mut cache = one(sensor);
            cache
                .ingest(time(1), sample(sensor, 1, 1, 1, readings))
                .unwrap();
            cache
                .ingest(
                    time(2),
                    report(
                        sensor,
                        1,
                        2,
                        2,
                        Event::Sample {
                            readings,
                            device_status: status,
                        },
                    ),
                )
                .unwrap();
            let snapshot = cache.snapshot(time(2)).unwrap();
            assert_eq!(snapshot.readiness, Readiness::Unavailable);
            assert!(
                snapshot
                    .fields
                    .iter()
                    .filter(|f| f.source.is_some())
                    .all(|f| {
                        f.availability == Availability::Fault(expected)
                            && f.value.is_none()
                            && f.sample.is_none()
                    })
            );
            cache
                .ingest(time(3), sample(sensor, 1, 3, 3, readings))
                .unwrap();
            assert_eq!(
                cache
                    .snapshot(time(3))
                    .unwrap()
                    .field(Field::Temperature)
                    .observed_continuity_us,
                Some(0)
            );
        }
    }
    let mut scd = one(Sensor::Scd41);
    scd.ingest(time(1), sample(Sensor::Scd41, 1, 1, 1, co2(600.0)))
        .unwrap();
    assert_eq!(scd.snapshot(time(1)).unwrap().readiness, Readiness::Ready);
    scd.ingest(
        time(2),
        report(
            Sensor::Scd41,
            1,
            2,
            2,
            Event::Sample {
                readings: co2(600.0),
                device_status: DeviceStatus::Reported(1),
            },
        ),
    )
    .unwrap();
    assert_eq!(
        scd.snapshot(time(2)).unwrap().readiness,
        Readiness::Unavailable
    );
}

#[test]
fn transport_fault_retains_ordering_and_not_ready_cannot_hide_it() {
    let mut cache = one(Sensor::Scd41);
    cache
        .ingest(time(10), sample(Sensor::Scd41, 1, 1, 10, co2(600.0)))
        .unwrap();
    cache
        .ingest(
            time(20),
            report(
                Sensor::Scd41,
                1,
                3,
                20,
                Event::Fault(SourceFault::Transport),
            ),
        )
        .unwrap();
    assert!(
        cache
            .ingest(time(21), sample(Sensor::Scd41, 1, 2, 15, co2(700.0)))
            .is_err()
    );
    cache
        .ingest(time(22), report(Sensor::Scd41, 1, 4, 22, Event::NoNewData))
        .unwrap();
    assert_eq!(
        cache
            .snapshot(time(22))
            .unwrap()
            .field(Field::Co2)
            .availability,
        Availability::Fault(SourceFault::Transport)
    );
    cache
        .ingest(time(30), sample(Sensor::Scd41, 1, 5, 30, co2(620.0)))
        .unwrap();
    assert_eq!(
        cache
            .snapshot(time(30))
            .unwrap()
            .field(Field::Co2)
            .observed_continuity_us,
        Some(0)
    );
}

#[test]
fn malformed_or_old_reports_cannot_erase_or_relabel_newer_good_data() {
    let mut cache = one(Sensor::Scd41);
    cache
        .ingest(time(10), sample(Sensor::Scd41, 1, 2, 8, co2(612.0)))
        .unwrap();
    for rejected in [
        sample(Sensor::Scd41, 2, 3, 9, co2(700.0)),
        sample(Sensor::Scd41, 1, 2, 9, co2(700.0)),
        sample(Sensor::Scd41, 1, 1, 9, co2(700.0)),
        sample(Sensor::Scd41, 1, 3, 7, co2(700.0)),
        sample(Sensor::Scd41, 1, 3, 12, co2(700.0)),
        sample(Sensor::Sen55, 1, 3, 9, sen55()),
        sample(Sensor::Scd41, 1, 3, 9, sen55()),
    ] {
        assert!(cache.ingest(time(11), rejected).is_err());
        let latest = cache.snapshot(time(11)).unwrap();
        assert_eq!(latest.field(Field::Co2).value, co2(612.0).get(Field::Co2));
        assert_eq!(
            latest
                .field(Field::Co2)
                .sample
                .unwrap()
                .observation
                .sequence(),
            2
        );
        assert_eq!(
            latest.field(Field::Co2).sample.unwrap().received_at,
            time(10)
        );
    }
    assert_eq!(
        Report::new(Sensor::Scd41, boot(1), 0, time(11), Event::NoNewData),
        Err(Error::ZeroSequence)
    );
}

#[test]
fn restart_invalidates_every_source_from_the_worker_but_preserves_other_workers() {
    for shared_worker in [false, true] {
        let second = if shared_worker { 1 } else { 2 };
        let mut cache = Environment::new(&[
            config(Sensor::Scd41, 1, 100),
            config(Sensor::Sen55, second, 100),
        ])
        .unwrap();
        cache
            .ingest(time(1), sample(Sensor::Scd41, 1, 9, 1, co2(600.0)))
            .unwrap();
        cache
            .ingest(time(1), sample(Sensor::Sen55, second, 9, 1, sen55()))
            .unwrap();
        assert_eq!(
            cache.restart_worker(time(2), boot(1), boot(1)),
            Err(Error::ReusedBoot)
        );
        cache.restart_worker(time(2), boot(1), boot(3)).unwrap();
        let restarted = cache.snapshot(time(2)).unwrap();
        assert_eq!(
            restarted.field(Field::Co2).availability,
            Availability::AwaitingSample
        );
        assert_eq!(
            restarted.field(Field::Temperature).availability,
            if shared_worker {
                Availability::AwaitingSample
            } else {
                Availability::Ready
            }
        );
        assert!(
            cache
                .ingest(time(3), sample(Sensor::Scd41, 1, 10, 3, co2(700.0)))
                .is_err()
        );
        cache
            .ingest(time(4), sample(Sensor::Scd41, 3, 1, 4, co2(620.0)))
            .unwrap();
        let current = cache.snapshot(time(4)).unwrap();
        assert_eq!(current.field(Field::Co2).observed_continuity_us, Some(0));
        assert_eq!(
            current
                .field(Field::Co2)
                .sample
                .unwrap()
                .observation
                .source_boot(),
            boot(3)
        );
    }
}

#[test]
fn stopped_worker_requires_explicit_restart_and_clock_regression_latches() {
    let mut cache = one(Sensor::Scd41);
    cache
        .ingest(time(1), sample(Sensor::Scd41, 1, 1, 1, co2(600.0)))
        .unwrap();
    cache
        .ingest(time(2), report(Sensor::Scd41, 1, 2, 2, Event::Stopped))
        .unwrap();
    assert_eq!(
        cache
            .snapshot(time(2))
            .unwrap()
            .field(Field::Co2)
            .availability,
        Availability::Stopped
    );
    assert_eq!(
        cache.ingest(time(3), sample(Sensor::Scd41, 1, 3, 3, co2(600.0))),
        Err(Error::Stopped)
    );
    cache.restart_worker(time(4), boot(1), boot(2)).unwrap();
    cache
        .ingest(time(5), sample(Sensor::Scd41, 2, 1, 5, co2(600.0)))
        .unwrap();
    assert_eq!(cache.snapshot(time(4)), Err(Error::ClockRegression));
    assert_eq!(cache.snapshot(time(6)), Err(Error::Faulted));
    assert_eq!(
        cache.restart_worker(time(6), boot(2), boot(3)),
        Err(Error::Faulted)
    );
}

#[test]
fn units_reject_nonfinite_inputs_without_clamping_extreme_finite_values() {
    for invalid in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        assert_eq!(Co2Ppm::new(invalid), Err(Error::NonFiniteValue));
        assert_eq!(Celsius::new(invalid), Err(Error::NonFiniteValue));
        assert_eq!(
            RelativeHumidityPercent::new(invalid),
            Err(Error::NonFiniteValue)
        );
        assert_eq!(
            MicrogramsPerCubicMeter::new(invalid),
            Err(Error::NonFiniteValue)
        );
        assert_eq!(VocIndex::new(invalid), Err(Error::NonFiniteValue));
        assert_eq!(NoxIndex::new(invalid), Err(Error::NonFiniteValue));
    }
    assert_eq!(Celsius::new(-273.16).unwrap().get(), -273.16);
    assert_eq!(RelativeHumidityPercent::new(100.01).unwrap().get(), 100.01);
    assert_eq!(Co2Ppm::new(f64::MAX).unwrap().get(), f64::MAX);
    assert_eq!(VocIndex::new(112.3).unwrap().get(), 112.3);
    assert_eq!(NoxIndex::new(1.4).unwrap().get(), 1.4);
    assert_eq!(MicrogramsPerCubicMeter::new(2.5).unwrap().get(), 2.5);
}

#[test]
fn monotonic_and_sequence_limits_do_not_wrap_and_fixed_state_stays_small() {
    let mut cache = one(Sensor::Scd41);
    cache
        .ingest(
            time(u64::MAX),
            sample(Sensor::Scd41, 1, u64::MAX, u64::MAX, co2(600.0)),
        )
        .unwrap();
    assert_eq!(
        cache
            .snapshot(time(u64::MAX))
            .unwrap()
            .field(Field::Co2)
            .sample
            .unwrap()
            .age_us,
        0
    );
    assert!(
        cache
            .ingest(
                time(u64::MAX),
                sample(Sensor::Scd41, 1, u64::MAX, u64::MAX, co2(601.0))
            )
            .is_err()
    );
    assert!(std::mem::size_of::<Environment>() <= 4096);
    assert!(std::mem::size_of::<Snapshot>() <= 4096);
}
