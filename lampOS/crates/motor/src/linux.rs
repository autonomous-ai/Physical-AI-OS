//! One finite read-only inspection through an exclusively owned Linux tty.
//!
//! Opening an unqualified USB adapter can change modem lines in the kernel.
//! This module sends no modem, break, reset, register-write or torque commands.
//! All deadlines are observed between syscalls, not kernel preemption guarantees.

use crate::{
    calibration::UnitId,
    inspection::{self, InspectionReport, Interest, PortIo, Runtime, TRANSACTION_DEADLINE},
};
use rustix::{
    event::{PollFd, PollFlags, Timespec, poll},
    fd::OwnedFd,
    fs::{self, FileType, FlockOperation, Mode, OFlags},
    termios::{
        self, ControlModes, InputModes, OptionalActions, QueueSelector, SpecialCodeIndex, Termios,
    },
};
use serde::Serialize;
use std::{
    fs::File,
    io::{self, Read},
    os::{
        fd::IntoRawFd,
        unix::fs::{FileTypeExt, MetadataExt},
    },
    path::{Component, Path, PathBuf},
    time::{Duration, Instant},
};

const BAUD: u32 = 1_000_000;
const CAP_SYS_ADMIN_BIT: u64 = 1 << 21;
const MAX_PATH_BYTES: usize = 4096;
const MAX_COMPONENTS: usize = 64;
const MAX_STATUS_BYTES: u64 = 16_384;

/// No implicit device, unit identifier, lock location or calibration fallback.
#[derive(Clone, Debug)]
pub struct InspectionConfig {
    device: PathBuf,
    unit_id: UnitId,
    lock_directory: PathBuf,
}

