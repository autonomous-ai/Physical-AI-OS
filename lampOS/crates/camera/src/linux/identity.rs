use super::{LinuxConfig, UsbIdentity};
use crate::PortError;
use crate::backend::{Check, Fault, Stage, checked};
use rustix::fs::{self, FileType, FlockOperation, Mode, OFlags};
use std::fs::File;
use std::io::Read;
use std::os::fd::{IntoRawFd, OwnedFd};
use std::os::unix::fs::MetadataExt;
use std::path::{Component, Path, PathBuf};

/// Observed kernel/sysfs identity; neither a serial nor a USB path authenticates
/// hardware. All strings are bounded before retention.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IdentityReport {
    pub canonical_node: PathBuf,
    pub major: u32,
    pub minor: u32,
    pub inode: u64,
    pub usb: UsbIdentity,
}

pub(super) struct Opened {
    pub fd: Option<OwnedFd>,
    lock: Option<OwnedFd>,
    pub report: IdentityReport,
}
impl Opened {
    pub(super) fn close(&mut self) -> Result<(), PortError> {
        let mut first = None;
        for fd in [&mut self.fd, &mut self.lock] {
            if let Some(fd) = fd.take() {
                // Consumes ownership; close is never retried even on error.
                if let Err(error) = v4l2r::nix::unistd::close(fd.into_raw_fd()) {
                    first.get_or_insert(super::errno(error as i32));
                }
            }
        }
        first.map_or(Ok(()), Err)
    }
}
impl Drop for Opened {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

pub(super) fn valid_path(path: &Path) -> bool {
    path.is_absolute()
        && path.as_os_str().len() <= 4096
        && !path
            .components()
            .any(|c| matches!(c, Component::ParentDir | Component::CurDir))
}
fn io_error(error: std::io::Error) -> PortError {
    error
        .raw_os_error()
        .map(super::errno)
        .unwrap_or(PortError::InvalidData)
}
fn trusted(mode: u32, uid: u32, current: u32, last: bool) -> Result<(), PortError> {
    if (uid != 0 && uid != current)
        || mode & 0o022 != 0
        || (last && (uid != current || mode & 0o777 != 0o700))
    {
        return Err(PortError::InvalidData);
    }
    Ok(())
}
fn directory(path: &Path, check: &mut impl Check) -> Result<OwnedFd, Fault> {
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC | OFlags::NOFOLLOW;
    let uid = rustix::process::geteuid().as_raw();
    let mut fd = checked(check, Stage::Lock, || {
        fs::open("/", flags, Mode::empty()).map_err(|e| super::errno(e.raw_os_error()))
    })?;
    let stat = checked(check, Stage::Lock, || {
        fs::fstat(&fd).map_err(|e| super::errno(e.raw_os_error()))
    })?;
    trusted(stat.st_mode, stat.st_uid, uid, false).map_err(|e| Fault::new(Stage::Lock, e))?;
    for part in path.components() {
        if let Component::Normal(name) = part {
            let next = checked(check, Stage::Lock, || {
                fs::openat(&fd, name, flags, Mode::empty())
                    .map_err(|e| super::errno(e.raw_os_error()))
            })?;
            let stat = checked(check, Stage::Lock, || {
                fs::fstat(&next).map_err(|e| super::errno(e.raw_os_error()))
            })?;
            trusted(stat.st_mode, stat.st_uid, uid, false)
                .map_err(|e| Fault::new(Stage::Lock, e))?;
            fd = next;
        }
    }
    let stat = checked(check, Stage::Lock, || {
        fs::fstat(&fd).map_err(|e| super::errno(e.raw_os_error()))
    })?;
    trusted(stat.st_mode, stat.st_uid, uid, true).map_err(|e| Fault::new(Stage::Lock, e))?;
    Ok(fd)
}
fn text(path: &Path, check: &mut impl Check) -> Result<String, Fault> {
    checked(check, Stage::Identity, || {
        let mut data = [0_u8; 257];
        let mut file = File::open(path).map_err(io_error)?.take(257);
        let mut used = 0;
        while used < data.len() {
            let size = file.read(&mut data[used..]).map_err(io_error)?;
            if size == 0 {
                break;
            }
            used += size;
        }
        if used == data.len() {
            return Err(PortError::InvalidData);
        }
        let value = std::str::from_utf8(&data[..used])
            .map_err(|_| PortError::InvalidData)?
            .trim_end_matches('\n');
        if value.is_empty() || !value.bytes().all(|c| c.is_ascii_graphic() || c == b' ') {
            return Err(PortError::InvalidData);
        }
        Ok(value.to_owned())
    })
}
fn optional_text(path: &Path, check: &mut impl Check) -> Result<Option<String>, Fault> {
    match text(path, check) {
        Ok(value) => Ok(Some(value)),
        Err(Fault {
            error: PortError::Io(2),
            ..
        }) => Ok(None),
        Err(error) => Err(error),
    }
}
fn hex16(value: &str) -> Result<u16, Fault> {
    if value.len() != 4 || !value.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(Fault::invalid(Stage::Identity));
    }
    u16::from_str_radix(value, 16).map_err(|_| Fault::invalid(Stage::Identity))
}
fn observe_usb(major: u32, minor: u32, check: &mut impl Check) -> Result<UsbIdentity, Fault> {
    let path = PathBuf::from(format!("/sys/dev/char/{major}:{minor}"));
    let mut location = checked(check, Stage::Identity, || {
        std::fs::canonicalize(&path).map_err(io_error)
    })?;
    if !location.starts_with("/sys/devices") {
        return Err(Fault::invalid(Stage::Identity));
    }
    let capture_index = text(&location.join("index"), check)?
        .parse::<u8>()
        .map_err(|_| Fault::invalid(Stage::Identity))?;
    let mut interface_number = None;
    // This follows this node's ancestry only. It never enumerates other cameras.
    for _ in 0..16 {
        if let Some(value) = optional_text(&location.join("bInterfaceNumber"), check)? {
            if interface_number.is_some() || value.len() != 2 {
                return Err(Fault::invalid(Stage::Identity));
            }
            interface_number =
                Some(u8::from_str_radix(&value, 16).map_err(|_| Fault::invalid(Stage::Identity))?);
        }
        if let Some(vendor) = optional_text(&location.join("idVendor"), check)? {
            let product = text(&location.join("idProduct"), check)?;
            let topology = location
                .file_name()
                .and_then(|s| s.to_str())
                .ok_or_else(|| Fault::invalid(Stage::Identity))?
                .to_owned();
            let usb = UsbIdentity {
                vendor: hex16(&vendor)?,
                product: hex16(&product)?,
                topology,
                serial: optional_text(&location.join("serial"), check)?,
                interface_number: interface_number
                    .ok_or_else(|| Fault::invalid(Stage::Identity))?,
                capture_index,
            };
            usb.validate().map_err(|e| Fault::new(Stage::Identity, e))?;
            return Ok(usb);
        }
        if !location.pop() || !location.starts_with("/sys/devices") {
            break;
        }
    }
    Err(Fault::invalid(Stage::Identity))
}

