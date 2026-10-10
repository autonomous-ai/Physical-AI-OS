//! Fixed-size environmental state. No driver I/O, locks, scheduler, allocation
//! on ingest/snapshot, automatic source selection, or numerical safety policy.
//! See `docs/environment-snapshots.md` for the pinned V1 reuse and limitations.

mod types;
pub use types::*;

use lamp_interaction::{BootId, Confidence, Freshness, MonoTime, Observation, ObservationTracker};

#[derive(Debug)]
struct Source {
    config: SourceConfig,
    reports: ObservationTracker,
    samples: ObservationTracker,
    report_received_at: Option<MonoTime>,
    sample_received_at: Option<MonoTime>,
    readings: Readings,
    first_valid: [Option<MonoTime>; FIELD_COUNT],
    device_status: Option<DeviceStatus>,
    fault: Option<SourceFault>,
    stopped: bool,
}

impl Source {
    fn new(config: SourceConfig) -> Self {
        Self {
            config,
            reports: ObservationTracker::new(config.worker_boot),
            samples: ObservationTracker::new(config.worker_boot),
            report_received_at: None,
            sample_received_at: None,
            readings: Readings::default(),
            first_valid: [None; FIELD_COUNT],
            device_status: None,
            fault: None,
            stopped: false,
        }
    }

    fn clear_sample(&mut self) {
        self.samples = ObservationTracker::new(self.config.worker_boot);
        self.sample_received_at = None;
        self.readings = Readings::default();
        self.first_valid = [None; FIELD_COUNT];
    }

    fn fresh_age(&mut self, now: MonoTime) -> Result<Option<u64>, Error> {
        // UNKNOWN confidence is deliberate. This only establishes age; the
        // explicit sample validity and status checks establish usable data.
        match self
            .samples
            .freshness(now, self.config.stale_after_us(), 0)?
        {
            Freshness::Unknown { age_us } => Ok(Some(age_us)),
            _ => Ok(None),
        }
    }

    fn summary(&self) -> SourceSnapshot {
        SourceSnapshot {
            config: self.config,
            last_report: self.reports.latest(),
            report_received_at: self.report_received_at,
            device_status: self.device_status,
            fault: self.fault,
            stopped: self.stopped,
        }
    }
}

/// Latest state for at most three explicitly selected source profiles and nine
/// fields. One supervisor-bound producer incarnation per source. Public times
/// are trusted receipt/check times in the same monotonic domain as acquisition.
#[derive(Debug)]
pub struct Environment {
    sources: [Option<Source>; MAX_SOURCES],
    owners: [Option<Sensor>; FIELD_COUNT],
    last_now: Option<MonoTime>,
    faulted: bool,
}

impl Environment {
    pub fn new(configs: &[SourceConfig]) -> Result<Self, Error> {
        if configs.len() > MAX_SOURCES {
            return Err(Error::TooManySources);
        }
        let mut sources: [Option<Source>; MAX_SOURCES] = std::array::from_fn(|_| None);
        let mut owners = [None; FIELD_COUNT];
        for config in configs {
            if sources[config.sensor.index()].is_some() {
                return Err(Error::DuplicateSource);
            }
            for field in Field::ALL {
                if config.sensor.supports(field) {
                    if owners[field.index()].is_some() {
                        return Err(Error::ConflictingField(field));
                    }
                    owners[field.index()] = Some(config.sensor);
                }
            }
            sources[config.sensor.index()] = Some(Source::new(*config));
        }
        Ok(Self {
            sources,
            owners,
            last_now: None,
            faulted: false,
        })
    }

    /// Accept one complete decoded report. Rejected identity/order/payload
    /// cannot replace newer data. Merely receiving a report never renews a
    /// sample's acquisition time. Faults and stop events preserve their own
    /// ordering high-water mark after discarding sample data.
    pub fn ingest(&mut self, received_at: MonoTime, report: Report) -> Result<(), Error> {
        self.advance(received_at)?;
        let source = self.sources[report.sensor.index()]
            .as_mut()
            .ok_or(Error::UnconfiguredSource)?;
        if source.stopped {
            return Err(Error::Stopped);
        }
        if let Event::Sample { readings, .. } = report.event {
            for field in Field::ALL {
                if readings.get(field).is_some() && !report.sensor.supports(field) {
                    return Err(Error::UnsupportedField(field));
                }
            }
        }
        let observation = Observation::new(
            report.source_boot,
            report.sequence,
            report.observed_at,
            Confidence::UNKNOWN,
        );
        source.reports.record(received_at, observation)?;
        source.report_received_at = Some(received_at);
        match report.event {
            Event::Sample {
                readings,
                device_status,
            } => {
                source.device_status = Some(device_status);
                let fault = match device_status {
                    DeviceStatus::Reported(bits) if bits != 0 => {
                        Some(SourceFault::DeviceStatus(bits))
                    }
                    DeviceStatus::NotReported if report.sensor != Sensor::Scd41 => {
                        Some(SourceFault::StatusUnavailable)
                    }
                    _ => None,
                };
                source.fault = fault;
                if fault.is_some() {
                    source.clear_sample();
                    return Ok(());
                }
                source.samples.record(received_at, observation)?;
                source.sample_received_at = Some(received_at);
                let fresh = source.fresh_age(received_at)?.is_some();
                for field in Field::ALL {
                    let first = &mut source.first_valid[field.index()];
                    if fresh && readings.get(field).is_some() {
                        first.get_or_insert(report.observed_at);
                    } else {
                        *first = None;
                    }
                }
                source.readings = readings;
            }
            Event::NoNewData => {}
            Event::Fault(fault) => {
                source.clear_sample();
                source.device_status = None;
                source.fault = Some(fault);
            }
            Event::Stopped => {
                source.clear_sample();
                source.device_status = None;
                source.stopped = true;
            }
        }
        Ok(())
    }