impl InspectionConfig {
    pub fn new(device: PathBuf, unit_id: UnitId, lock_directory: PathBuf) -> io::Result<Self> {
        validate_path(&device)?;
        validate_path(&lock_directory)?;
        Ok(Self {
            device,
            unit_id,
            lock_directory,
        })
    }
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn denied(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}
fn validate_path(path: &Path) -> io::Result<()> {
    if !path.is_absolute()
        || path.as_os_str().len() > MAX_PATH_BYTES
        || path.to_str().is_none()
        || path.components().count() > MAX_COMPONENTS
        || path
            .components()
            .any(|part| matches!(part, Component::ParentDir))
    {
        return Err(invalid(
            "path must be bounded absolute UTF-8 with no parent traversal",
        ));
    }
    Ok(())
}

#[derive(Clone, Debug, Serialize)]
pub struct Diagnostic {
    pub operation: &'static str,
    pub code: Option<i32>,
    pub detail: String,
}
impl Diagnostic {
    fn new(operation: &'static str, error: io::Error) -> Self {
        Self {
            operation,
            code: error.raw_os_error(),
            detail: error.to_string().chars().take(192).collect(),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct DeviceIdentity {
    pub filesystem_device: u64,
    pub inode: u64,
    pub character_device: u64,
    pub major: u32,
    pub minor: u32,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct SerialSettings {
    pub input_baud: u32,
    pub output_baud: u32,
    pub input_flags_raw: u32,
    pub output_flags_raw: u32,
    pub control_flags_raw: u32,
    pub local_flags_raw: u32,
    pub line_discipline: u8,
    pub vmin: u8,
    pub vtime: u8,
    /// All Linux-defined termios control characters, in the documented order.
    pub control_characters: [u8; 17],
    pub hangup_on_close: bool,
}

impl SerialSettings {
    fn read(value: &Termios) -> Self {
        use SpecialCodeIndex as S;
        let keys = [
            S::VINTR,
            S::VQUIT,
            S::VERASE,
            S::VKILL,
            S::VEOF,
            S::VTIME,
            S::VMIN,
            S::VSWTC,
            S::VSTART,
            S::VSTOP,
            S::VSUSP,
            S::VEOL,
            S::VREPRINT,
            S::VDISCARD,
            S::VWERASE,
            S::VLNEXT,
            S::VEOL2,
        ];
        Self {
            input_baud: value.input_speed(),
            output_baud: value.output_speed(),
            input_flags_raw: value.input_modes.bits(),
            output_flags_raw: value.output_modes.bits(),
            control_flags_raw: value.control_modes.bits(),
            local_flags_raw: value.local_modes.bits(),
            line_discipline: value.line_discipline,
            vmin: value.special_codes[S::VMIN],
            vtime: value.special_codes[S::VTIME],
            control_characters: keys.map(|key| value.special_codes[key]),
            hangup_on_close: value.control_modes.contains(ControlModes::HUPCL),
        }
    }
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct CleanupReport {
    pub started_us: u64,
    pub ended_us: u64,
    pub output_flush_attempted: bool,
    pub prior_settings_restore_attempted: bool,
    pub restored_settings: Option<SerialSettings>,
    pub hupcl_intentionally_not_restored: bool,
    pub tty_exclusive_release_attempted: bool,
    pub tty_close_attempted: bool,
    /// Fixed cleanup work; failures do not skip the subsequent close attempt.
    pub errors: Vec<Diagnostic>,
}

#[derive(Clone, Debug, Serialize)]
pub struct InspectionResult {
    pub schema_version: u32,
    pub unit_id: String,
    pub unit_identity_verified: bool,
    pub requested_device: PathBuf,
    pub resolved_device: Option<PathBuf>,
    pub lock_directory: PathBuf,
    pub device_identity: Option<DeviceIdentity>,
    pub timestamp_domain: &'static str,
    pub tty_open_started_us: Option<u64>,
    pub tty_open_completed_us: Option<u64>,
    pub prepared_us: Option<u64>,
    pub elapsed_us: u64,
    pub adapter_open_close_effects_qualified: bool,
    pub tty_exclusive_applied: bool,
    pub preexisting_foreign_handles_excluded: bool,
    pub prior_settings: Option<SerialSettings>,
    pub negotiated_settings: Option<SerialSettings>,
    pub preparation_error: Option<Diagnostic>,
    pub inspection: Option<InspectionReport>,
    pub cleanup: CleanupReport,
    pub completed: bool,
}

fn micros(origin: Instant) -> u64 {
    origin.elapsed().as_micros().try_into().unwrap_or(u64::MAX)
}

/// Open, inspect exactly once, and explicitly close. Cancellation must itself be
/// nonblocking; it is sampled before/after bounded work. The caller must stop
/// every foreign bus owner first and supervise syscalls that do not return.
///
/// The unit ID is a caller label, not an authenticated servo identity. A complete
/// result is not proof that old same-length wire replies could not match.
#[must_use]
pub fn inspect_once(
    config: &InspectionConfig,
    mut cancelled: impl FnMut() -> io::Result<bool>,
) -> InspectionResult {
    let origin = Instant::now();
    let mut result = InspectionResult {
        schema_version: 1,
        unit_id: config.unit_id.as_str().to_owned(),
        unit_identity_verified: false,
        requested_device: config.device.clone(),
        resolved_device: None,
        lock_directory: config.lock_directory.clone(),
        device_identity: None,
        timestamp_domain: "host_monotonic_microseconds_since_inspect_once_entry",
        tty_open_started_us: None,
        tty_open_completed_us: None,
        prepared_us: None,
        elapsed_us: 0,
        adapter_open_close_effects_qualified: false,
        tty_exclusive_applied: false,
        preexisting_foreign_handles_excluded: false,
        prior_settings: None,
        negotiated_settings: None,
        preparation_error: None,
        inspection: None,
        cleanup: CleanupReport::default(),
        completed: false,
    };
    let mut owner = Owner::default();
    match owner.prepare(config, &mut result, origin, &mut cancelled) {
        Ok(()) => {
            result.prepared_us = Some(micros(origin));
            let mut runtime = HostRuntime {
                origin,
                cancelled: &mut cancelled,
            };
            // The only hardware write path is the private, fixed inspection plan.
            let mut port = LinuxPort {
                fd: owner.fd.as_ref().expect("prepared tty"),
            };
            result.inspection = Some(inspection::run(&mut port, &mut runtime));
        }
        Err(error) => result.preparation_error = Some(error),
    }
    let flush = result.inspection.as_ref().is_some_and(|r| !r.completed);
    result.cleanup = owner.finish(origin, flush);
    result.elapsed_us = micros(origin);
    result.completed = result.preparation_error.is_none()
        && result.inspection.as_ref().is_some_and(|r| r.completed)
        && result.cleanup.errors.is_empty();
    result
}

struct HostRuntime<'a, F> {
    origin: Instant,
    cancelled: &'a mut F,
}
impl<F: FnMut() -> io::Result<bool>> Runtime for HostRuntime<'_, F> {
    fn now(&self) -> Duration {
        self.origin.elapsed()
    }
    fn cancelled(&mut self) -> io::Result<bool> {
        (self.cancelled)()
    }
}

fn checkpoint(origin: Instant, cancelled: &mut impl FnMut() -> io::Result<bool>) -> io::Result<()> {
    if cancelled()? {
        return Err(io::Error::new(
            io::ErrorKind::Interrupted,
            "inspection cancelled before readiness",
        ));
    }
    if origin.elapsed() >= TRANSACTION_DEADLINE {
        return Err(io::Error::new(
            io::ErrorKind::TimedOut,
            "inspection preparation exceeded 200 ms fault deadline",
        ));
    }
    Ok(())
}

fn trusted_metadata(
    mode: u32,
    owner: u32,
    current_uid: u32,
    final_directory: bool,
) -> io::Result<()> {
    if owner != 0 && owner != current_uid {
        return Err(denied("lock directory ancestor has an untrusted owner"));
    }
    if mode & 0o022 != 0 {
        return Err(denied("lock directory ancestor is group/world writable"));
    }
    if final_directory && mode & 0o777 != 0o700 {
        return Err(denied("lock directory must already exist with mode 0700"));
    }
    Ok(())
}

fn trusted_lock_directory(
    path: &Path,
    origin: Instant,
    cancelled: &mut impl FnMut() -> io::Result<bool>,
) -> io::Result<OwnedFd> {
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC | OFlags::NOFOLLOW;
    let uid = rustix::process::geteuid().as_raw();
    checkpoint(origin, cancelled)?;
    let mut directory = fs::open("/", flags, Mode::empty())?;
    let root = fs::fstat(&directory)?;
    trusted_metadata(root.st_mode, root.st_uid, uid, false)?;
    for part in path.components() {
        if let Component::Normal(name) = part {
            checkpoint(origin, cancelled)?;
            let next = fs::openat(&directory, name, flags, Mode::empty())?;
            let metadata = fs::fstat(&next)?;
            trusted_metadata(metadata.st_mode, metadata.st_uid, uid, false)?;
            directory = next;
        }
    }
    checkpoint(origin, cancelled)?;
    let metadata = fs::fstat(&directory)?;
    trusted_metadata(metadata.st_mode, metadata.st_uid, uid, true)?;
    Ok(directory)
}

fn effective_capabilities(status: &str) -> io::Result<u64> {
    let mut fields = status
        .lines()
        .filter_map(|line| line.strip_prefix("CapEff:"));
    let value = fields
        .next()
        .ok_or_else(|| invalid("effective capability field unavailable"))?
        .trim();
    if fields.next().is_some()
        || value.is_empty()
        || value.len() > 16
        || !value.bytes().all(|b| b.is_ascii_hexdigit())
    {
        return Err(invalid("malformed effective capability field"));
    }
    u64::from_str_radix(value, 16).map_err(|_| invalid("invalid effective capability field"))
}

fn reject_exclusive_bypass() -> io::Result<()> {
    let mut bytes = Vec::with_capacity(2048);
    File::open("/proc/thread-self/status")?
        .take(MAX_STATUS_BYTES + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_STATUS_BYTES {
        return Err(invalid("capability status exceeded bound"));
    }
    let text =
        std::str::from_utf8(&bytes).map_err(|_| invalid("invalid capability status encoding"))?;
    if effective_capabilities(text)? & CAP_SYS_ADMIN_BIT != 0 {
        return Err(denied(
            "CAP_SYS_ADMIN bypasses tty exclusivity; use an unprivileged worker",
        ));
    }
    Ok(())
}

#[derive(Default)]
struct Owner {
    fd: Option<OwnedFd>,
    lock: Option<OwnedFd>,
    original: Option<Termios>,
    exclusive: bool,
}

impl Owner {
    fn prepare(
        &mut self,
        config: &InspectionConfig,
        report: &mut InspectionResult,
        origin: Instant,
        cancelled: &mut impl FnMut() -> io::Result<bool>,
    ) -> Result<(), Diagnostic> {
        let mut stage = "preflight";
        let result = (|| -> io::Result<()> {
            checkpoint(origin, cancelled)?;
            reject_exclusive_bypass()?;
            checkpoint(origin, cancelled)?;
            let path = std::fs::canonicalize(&config.device)?;
            validate_path(&path)?;
            let expected = std::fs::metadata(&path)?;
            if !expected.file_type().is_char_device() {
                return Err(invalid("explicit device is not a character device"));
            }
            let identity = DeviceIdentity {
                filesystem_device: expected.dev(),
                inode: expected.ino(),
                character_device: expected.rdev(),
                major: fs::major(expected.rdev()),
                minor: fs::minor(expected.rdev()),
            };
            report.resolved_device = Some(path.clone());
            report.device_identity = Some(identity);
            stage = "pre_open_lock";
            let directory = trusted_lock_directory(&config.lock_directory, origin, cancelled)?;
            let name = format!("motor-{}-{}.lock", identity.major, identity.minor);
            checkpoint(origin, cancelled)?;
            let lock = fs::openat(
                &directory,
                name,
                OFlags::RDWR | OFlags::CREATE | OFlags::CLOEXEC | OFlags::NOFOLLOW,
                Mode::RUSR | Mode::WUSR,
            )?;
            let metadata = fs::fstat(&lock)?;
            if FileType::from_raw_mode(metadata.st_mode) != FileType::RegularFile
                || metadata.st_nlink != 1
                || (metadata.st_uid != 0 && metadata.st_uid != rustix::process::geteuid().as_raw())
                || metadata.st_mode & 0o777 != 0o600
            {
                return Err(denied(
                    "lock must be a private, trusted, singly linked regular 0600 file",
                ));
            }
            fs::flock(&lock, FlockOperation::NonBlockingLockExclusive)?;
            self.lock = Some(lock);
            checkpoint(origin, cancelled)?;
            stage = "tty_open";
            report.tty_open_started_us = Some(micros(origin));
            self.fd = Some(fs::open(
                &path,
                OFlags::RDWR
                    | OFlags::NONBLOCK
                    | OFlags::NOCTTY
                    | OFlags::CLOEXEC
                    | OFlags::NOFOLLOW,
                Mode::empty(),
            )?);
            report.tty_open_completed_us = Some(micros(origin));
            checkpoint(origin, cancelled)?;
            let fd = self.fd.as_ref().expect("tty was opened");
            stage = "tty_identity";
            let actual = fs::fstat(fd)?;
            if actual.st_dev != identity.filesystem_device
                || actual.st_ino != identity.inode
                || actual.st_rdev != identity.character_device
                || FileType::from_raw_mode(actual.st_mode) != FileType::CharacterDevice
            {
                return Err(denied(
                    "device identity changed between lock selection and tty open",
                ));
            }
            stage = "tty_exclusive";
            fs::flock(fd, FlockOperation::NonBlockingLockExclusive)?;
            checkpoint(origin, cancelled)?;
            termios::ioctl_tiocexcl(fd)?;
            self.exclusive = true;
            report.tty_exclusive_applied = true;
            checkpoint(origin, cancelled)?;
            stage = "prior_termios";
            let prior = termios::tcgetattr(fd)?;
            report.prior_settings = Some(SerialSettings::read(&prior));
            // Changing B0 to a nonzero rate can deliberately raise DTR in drivers.
            // Hardware flow-control transitions and non-N_TTY disciplines
            // also require separate qualification; do not change them here.
            if prior.output_speed() == 0
                || prior.line_discipline != 0
                || prior.control_modes.contains(ControlModes::CRTSCTS)
            {
                return Err(invalid(
                    "prior B0, non-N_TTY discipline or hardware flow control is not qualified",
                ));
            }
            self.original = Some(prior.clone());
            checkpoint(origin, cancelled)?;
            let mut desired = prior;
            desired.make_raw();
            desired
                .input_modes
                .remove(InputModes::IXON | InputModes::IXOFF | InputModes::IXANY);
            desired.control_modes.remove(
                ControlModes::CSIZE
                    | ControlModes::CSTOPB
                    | ControlModes::PARENB
                    | ControlModes::PARODD
                    | ControlModes::CMSPAR
                    | ControlModes::CRTSCTS
                    | ControlModes::HUPCL,
            );
            desired
                .control_modes
                .insert(ControlModes::CS8 | ControlModes::CREAD | ControlModes::CLOCAL);
            desired.special_codes[SpecialCodeIndex::VMIN] = 1;
            desired.special_codes[SpecialCodeIndex::VTIME] = 0;
            desired.set_speed(BAUD)?;
            stage = "configure_termios";
            checkpoint(origin, cancelled)?;
            termios::tcsetattr(fd, OptionalActions::Now, &desired)?;
            checkpoint(origin, cancelled)?;
            let actual = termios::tcgetattr(fd)?;
            let actual = SerialSettings::read(&actual);
            report.negotiated_settings = Some(actual.clone());
            if actual != SerialSettings::read(&desired) {
                return Err(invalid(
                    "driver did not retain requested 1Mbaud raw 8N1 settings",
                ));
            }
            checkpoint(origin, cancelled)?;
            Ok(())
        })();
        result.map_err(|error| Diagnostic::new(stage, error))
    }

    fn finish(&mut self, origin: Instant, flush: bool) -> CleanupReport {
        let mut report = CleanupReport {
            started_us: micros(origin),
            ..CleanupReport::default()
        };
        if let Some(fd) = self.fd.take() {
            if flush {
                report.output_flush_attempted = true;
                if let Err(error) = termios::tcflush(&fd, QueueSelector::OFlush) {
                    report
                        .errors
                        .push(Diagnostic::new("discard_unsent_host_output", error.into()));
                }
            }
            if let Some(mut original) = self.original.take() {
                report.prior_settings_restore_attempted = true;
                report.hupcl_intentionally_not_restored =
                    original.control_modes.contains(ControlModes::HUPCL);
                original.control_modes.remove(ControlModes::HUPCL);
                match termios::tcsetattr(&fd, OptionalActions::Now, &original) {
                    Ok(()) => match termios::tcgetattr(&fd) {
                        Ok(actual) => {
                            let actual = SerialSettings::read(&actual);
                            if actual != SerialSettings::read(&original) {
                                report.errors.push(Diagnostic::new("restore_verify", invalid("prior serial settings did not read back, with HUPCL cleared")));
                            }
                            report.restored_settings = Some(actual);
                        }
                        Err(error) => report
                            .errors
                            .push(Diagnostic::new("restore_readback", error.into())),
                    },
                    Err(error) => report
                        .errors
                        .push(Diagnostic::new("restore_termios", error.into())),
                }
            }
            if self.exclusive {
                report.tty_exclusive_release_attempted = true;
                self.exclusive = false;
                if let Err(error) = termios::ioctl_tiocnxcl(&fd) {
                    report
                        .errors
                        .push(Diagnostic::new("release_tty_exclusive", error.into()));
                }
            }
            report.tty_close_attempted = true;
            // Linux releases the descriptor even if close reports EINTR. Never
            // retry a numeric descriptor that another thread could have reused.
            if let Err(error) = nix::unistd::close(fd.into_raw_fd()) {
                report.errors.push(Diagnostic::new(
                    "close_tty",
                    io::Error::from_raw_os_error(error as i32),
                ));
            }
        }
        if let Some(lock) = self.lock.take()
            && let Err(error) = nix::unistd::close(lock.into_raw_fd())
        {
            report.errors.push(Diagnostic::new(
                "close_lock",
                io::Error::from_raw_os_error(error as i32),
            ));
        }
        report.ended_us = micros(origin);
        report
    }
}

impl Drop for Owner {
    fn drop(&mut self) {
        // Only unwinding/early panic reaches a live descriptor here. Explicit
        // normal cleanup reports every error; Drop can only make a best effort.
        if self.fd.is_some() || self.lock.is_some() {
            let _ = self.finish(Instant::now(), true);
        }
    }
}

struct LinuxPort<'a> {
    fd: &'a OwnedFd,
}
impl PortIo for LinuxPort<'_> {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        rustix::io::read(self.fd, bytes).map_err(Into::into)
    }
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        rustix::io::write(self.fd, bytes).map_err(Into::into)
    }
    fn wait(&mut self, interest: Interest, maximum: Duration) -> io::Result<()> {
        let events = match interest {
            Interest::Read => PollFlags::IN,
            Interest::Write => PollFlags::OUT,
        };
        let mut fds = [PollFd::new(self.fd, events)];
        let timeout = Timespec {
            tv_sec: 0,
            tv_nsec: maximum
                .as_nanos()
                .try_into()
                .map_err(|_| invalid("poll interval out of range"))?,
        };
        poll(&mut fds, Some(&timeout))?;
        if fds[0]
            .revents()
            .intersects(PollFlags::ERR | PollFlags::HUP | PollFlags::NVAL)
        {
            return Err(io::Error::new(
                io::ErrorKind::BrokenPipe,
                "tty poll reported disconnect/error",
            ));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lock_directory_policy_rejects_shared_or_untrusted_ancestors() {
        for mode in [0o777, 0o1777, 0o775, 0o702] {
            assert!(trusted_metadata(mode, 0, 1000, false).is_err());
        }
        assert!(trusted_metadata(0o755, 1001, 1000, false).is_err());
        assert!(trusted_metadata(0o755, 0, 1000, false).is_ok());
        assert!(trusted_metadata(0o700, 1000, 1000, true).is_ok());
        for mode in [0o755, 0o750, 0o500] {
            assert!(trusted_metadata(mode, 1000, 1000, true).is_err());
        }
    }

    #[test]
    fn effective_capability_parse_fails_closed() {
        assert_eq!(
            effective_capabilities("Name:\ttest\nCapEff:\t0000000000200000\n").unwrap(),
            CAP_SYS_ADMIN_BIT
        );
        assert_eq!(
            effective_capabilities("CapEff:\t0000000000000000\n").unwrap(),
            0
        );
        for value in [
            "",
            "CapEff:\n",
            "CapEff: garbage",
            "CapEff: 0\nCapEff: 1",
            "CapEff: 10000000000000000",
        ] {
            assert!(effective_capabilities(value).is_err());
        }
    }

    #[test]
    fn config_requires_bounded_explicit_absolute_paths() {
        for value in ["", "ttyACM0", "/dev/../dev/ttyACM0"] {
            assert!(validate_path(Path::new(value)).is_err());
        }
        assert!(validate_path(Path::new(&format!("/{}", "a".repeat(MAX_PATH_BYTES)))).is_err());
        assert!(validate_path(Path::new("/dev/device-servo")).is_ok());
    }
}
