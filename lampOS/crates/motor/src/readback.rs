//! Decoding for explicitly matched read windows. All physical scales stay raw.

use crate::{
    Joint,
    protocol::{MatchedReply, ReadWindow, ReplyKind},
    units::{EncoderWord, HomingOffset, UnitError},
};
use std::fmt;

impl MatchedReply {
    fn read_bytes(&self, expected: ReadWindow) -> Result<&[u8], DecodeError> {
        if self.kind() != ReplyKind::Read(expected) {
            return Err(DecodeError::WrongWindow {
                expected,
                actual: self.kind(),
            });
        }
        Ok(self.parameters())
    }

    /// Ping itself has no model bytes. This requires a separate register-3 read.
    pub fn model_number_raw(&self) -> Result<u16, DecodeError> {
        let data = self.read_bytes(ReadWindow::MODEL_NUMBER)?;
        Ok(u16::from_le_bytes([data[0], data[1]]))
    }

    pub fn position_limits(&self) -> Result<PositionLimitRegisters, DecodeError> {
        let data = self.read_bytes(ReadWindow::POSITION_LIMITS)?;
        Ok(PositionLimitRegisters {
            joint: self.joint(),
            min: EncoderWord::from_raw(u16::from_le_bytes([data[0], data[1]])),
            max: EncoderWord::from_raw(u16::from_le_bytes([data[2], data[3]])),
        })
    }

    pub fn homing_and_mode(&self) -> Result<HomingModeRegisters, DecodeError> {
        let data = self.read_bytes(ReadWindow::HOMING_AND_MODE)?;
        let homing_word = u16::from_le_bytes([data[0], data[1]]);
        Ok(HomingModeRegisters {
            joint: self.joint(),
            homing_word,
            homing_offset: HomingOffset::from_register(homing_word).map_err(DecodeError::Units)?,
            operating_mode_raw: data[2],
        })
    }

    /// Registers 56..=70, including otherwise uninterpreted gap bytes.
    pub fn telemetry(&self) -> Result<RawTelemetry, DecodeError> {
        let data = self.read_bytes(ReadWindow::TELEMETRY)?;
        let mut bytes = [0; 15];
        bytes.copy_from_slice(data);
        Ok(RawTelemetry {
            joint: self.joint(),
            bytes,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PositionLimitRegisters {
    joint: Joint,
    min: EncoderWord,
    max: EncoderWord,
}

impl PositionLimitRegisters {
    pub const fn joint(self) -> Joint {
        self.joint
    }
    pub const fn min(self) -> EncoderWord {
        self.min
    }
    pub const fn max(self) -> EncoderWord {
        self.max
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct HomingModeRegisters {
    joint: Joint,
    homing_word: u16,
    homing_offset: HomingOffset,
    operating_mode_raw: u8,
}

impl HomingModeRegisters {
    pub const fn joint(self) -> Joint {
        self.joint
    }
    pub const fn homing_word(self) -> u16 {
        self.homing_word
    }
    pub const fn homing_offset(self) -> HomingOffset {
        self.homing_offset
    }
    pub const fn operating_mode_raw(self) -> u8 {
        self.operating_mode_raw
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RawVelocity(u16);

impl RawVelocity {
    pub const fn word(self) -> u16 {
        self.0
    }

    /// Sign-magnitude bit 15; result remains unscaled velocity-register units.
    /// In particular 0x8001 is -1, not two's-complement -32767.
    pub fn signed_raw(self) -> i16 {
        let magnitude = (self.0 & 0x7fff) as i16;
        if self.0 & 0x8000 == 0 {
            magnitude
        } else {
            -magnitude
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RawLoad(u16);

impl RawLoad {
    pub const fn word(self) -> u16 {
        self.0
    }
    pub const fn magnitude_raw(self) -> u16 {
        self.0 & 0x03ff
    }
    pub const fn direction_bit(self) -> bool {
        self.0 & 0x0400 != 0
    }
    pub const fn uninterpreted_bits(self) -> u16 {
        self.0 & 0xf800
    }
}

/// Numerical register decoding only. No timestamp, freshness, force, degrees,
/// degrees/second, temperature scale, or voltage scale is asserted by this type.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RawTelemetry {
    joint: Joint,
    bytes: [u8; 15],
}

impl RawTelemetry {
    pub const fn joint(self) -> Joint {
        self.joint
    }
    pub const fn bytes(&self) -> &[u8; 15] {
        &self.bytes
    }

    fn word(self, offset: usize) -> u16 {
        u16::from_le_bytes([self.bytes[offset], self.bytes[offset + 1]])
    }

    pub fn position(self) -> EncoderWord {
        EncoderWord::from_raw(self.word(0))
    }
    pub fn velocity(self) -> RawVelocity {
        RawVelocity(self.word(2))
    }
    pub fn load(self) -> RawLoad {
        RawLoad(self.word(4))
    }
    pub const fn voltage_raw(self) -> u8 {
        self.bytes[6]
    }
    pub const fn temperature_raw(self) -> u8 {
        self.bytes[7]
    }
    /// Register 65, distinct from the status packet's error byte.
    pub const fn status_raw(self) -> u8 {
        self.bytes[9]
    }
    pub const fn moving_raw(self) -> u8 {
        self.bytes[10]
    }
    pub fn current_raw(self) -> u16 {
        self.word(13)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DecodeError {
    WrongWindow {
        expected: ReadWindow,
        actual: ReplyKind,
    },
    Units(UnitError),
}

impl fmt::Display for DecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::WrongWindow { expected, actual } => {
                write!(f, "cannot decode {actual:?} as {expected:?}")
            }
            Self::Units(error) => write!(f, "register decoding: {error}"),
        }
    }
}

impl std::error::Error for DecodeError {}
