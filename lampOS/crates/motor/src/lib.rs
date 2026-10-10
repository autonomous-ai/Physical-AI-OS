//! Read-only Feetech STS3215 protocol and per-unit Lamp calibration.
//!
//! Linux provides one finite read-only inspection with explicit device/unit/lock
//! paths. There are no register-write, torque, or movement APIs. Packet integrity,
//! serial exclusivity, and calibration consistency are not hardware qualification.
//! A future motion owner still needs proven units, placement, safety limits and
//! the final movement ownership/cancellation boundary.

use std::fmt;

#[cfg(any(target_os = "linux", test))]
pub mod inspection;
#[cfg(target_os = "linux")]
pub mod linux;

pub mod calibration;
pub mod protocol;
pub mod readback;
pub mod units;

pub const JOINT_COUNT: usize = 5;
/// Model number from the pinned STS3215 SDK table, not a device observation.
pub const STS3215_MODEL_NUMBER: u16 = 777;

/// Fixed Lamp wiring identity. These are bus IDs, not array offsets.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
#[repr(u8)]
pub enum Joint {
    BaseYaw = 1,
    BasePitch = 2,
    ElbowPitch = 3,
    WristRoll = 4,
    WristPitch = 5,
}

impl Joint {
    pub const ALL: [Self; JOINT_COUNT] = [
        Self::BaseYaw,
        Self::BasePitch,
        Self::ElbowPitch,
        Self::WristRoll,
        Self::WristPitch,
    ];

    pub const fn id(self) -> u8 {
        self as u8
    }

    pub const fn index(self) -> usize {
        self as usize - 1
    }

    pub const fn name(self) -> &'static str {
        match self {
            Self::BaseYaw => "base_yaw",
            Self::BasePitch => "base_pitch",
            Self::ElbowPitch => "elbow_pitch",
            Self::WristRoll => "wrist_roll",
            Self::WristPitch => "wrist_pitch",
        }
    }

    pub(crate) const fn mask(self) -> u8 {
        1 << self.index()
    }
}

impl TryFrom<u8> for Joint {
    type Error = UnknownJointId;

    fn try_from(value: u8) -> Result<Self, Self::Error> {
        match value {
            1 => Ok(Self::BaseYaw),
            2 => Ok(Self::BasePitch),
            3 => Ok(Self::ElbowPitch),
            4 => Ok(Self::WristRoll),
            5 => Ok(Self::WristPitch),
            _ => Err(UnknownJointId(value)),
        }
    }
}

impl fmt::Display for Joint {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.name())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct UnknownJointId(pub u8);

impl fmt::Display for UnknownJointId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "servo ID {} is not one of Lamp's five joints", self.0)
    }
}

impl std::error::Error for UnknownJointId {}
