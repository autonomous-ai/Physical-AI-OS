//! Explicit per-unit calibration, structural validation, and unit conversions.
//!
//! No repository defaults, file search, hardware writes, or physical safety
//! claims. Unit binding checks the identity supplied by the caller, not an
//! authenticated device. Fresh register and placement qualification remain
//! required before any future motion implementation.

use crate::{
    JOINT_COUNT, Joint,
    readback::{HomingModeRegisters, PositionLimitRegisters},
    units::{EncoderCounts, HomingOffset, NormalizedPosition, UnitError},
};
use std::fmt;

const MAX_UNIT_ID_BYTES: usize = 64;

/// Bounded, explicit identifier attached to provisioned per-unit calibration.
#[derive(Clone, Eq, PartialEq)]
pub struct UnitId {
    bytes: [u8; MAX_UNIT_ID_BYTES],
    length: u8,
}

impl UnitId {
    pub fn new(value: &str) -> Result<Self, InvalidUnitId> {
        if value.is_empty()
            || value.len() > MAX_UNIT_ID_BYTES
            || !value
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"-_.:".contains(&byte))
        {
            return Err(InvalidUnitId);
        }
        let mut bytes = [0; MAX_UNIT_ID_BYTES];
        bytes[..value.len()].copy_from_slice(value.as_bytes());
        Ok(Self {
            bytes,
            length: value.len() as u8,
        })
    }

    pub fn as_str(&self) -> &str {
        // The constructor accepts only ASCII; storage is private and immutable.
        std::str::from_utf8(&self.bytes[..usize::from(self.length)])
            .expect("validated ASCII unit ID")
    }
}

impl fmt::Debug for UnitId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_tuple("UnitId").field(&self.as_str()).finish()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct InvalidUnitId;

impl fmt::Display for InvalidUnitId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(
            "unit ID requires 1..=64 ASCII letters, digits, hyphens, underscores, dots or colons",
        )
    }
}

impl std::error::Error for InvalidUnitId {}

/// Raw legacy fields plus their explicit joint key. Numeric types deliberately
/// allow invalid calibration values to reach validation rather than truncate.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CalibrationRecord {
    pub joint: Joint,
    pub id: u16,
    pub drive_mode: u8,
    pub homing_offset: i32,
    pub range_min: i32,
    pub range_max: i32,
}

/// Software normalization sign, not an independently readable servo register.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DriveMode {
    Normal,
    Reversed,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct JointCalibration {
    joint: Joint,
    drive_mode: DriveMode,
    homing_offset: HomingOffset,
    min: EncoderCounts,
    max: EncoderCounts,
}

impl JointCalibration {
    fn validate(record: CalibrationRecord) -> Result<Self, CalibrationError> {
        let joint = record.joint;
        if record.id != u16::from(joint.id()) {
            return Err(CalibrationError::ServoId {
                joint,
                found: record.id,
            });
        }
        let drive_mode = match record.drive_mode {
            0 => DriveMode::Normal,
            1 => DriveMode::Reversed,
            value => {
                return Err(CalibrationError::DriveMode {
                    joint,
                    found: value,
                });
            }
        };
        let units = |error| CalibrationError::Units { joint, error };
        let homing_offset = HomingOffset::new(record.homing_offset).map_err(units)?;
        let min = EncoderCounts::new(record.range_min).map_err(units)?;
        let max = EncoderCounts::new(record.range_max).map_err(units)?;
        if min >= max {
            return Err(CalibrationError::InvalidSpan {
                joint,
                min: min.get(),
                max: max.get(),
            });
        }
        Ok(Self {
            joint,
            drive_mode,
            homing_offset,
            min,
            max,
        })
    }

    pub const fn joint(self) -> Joint {
        self.joint
    }
    pub const fn drive_mode(self) -> DriveMode {
        self.drive_mode
    }
    pub const fn homing_offset(self) -> HomingOffset {
        self.homing_offset
    }
    pub const fn range_min(self) -> EncoderCounts {
        self.min
    }
    pub const fn range_max(self) -> EncoderCounts {
        self.max
    }

    /// Convert an already homing-adjusted position. Unlike legacy clipping,
    /// readings outside the provisioned span are explicit errors, not endpoints.
    pub fn normalize(
        self,
        position: EncoderCounts,
    ) -> Result<NormalizedPosition, CalibrationError> {
        if position < self.min || position > self.max {
            return Err(CalibrationError::PositionOutsideSpan {
                joint: self.joint,
                position: position.get(),
                min: self.min.get(),
                max: self.max.get(),
            });
        }
        let normalized = f64::from(position.get() - self.min.get())
            / f64::from(self.max.get() - self.min.get())
            * 200.0
            - 100.0;
        let normalized = match self.drive_mode {
            DriveMode::Normal => normalized,
            DriveMode::Reversed => -normalized,
        };
        NormalizedPosition::new(normalized).map_err(|error| CalibrationError::Units {
            joint: self.joint,
            error,
        })
    }

    /// Pure inverse mapping, not a motion command. Match the pinned SDK's
    /// positive-count truncation rather than round-to-nearest. Floating-point
    /// round trips may lose one count; no physical angle accuracy is asserted.
    pub fn denormalize(self, position: NormalizedPosition) -> EncoderCounts {
        let value = match self.drive_mode {
            DriveMode::Normal => position.get(),
            DriveMode::Reversed => -position.get(),
        };
        let counts = ((value + 100.0) / 200.0) * f64::from(self.max.get() - self.min.get())
            + f64::from(self.min.get());
        EncoderCounts::new(counts.trunc() as i32)
            .expect("validated normalized position maps inside validated encoder span")
    }

