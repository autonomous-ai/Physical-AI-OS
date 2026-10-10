//! Register coordinates and dimensionless calibration units. No degrees or force.

use std::fmt;

pub const ENCODER_MAX: u16 = 4095;

/// An unqualified Present_Position register. Non-position modes may use values
/// outside the single-turn coordinate accepted by `EncoderCounts`.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct EncoderWord(u16);

impl EncoderWord {
    pub const fn from_raw(word: u16) -> Self {
        Self(word)
    }

    pub const fn raw(self) -> u16 {
        self.0
    }

    pub fn single_turn_counts(self) -> Result<EncoderCounts, UnitError> {
        EncoderCounts::new(i32::from(self.0))
    }
}

/// 0..=4095 counts in the servo's homing-adjusted single-turn coordinate.
/// Valid numeric range alone does not establish the device's operating mode.
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct EncoderCounts(u16);

impl EncoderCounts {
    pub fn new(value: i32) -> Result<Self, UnitError> {
        if !(0..=i32::from(ENCODER_MAX)).contains(&value) {
            return Err(UnitError::EncoderOutOfRange(value));
        }
        Ok(Self(value as u16))
    }

    pub const fn get(self) -> u16 {
        self.0
    }
}

/// Legacy RANGE_M100_100 coordinates. These are not angles or safety limits.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct NormalizedPosition(f64);

impl NormalizedPosition {
    pub fn new(value: f64) -> Result<Self, UnitError> {
        if !value.is_finite() {
            return Err(UnitError::NormalizedNotFinite);
        }
        if !(-100.0..=100.0).contains(&value) {
            return Err(UnitError::NormalizedOutOfRange);
        }
        Ok(Self(value))
    }

    pub const fn get(self) -> f64 {
        self.0
    }
}

/// Homing register 31: sign-magnitude with sign bit 11, magnitude <= 2047.
/// Present_Position already includes this offset; do not subtract it again.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct HomingOffset(i16);

impl HomingOffset {
    pub fn new(value: i32) -> Result<Self, UnitError> {
        if !(-2047..=2047).contains(&value) {
            return Err(UnitError::HomingOutOfRange(value));
        }
        Ok(Self(value as i16))
    }

    pub fn from_register(word: u16) -> Result<Self, UnitError> {
        if word & 0xf000 != 0 {
            return Err(UnitError::HomingReservedBits(word));
        }
        let magnitude = (word & 0x07ff) as i16;
        Ok(Self(if word & 0x0800 == 0 {
            magnitude
        } else {
            -magnitude
        }))
    }

    pub const fn get(self) -> i16 {
        self.0
    }

    /// Canonical numeric representation only; no register-write packet exists.
    /// A readback negative zero decodes as zero and encodes as positive zero.
    pub fn register_word(self) -> u16 {
        self.0.unsigned_abs() | if self.0 < 0 { 0x0800 } else { 0 }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum UnitError {
    EncoderOutOfRange(i32),
    NormalizedNotFinite,
    NormalizedOutOfRange,
    HomingOutOfRange(i32),
    HomingReservedBits(u16),
}

impl fmt::Display for UnitError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EncoderOutOfRange(value) => {
                write!(f, "encoder count {value} is outside 0..=4095")
            }
            Self::NormalizedNotFinite => f.write_str("normalized position must be finite"),
            Self::NormalizedOutOfRange => f.write_str("normalized position is outside -100..=100"),
            Self::HomingOutOfRange(value) => {
                write!(f, "homing offset {value} exceeds sign-bit-11 magnitude")
            }
            Self::HomingReservedBits(word) => {
                write!(f, "homing word {word:#06x} has reserved upper bits")
            }
        }
    }
}

impl std::error::Error for UnitError {}
