use crate::{BootId, Error, MonoTime};
use serde::{Deserialize, Serialize};
use std::num::NonZeroU64;

/// Confidence is not identity, addressee evidence, or a direction estimate.
/// An unknown score must not be converted into a confident positive observation.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(try_from = "Option<u16>", into = "Option<u16>")]
pub struct Confidence(Option<u16>);

impl Confidence {
    pub const UNKNOWN: Self = Self(None);

    pub fn per_mille(value: u16) -> Result<Self, Error> {
        if value > 1_000 {
            return Err(Error::InvalidConfidence);
        }
        Ok(Self(Some(value)))
    }

    pub fn value(self) -> Option<u16> {
        self.0
    }
}

impl TryFrom<Option<u16>> for Confidence {
    type Error = Error;
    fn try_from(value: Option<u16>) -> Result<Self, Self::Error> {
        value.map_or(Ok(Self::UNKNOWN), Self::per_mille)
    }
}

impl From<Confidence> for Option<u16> {
    fn from(value: Confidence) -> Self {
        value.0
    }
}

/// Fixed-size provenance, kept beside a bounded frame/sensor payload by the
/// worker. Acquisition time is distinct from controller receipt time.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Observation {
    source_boot: BootId,
    sequence: NonZeroU64,
    acquired_at: MonoTime,
    confidence: Confidence,
}

impl Observation {
    pub fn new(
        source_boot: BootId,
        sequence: NonZeroU64,
        acquired_at: MonoTime,
        confidence: Confidence,
    ) -> Self {
        Self {
            source_boot,
            sequence,
            acquired_at,
            confidence,
        }
    }
    pub fn source_boot(self) -> BootId {
        self.source_boot
    }
    pub fn sequence(self) -> u64 {
        self.sequence.get()
    }
    pub fn acquired_at(self) -> MonoTime {
        self.acquired_at
    }
    pub fn confidence(self) -> Confidence {
        self.confidence
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Freshness {
    Missing,
    Future,
    Stale { age_us: u64 },
    Unknown { age_us: u64 },
    Uncertain { age_us: u64, confidence: u16 },
    Fresh { age_us: u64, confidence: u16 },
}

/// One fixed-size latest value per selected worker. There is no accumulated
/// frame history or queue. Rebind by constructing a tracker with the restarted
/// worker's new boot; a delayed old-worker packet cannot replace its evidence.
#[derive(Debug)]
pub struct ObservationTracker {
    expected_boot: BootId,
    last_now: Option<MonoTime>,
    faulted: bool,
    latest: Option<Observation>,
}

impl ObservationTracker {
    pub fn new(expected_boot: BootId) -> Self {
        Self {
            expected_boot,
            last_now: None,
            faulted: false,
            latest: None,
        }
    }

    pub fn record(&mut self, now: MonoTime, observation: Observation) -> Result<(), Error> {
        self.advance(now)?;
        if observation.source_boot != self.expected_boot {
            return Err(Error::WrongObservationSource);
        }
        if observation.acquired_at > now {
            return Err(Error::FutureObservation);
        }
        if self.latest.is_some_and(|previous| {
            observation.sequence <= previous.sequence
                || observation.acquired_at < previous.acquired_at
        }) {
            return Err(Error::OutOfOrderObservation);
        }
        // Keep uncertain/newer evidence rather than silently falling back to an
        // older, more flattering high-confidence frame.
        self.latest = Some(observation);
        Ok(())
    }

    pub fn latest(&self) -> Option<Observation> {
        self.latest
    }

    pub fn freshness(
        &mut self,
        now: MonoTime,
        max_age_us: u64,
        min_confidence: u16,
    ) -> Result<Freshness, Error> {
        if max_age_us == 0 || min_confidence > 1_000 {
            return Err(Error::InvalidFreshnessPolicy);
        }
        self.advance(now)?;
        let Some(observation) = self.latest else {
            return Ok(Freshness::Missing);
        };
        let Some(age_us) = now.elapsed_since(observation.acquired_at) else {
            return Ok(Freshness::Future);
        };
        if age_us > max_age_us {
            return Ok(Freshness::Stale { age_us });
        }
        Ok(match observation.confidence.value() {
            None => Freshness::Unknown { age_us },
            Some(confidence) if confidence < min_confidence => {
                Freshness::Uncertain { age_us, confidence }
            }
            Some(confidence) => Freshness::Fresh { age_us, confidence },
        })
    }

    fn advance(&mut self, now: MonoTime) -> Result<(), Error> {
        if self.faulted {
            return Err(Error::Faulted);
        }
        if self.last_now.is_some_and(|last| now < last) {
            self.latest = None;
            self.faulted = true;
            return Err(Error::ClockRegression);
        }
        self.last_now = Some(now);
        Ok(())
    }
}