    /// Compare supplied register values only. They must be from this joint and
    /// position mode; software drive_mode has no register to compare. This is
    /// not a freshness, servo identity, torque, collision, or placement check.
    pub fn compare_registers(
        self,
        limits: PositionLimitRegisters,
        homing: HomingModeRegisters,
    ) -> Result<(), CalibrationError> {
        for observed in [limits.joint(), homing.joint()] {
            if observed != self.joint {
                return Err(CalibrationError::ReadbackJoint {
                    expected: self.joint,
                    observed,
                });
            }
        }
        if homing.operating_mode_raw() != 0 {
            return Err(CalibrationError::OperatingMode {
                joint: self.joint,
                observed: homing.operating_mode_raw(),
            });
        }
        if limits.min().raw() != self.min.get() || limits.max().raw() != self.max.get() {
            return Err(CalibrationError::ReadbackSpan {
                joint: self.joint,
                expected: [self.min.get(), self.max.get()],
                observed: [limits.min().raw(), limits.max().raw()],
            });
        }
        if homing.homing_offset() != self.homing_offset {
            return Err(CalibrationError::ReadbackHoming {
                joint: self.joint,
                expected: self.homing_offset.get(),
                observed: homing.homing_offset().get(),
            });
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct UnitCalibration {
    unit: UnitId,
    joints: [JointCalibration; JOINT_COUNT],
}

impl UnitCalibration {
    pub fn new(
        unit: UnitId,
        records: [CalibrationRecord; JOINT_COUNT],
    ) -> Result<Self, CalibrationError> {
        let mut joints = [None; JOINT_COUNT];
        for record in records {
            let index = record.joint.index();
            if joints[index].is_some() {
                return Err(CalibrationError::DuplicateJoint(record.joint));
            }
            joints[index] = Some(JointCalibration::validate(record)?);
        }
        // Five unique typed identities exhaust exactly five array slots.
        let joints =
            joints.map(|value| value.expect("all five typed calibration records are present"));
        Ok(Self { unit, joints })
    }

    pub fn unit_id(&self) -> &UnitId {
        &self.unit
    }

    /// Require the expected unit identifier before exposing conversions. The
    /// caller must establish that identifier independently of this file.
    pub fn bind(&self, expected_unit: &UnitId) -> Result<BoundCalibration<'_>, CalibrationError> {
        if &self.unit != expected_unit {
            return Err(CalibrationError::UnitIdentityMismatch);
        }
        Ok(BoundCalibration { calibration: self })
    }
}

#[derive(Clone, Copy, Debug)]
pub struct BoundCalibration<'a> {
    calibration: &'a UnitCalibration,
}

impl BoundCalibration<'_> {
    pub fn joint(self, joint: Joint) -> JointCalibration {
        self.calibration.joints[joint.index()]
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CalibrationError {
    UnitIdentityMismatch,
    DuplicateJoint(Joint),
    ServoId {
        joint: Joint,
        found: u16,
    },
    DriveMode {
        joint: Joint,
        found: u8,
    },
    Units {
        joint: Joint,
        error: UnitError,
    },
    InvalidSpan {
        joint: Joint,
        min: u16,
        max: u16,
    },
    PositionOutsideSpan {
        joint: Joint,
        position: u16,
        min: u16,
        max: u16,
    },
    ReadbackJoint {
        expected: Joint,
        observed: Joint,
    },
    OperatingMode {
        joint: Joint,
        observed: u8,
    },
    ReadbackSpan {
        joint: Joint,
        expected: [u16; 2],
        observed: [u16; 2],
    },
    ReadbackHoming {
        joint: Joint,
        expected: i16,
        observed: i16,
    },
}

impl fmt::Display for CalibrationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnitIdentityMismatch => f.write_str("calibration belongs to a different unit"),
            Self::DuplicateJoint(joint) => write!(f, "calibration repeats {joint}"),
            Self::ServoId { joint, found } => {
                write!(f, "{joint} calibration uses servo ID {found}")
            }
            Self::DriveMode { joint, found } => {
                write!(f, "{joint} drive_mode {found} is not 0 or 1")
            }
            Self::Units { joint, error } => write!(f, "{joint} calibration units: {error}"),
            Self::InvalidSpan { joint, min, max } => write!(
                f,
                "{joint} calibration span {min}..={max} is empty or reversed"
            ),
            Self::PositionOutsideSpan {
                joint,
                position,
                min,
                max,
            } => write!(
                f,
                "{joint} position {position} is outside calibrated {min}..={max}"
            ),
            Self::ReadbackJoint { expected, observed } => {
                write!(f, "readback for {observed}, expected {expected}")
            }
            Self::OperatingMode { joint, observed } => write!(
                f,
                "{joint} operating mode {observed} is not position mode 0"
            ),
            Self::ReadbackSpan {
                joint,
                expected,
                observed,
            } => write!(
                f,
                "{joint} register span {observed:?} differs from calibration {expected:?}"
            ),
            Self::ReadbackHoming {
                joint,
                expected,
                observed,
            } => write!(
                f,
                "{joint} homing offset {observed} differs from calibration {expected}"
            ),
        }
    }
}

impl std::error::Error for CalibrationError {}
