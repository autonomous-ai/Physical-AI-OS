use lamp_interaction::{BootId, MonoTime, Observation};
use std::{fmt, num::NonZeroU64};

pub const MAX_SOURCES: usize = 3;
pub const FIELD_COUNT: usize = 9;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    TooManySources,
    DuplicateSource,
    ConflictingField(Field),
    InvalidFreshnessWindow,
    NonFiniteValue,
    ZeroSequence,
    UnconfiguredSource,
    UnsupportedField(Field),
    ReusedBoot,
    Stopped,
    ClockRegression,
    Faulted,
    Observation(lamp_interaction::Error),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "environment snapshot: {self:?}")
    }
}

impl std::error::Error for Error {}

impl From<lamp_interaction::Error> for Error {
    fn from(error: lamp_interaction::Error) -> Self {
        Self::Observation(error)
    }
}

/// A configured profile, not detected or authenticated hardware. SCD41 exposes
/// CO2 only here, matching the pinned V1 exported fields.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Sensor {
    Scd41,
    Sen55,
    Sen63c,
}

impl Sensor {
    pub fn supports(self, field: Field) -> bool {
        match self {
            Self::Scd41 => field == Field::Co2,
            Self::Sen55 => field != Field::Co2,
            Self::Sen63c => !matches!(field, Field::Voc | Field::Nox),
        }
    }