pub(super) fn open(config: &LinuxConfig, check: &mut impl Check) -> Result<Opened, Fault> {
    let canonical = checked(check, Stage::Identity, || {
        std::fs::canonicalize(&config.device).map_err(io_error)
    })?;
    if !valid_path(&canonical) || !canonical.starts_with("/dev") {
        return Err(Fault::invalid(Stage::Identity));
    }
    let stat = checked(check, Stage::Identity, || {
        std::fs::symlink_metadata(&canonical).map_err(io_error)
    })?;
    if FileType::from_raw_mode(stat.mode()) != FileType::CharacterDevice {
        return Err(Fault::invalid(Stage::Identity));
    }
    let major = fs::major(stat.rdev());
    let minor = fs::minor(stat.rdev());
    let before = observe_usb(major, minor, check)?;
    if !config.expected.matches(&before) {
        return Err(Fault::invalid(Stage::Identity));
    }
    let directory = directory(&config.lock_directory, check)?;
    let name = format!("camera-{major}-{minor}.lock");
    let lock = checked(check, Stage::Lock, || {
        fs::openat(
            &directory,
            name.as_str(),
            OFlags::RDWR | OFlags::CREATE | OFlags::CLOEXEC | OFlags::NOFOLLOW,
            Mode::RUSR | Mode::WUSR,
        )
        .map_err(|e| super::errno(e.raw_os_error()))
    })?;
    let metadata = checked(check, Stage::Lock, || {
        fs::fstat(&lock).map_err(|e| super::errno(e.raw_os_error()))
    })?;
    if FileType::from_raw_mode(metadata.st_mode) != FileType::RegularFile
        || metadata.st_nlink != 1
        || metadata.st_uid != rustix::process::geteuid().as_raw()
        || metadata.st_mode & 0o777 != 0o600
    {
        return Err(Fault::invalid(Stage::Lock));
    }
    checked(check, Stage::Lock, || {
        fs::flock(&lock, FlockOperation::NonBlockingLockExclusive)
            .map_err(|e| super::errno(e.raw_os_error()))
    })?;
    let fd = checked(check, Stage::Open, || {
        fs::open(
            &canonical,
            OFlags::RDWR | OFlags::NONBLOCK | OFlags::NOCTTY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
            Mode::empty(),
        )
        .map_err(|e| super::errno(e.raw_os_error()))
    })?;
    let actual = checked(check, Stage::Identity, || {
        fs::fstat(&fd).map_err(|e| super::errno(e.raw_os_error()))
    })?;
    if FileType::from_raw_mode(actual.st_mode) != FileType::CharacterDevice
        || actual.st_rdev != stat.rdev()
        || actual.st_ino != stat.ino()
        || actual.st_dev != stat.dev()
    {
        return Err(Fault::invalid(Stage::Identity));
    }
    checked(check, Stage::Lock, || {
        fs::flock(&fd, FlockOperation::NonBlockingLockExclusive)
            .map_err(|e| super::errno(e.raw_os_error()))
    })?;
    let after = observe_usb(major, minor, check)?;
    if before != after {
        return Err(Fault::invalid(Stage::Identity));
    }
    Ok(Opened {
        fd: Some(fd),
        lock: Some(lock),
        report: IdentityReport {
            canonical_node: canonical,
            major,
            minor,
            inode: actual.st_ino,
            usb: after,
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn lock_ancestors_and_final_directory_are_private_and_trusted() {
        assert!(trusted(0o755, 0, 1000, false).is_ok());
        assert!(trusted(0o700, 1000, 1000, true).is_ok());
        for (mode, uid, last) in [
            (0o777, 0, false),
            (0o775, 1000, false),
            (0o755, 1000, true),
            (0o700, 0, true),
            (0o700, 99, false),
        ] {
            assert!(trusted(mode, uid, 1000, last).is_err());
        }
    }
    #[test]
    fn no_relative_or_parent_paths_and_no_oversized_usb_values() {
        assert!(valid_path(Path::new("/dev/device-camera")));
        for bad in ["video0", "/dev/../video0"] {
            assert!(!valid_path(Path::new(bad)));
        }
        for bad in ["1", "10000", "fffff", "----"] {
            assert!(hex16(bad).is_err());
        }
        assert_eq!(hex16("1bcf").unwrap(), 0x1bcf);
    }
}
