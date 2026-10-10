//! Explicit, bounded camera provisioning. Loading never opens a device.
use lamp_camera::{CaptureConfig, FrameInterval, SourceId};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::OpenOptions,
    io::{self, Read},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Component, Path, PathBuf},
};

const MAX_CONFIG_BYTES: usize = 8192;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UsbIdentity {
    pub vendor: u16,
    pub product: u16,
    pub topology: String,
    pub serial: Option<String>,
    pub interface_number: u8,
    pub capture_index: u8,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Interval {
    pub numerator: u32,
    pub denominator: u32,
}
impl Interval {
    pub fn checked(self) -> io::Result<FrameInterval> {
        FrameInterval::new(self.numerator, self.denominator).map_err(io::Error::other)
    }
}
impl From<FrameInterval> for Interval {
    fn from(value: FrameInterval) -> Self {
        Self {
            numerator: value.numerator(),
            denominator: value.denominator(),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CameraConfig {
    pub device: PathBuf,
    pub lock_directory: PathBuf,
    pub source: String,
    pub expected_usb: UsbIdentity,
    pub width: u32,
    pub height: u32,
    pub interval: Option<Interval>,
    pub max_frame_bytes: usize,
    pub buffers: u8,
}

pub fn valid_absolute(path: &Path) -> bool {
    path.is_absolute()
        && path.as_os_str().len() <= 4096
        && !path
            .components()
            .any(|part| matches!(part, Component::ParentDir | Component::CurDir))
}
impl CameraConfig {
    pub fn capture(&self) -> io::Result<CaptureConfig> {
        let usb = &self.expected_usb;
        if !valid_absolute(&self.device)
            || !valid_absolute(&self.lock_directory)
            || usb.vendor == 0
            || usb.product == 0
            || usb.topology.is_empty()
            || usb.topology.len() > 128
            || !usb
                .topology
                .bytes()
                .all(|c| c.is_ascii_digit() || b"-.:".contains(&c))
            || usb.serial.as_ref().is_some_and(|s| {
                s.is_empty()
                    || s.len() > 256
                    || !s.bytes().all(|c| c.is_ascii_graphic() || c == b' ')
            })
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid explicit camera path or USB identity",
            ));
        }
        let config = CaptureConfig {
            source: SourceId::new(&self.source).map_err(io::Error::other)?,
            width: self.width,
            height: self.height,
            interval: self.interval.map(Interval::checked).transpose()?,
            max_frame_bytes: self.max_frame_bytes,
            buffers: self.buffers,
        };
        config.validate().map_err(io::Error::other)?;
        Ok(config)
    }
    /// The parent passes this exact digest to the child. A changed provisioning
    /// file cannot silently select another device between parent and child reads.
    pub fn load(path: &Path, expected_digest: Option<&str>) -> io::Result<(Self, String)> {
        if !valid_absolute(path) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "camera config path must be absolute",
            ));
        }
        let flags = rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::NONBLOCK
            | rustix::fs::OFlags::CLOEXEC;
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(flags.bits() as i32)
            .open(path)?;
        let info = file.metadata()?;
        if !info.is_file()
            || info.len() > MAX_CONFIG_BYTES as u64
            || info.mode() & 0o077 != 0
            || info.uid() != rustix::process::geteuid().as_raw()
            || info.nlink() != 1
        {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "camera config must be a singly linked private regular file owned by this user",
            ));
        }
        let mut bytes = Vec::with_capacity(info.len() as usize);
        (&mut file)
            .take(MAX_CONFIG_BYTES as u64 + 1)
            .read_to_end(&mut bytes)?;
        if bytes.len() > MAX_CONFIG_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "camera configuration too large",
            ));
        }
        let digest = format!("{:x}", Sha256::digest(&bytes));
        if expected_digest.is_some_and(|expected| expected != digest) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "camera configuration digest changed",
            ));
        }
        let config: Self = serde_json::from_slice(&bytes).map_err(|_| {
            io::Error::new(io::ErrorKind::InvalidData, "invalid camera configuration")
        })?;
        config.capture()?;
        Ok((config, digest))
    }
    #[cfg(target_os = "linux")]
    pub fn linux(&self) -> io::Result<lamp_camera::linux::LinuxConfig> {
        let capture = self.capture()?;
        let usb = &self.expected_usb;
        lamp_camera::linux::LinuxConfig::new(
            self.device.clone(),
            self.lock_directory.clone(),
            capture.source,
            lamp_camera::linux::UsbIdentity {
                vendor: usb.vendor,
                product: usb.product,
                topology: usb.topology.clone(),
                serial: usb.serial.clone(),
                interface_number: usb.interface_number,
                capture_index: usb.capture_index,
            },
        )
        .map_err(|error| io::Error::other(format!("invalid Linux camera configuration: {error:?}")))
    }
}