    /// Explicit supervisor action after a producer restart. All sources using
    /// the retired worker boot are invalidated together; this also handles a
    /// worker that owns more than one sensor. IDs must never be reused, including
    /// historical IDs that this fixed-size latest-state store does not retain.
    pub fn restart_worker(
        &mut self,
        now: MonoTime,
        old_boot: BootId,
        new_boot: BootId,
    ) -> Result<(), Error> {
        self.advance(now)?;
        if old_boot == new_boot
            || self
                .sources
                .iter()
                .flatten()
                .any(|s| s.config.worker_boot == new_boot)
        {
            return Err(Error::ReusedBoot);
        }
        if !self
            .sources
            .iter()
            .flatten()
            .any(|s| s.config.worker_boot == old_boot)
        {
            return Err(Error::UnconfiguredSource);
        }
        for source in self.sources.iter_mut().flatten() {
            if source.config.worker_boot == old_boot {
                let mut config = source.config;
                config.worker_boot = new_boot;
                *source = Source::new(config);
            }
        }
        Ok(())
    }

    pub fn snapshot(&mut self, now: MonoTime) -> Result<Snapshot, Error> {
        self.advance(now)?;
        let mut fields = std::array::from_fn(|index| FieldSnapshot {
            field: Field::ALL[index],
            source: self.owners[index],
            availability: Availability::NotConfigured,
            value: None,
            sample: None,
            observed_continuity_us: None,
        });
        let mut configured = 0;
        let mut ready = 0;
        for source in self.sources.iter_mut().flatten() {
            let fresh = source.fresh_age(now)?.is_some();
            let sample = source
                .samples
                .latest()
                .zip(source.sample_received_at)
                .and_then(|(observation, received_at)| {
                    Some(SampleProvenance {
                        observation,
                        received_at,
                        age_us: now
                            .as_micros()
                            .checked_sub(observation.acquired_at().as_micros())?,
                    })
                });
            for field in Field::ALL {
                if !source.config.sensor.supports(field) {
                    continue;
                }
                configured += 1;
                let item = &mut fields[field.index()];
                item.sample = sample;
                item.availability = if source.stopped {
                    Availability::Stopped
                } else if let Some(fault) = source.fault {
                    Availability::Fault(fault)
                } else if sample.is_none() {
                    Availability::AwaitingSample
                } else if !fresh {
                    Availability::Stale
                } else if let Some(value) = source.readings.get(field) {
                    item.value = Some(value);
                    item.observed_continuity_us = source.first_valid[field.index()]
                        .zip(sample)
                        .and_then(|(first, last)| {
                            last.observation
                                .acquired_at()
                                .as_micros()
                                .checked_sub(first.as_micros())
                        });
                    ready += 1;
                    Availability::Ready
                } else {
                    Availability::Unavailable
                };
            }
        }
        Ok(Snapshot {
            checked_at: now,
            readiness: match (configured, ready) {
                (0, _) => Readiness::Disabled,
                (_, 0) => Readiness::Unavailable,
                (all, count) if all == count => Readiness::Ready,
                _ => Readiness::Partial,
            },
            fields,
            sources: std::array::from_fn(|index| self.sources[index].as_ref().map(Source::summary)),
        })
    }

    fn advance(&mut self, now: MonoTime) -> Result<(), Error> {
        if self.faulted {
            return Err(Error::Faulted);
        }
        if self.last_now.is_some_and(|previous| now < previous) {
            self.faulted = true;
            for source in self.sources.iter_mut().flatten() {
                source.clear_sample();
            }
            return Err(Error::ClockRegression);
        }
        self.last_now = Some(now);
        for source in self.sources.iter_mut().flatten() {
            if source.fresh_age(now)?.is_none() {
                source.first_valid = [None; FIELD_COUNT];
            }
        }
        Ok(())
    }
}