    pub(crate) fn index(self) -> usize {
        match self {
            Self::Scd41 => 0,
            Self::Sen55 => 1,
            Self::Sen63c => 2,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Field {
    Co2,
    Temperature,
    Humidity,
    Pm1,
    Pm2_5,
    Pm4,
    Pm10,
    Voc,
    Nox,
}

impl Field {
    pub const ALL: [Self; FIELD_COUNT] = [
        Self::Co2,
        Self::Temperature,
        Self::Humidity,
        Self::Pm1,
        Self::Pm2_5,
        Self::Pm4,
        Self::Pm10,
        Self::Voc,
        Self::Nox,
    ];

    pub(crate) fn index(self) -> usize {
        self as usize
    }
}

macro_rules! unit {
    ($name:ident, $doc:literal) => {
        #[doc = $doc]
        /// Finite representation only; no accuracy, plausibility or safety claim.
        #[derive(Clone, Copy, Debug, PartialEq)]
        pub struct $name(f64);

        impl $name {
            pub fn new(value: f64) -> Result<Self, Error> {
                if !value.is_finite() {
                    return Err(Error::NonFiniteValue);
                }
                Ok(Self(value))
            }

            pub fn get(self) -> f64 {
                self.0
            }
        }
    };
}

unit!(Co2Ppm, "Carbon dioxide concentration, parts per million.");
unit!(
    Celsius,
    "Ambient temperature, degrees Celsius; not SoC temperature."
);
unit!(RelativeHumidityPercent, "Relative humidity, percent.");
unit!(
    MicrogramsPerCubicMeter,
    "Particulate mass concentration, micrograms per cubic meter."
);
unit!(VocIndex, "VOC index, not a gas concentration.");
unit!(NoxIndex, "NOx index, not a gas concentration.");

/// A complete decoded sample, not a delta. None means unavailable, including
/// warmup/sentinel values. The future driver must validate raw length, CRC,
/// status, sentinels and scaling before constructing these engineering units.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Readings {
    pub co2: Option<Co2Ppm>,
    pub temperature: Option<Celsius>,
    pub humidity: Option<RelativeHumidityPercent>,
    pub pm1: Option<MicrogramsPerCubicMeter>,
    pub pm2_5: Option<MicrogramsPerCubicMeter>,
    pub pm4: Option<MicrogramsPerCubicMeter>,
    pub pm10: Option<MicrogramsPerCubicMeter>,
    pub voc: Option<VocIndex>,
    pub nox: Option<NoxIndex>,
}

impl Readings {
    pub fn get(self, field: Field) -> Option<Measurement> {
        match field {
            Field::Co2 => self.co2.map(Measurement::Co2),
            Field::Temperature => self.temperature.map(Measurement::Temperature),
            Field::Humidity => self.humidity.map(Measurement::Humidity),
            Field::Pm1 => self.pm1.map(Measurement::Particulate),
            Field::Pm2_5 => self.pm2_5.map(Measurement::Particulate),
            Field::Pm4 => self.pm4.map(Measurement::Particulate),
            Field::Pm10 => self.pm10.map(Measurement::Particulate),
            Field::Voc => self.voc.map(Measurement::Voc),
            Field::Nox => self.nox.map(Measurement::Nox),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Measurement {
    Co2(Co2Ppm),
    Temperature(Celsius),
    Humidity(RelativeHumidityPercent),
    Particulate(MicrogramsPerCubicMeter),
    Voc(VocIndex),
    Nox(NoxIndex),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DeviceStatus {
    /// The pinned SCD41 exported sample has no status register field.
    NotReported,
    /// SEN55 and SEN63C require an explicit zero status to use the sample.
    Reported(u32),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SourceFault {
    Transport,
    InvalidPayload,
    DeviceStatus(u32),
    StatusUnavailable,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Event {
    Sample {
        readings: Readings,
        device_status: DeviceStatus,
    },
    /// A successful not-ready check is not a new sample or freshness renewal.
    NoNewData,
    Fault(SourceFault),
    /// Terminal for this incarnation. Resume requires restart with a new boot.
    Stopped,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Report {
    pub sensor: Sensor,
    pub source_boot: BootId,
    pub sequence: NonZeroU64,
    /// Sample acquisition time, or time of the non-sample event. One shared OS
    /// monotonic domain; never assign receipt time to delayed sample bytes.
    pub observed_at: MonoTime,
    pub event: Event,
}

impl Report {
    pub fn new(
        sensor: Sensor,
        source_boot: BootId,
        sequence: u64,
        observed_at: MonoTime,
        event: Event,
    ) -> Result<Self, Error> {
        Ok(Self {
            sensor,
            source_boot,
            sequence: NonZeroU64::new(sequence).ok_or(Error::ZeroSequence)?,
            observed_at,
            event,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SourceConfig {
    pub sensor: Sensor,
    pub worker_boot: BootId,
    stale_after_us: NonZeroU64,
}

impl SourceConfig {
    pub fn new(sensor: Sensor, worker_boot: BootId, stale_after_us: u64) -> Result<Self, Error> {
        Ok(Self {
            sensor,
            worker_boot,
            stale_after_us: NonZeroU64::new(stale_after_us).ok_or(Error::InvalidFreshnessWindow)?,
        })
    }

    pub fn stale_after_us(self) -> u64 {
        self.stale_after_us.get()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Availability {
    NotConfigured,
    AwaitingSample,
    Unavailable,
    Ready,
    Stale,
    Fault(SourceFault),
    Stopped,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SampleProvenance {
    /// Confidence stays UNKNOWN. Presence/status/freshness establish software
    /// readiness, not sensor accuracy or a fabricated numeric confidence.
    pub observation: Observation,
    pub received_at: MonoTime,
    pub age_us: u64,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct FieldSnapshot {
    pub field: Field,
    pub source: Option<Sensor>,
    pub availability: Availability,
    /// Present only when ready; stale or unavailable values cannot masquerade
    /// as current values. Acquisition/receipt provenance remains separate.
    pub value: Option<Measurement>,
    pub sample: Option<SampleProvenance>,
    /// Last valid acquisition minus first in the observed unexpired run. This
    /// does not advance with wall/receipt time or prove no samples were lost.
    pub observed_continuity_us: Option<u64>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SourceSnapshot {
    pub config: SourceConfig,
    pub last_report: Option<Observation>,
    pub report_received_at: Option<MonoTime>,
    pub device_status: Option<DeviceStatus>,
    pub fault: Option<SourceFault>,
    pub stopped: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Readiness {
    Disabled,
    Unavailable,
    Partial,
    Ready,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Snapshot {
    pub checked_at: MonoTime,
    pub readiness: Readiness,
    pub fields: [FieldSnapshot; FIELD_COUNT],
    /// Fixed sensor-indexed slots; absence is unconfigured, not probe failure.
    pub sources: [Option<SourceSnapshot>; MAX_SOURCES],
}

impl Snapshot {
    pub fn field(&self, field: Field) -> &FieldSnapshot {
        &self.fields[field.index()]
    }
}
