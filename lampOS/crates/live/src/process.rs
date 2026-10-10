//! Finite child lifetimes and private IPC. A failed child ends a qualification
//! session; there is no silent restart that could reuse old conversation state.
use crate::{
    diagnostic_control::DiagnosticStarted,
    transport::WorkerChannels,
    wire::{Control, WorkerEvent},
};
use lamp_interaction::BootId;
use std::{
    fs,
    io::{self, Read},
    os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt},
    path::{Path, PathBuf},
    process::{Child, Command, ExitStatus, Stdio},
    time::{Duration, Instant},
};

pub const WORKER_STARTUP_TIMEOUT: Duration = Duration::from_secs(3);

pub fn new_boot() -> io::Result<BootId> {
    let mut bytes = [0u8; 16];
    fs::File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    BootId::new(bytes).map_err(io::Error::other)
}

pub fn boot_text(boot: BootId) -> String {
    boot.bytes().iter().map(|b| format!("{b:02x}")).collect()
}
pub fn parse_boot(input: &str) -> io::Result<BootId> {
    if input.len() != 32 || !input.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "invalid boot ID",
        ));
    }
    let mut bytes = [0; 16];
    for (index, byte) in bytes.iter_mut().enumerate() {
        *byte =
            u8::from_str_radix(&input[index * 2..index * 2 + 2], 16).map_err(io::Error::other)?;
    }
    BootId::new(bytes).map_err(io::Error::other)
}

pub struct SessionDirectory {
    pub path: PathBuf,
}
impl SessionDirectory {
    pub fn create() -> io::Result<Self> {
        let path = PathBuf::from("/tmp").join(format!("lampOS-{}", boot_text(new_boot()?)));
        fs::create_dir(&path)?;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700))?;
        Ok(Self { path })
    }
}
impl Drop for SessionDirectory {
    fn drop(&mut self) {
        // Endpoints remove only their own inodes. Never recursively erase a path
        // a different process might have populated or replaced.
        let _ = fs::remove_dir(&self.path);
    }
}

pub struct Worker {
    pub channels: WorkerChannels,
    child: Child,
    pub boot: BootId,
    pub role: String,
    pub diagnostic_start: Option<DiagnosticStarted>,
}

impl Worker {
    #[cfg(test)]
    pub(crate) fn sleeping_fixture(directory: &Path, role: &str) -> (Self, WorkerChannels) {
        let parent = new_boot().unwrap();
        let boot = new_boot().unwrap();
        let mut channels = WorkerChannels::bind(directory, role, parent, boot, false).unwrap();
        let mut peer = WorkerChannels::bind(directory, role, parent, boot, true).unwrap();
        channels.connect(directory, role, false).unwrap();
        peer.connect(directory, role, true).unwrap();
        let child = Command::new("sh")
            .args(["-c", "exec sleep 60"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        (
            Self {
                channels,
                child,
                boot,
                role: role.into(),
                diagnostic_start: None,
            },
            peer,
        )
    }

    pub fn spawn(
        executable: &Path,
        directory: &Path,
        role: &str,
        parent: BootId,
        extra: &[&str],
    ) -> io::Result<Self> {
        Self::spawn_with_tick(executable, directory, role, parent, extra, || Ok(()))
    }

    pub fn spawn_with_tick(
        executable: &Path,
        directory: &Path,
        role: &str,
        parent: BootId,
        extra: &[&str],
        mut on_wait: impl FnMut() -> io::Result<()>,
    ) -> io::Result<Self> {
        let boot = new_boot()?;
        let channels = WorkerChannels::bind(directory, role, parent, boot, false)?;
        let child = Command::new(executable)
            .arg("worker")
            .arg(role)
            .arg(directory)
            .arg(boot_text(parent))
            .arg(boot_text(boot))
            .args(extra)
            .env_clear()
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()?;
        let mut worker = Self {
            channels,
            child,
            boot,
            role: role.into(),
            diagnostic_start: None,
        };
        let deadline = Instant::now() + WORKER_STARTUP_TIMEOUT;
        let peer_c = directory.join(format!("{role}.c.w"));
        let peer_d = directory.join(format!("{role}.d.w"));
        // Startup waits run before listening readiness is advertised. A later
        // recovery must use a separate supervisor path, never block live audio.
        while !peer_sockets_ready(&[&peer_c, &peer_d])? {
            on_wait()?;
            worker.check_running()?;
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "worker socket startup timed out",
                ));
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        worker.channels.connect(directory, role, false)?;
        loop {
            on_wait()?;
            worker.check_running()?;
            match worker.channels.control.receive()? {
                Some(WorkerEvent::Ready) => return Ok(worker),
                Some(WorkerEvent::DiagnosticsStarted { started })
                    if worker.diagnostic_start.is_none()
                        && matches!(role, "capture" | "speaker") =>
                {
                    worker.diagnostic_start = Some(started);
                }
                Some(_) => return Err(io::Error::other("unexpected worker startup message")),
                None => {}
            }
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "worker readiness timed out",
                ));
            }
            std::thread::sleep(Duration::from_millis(2));
        }
    }
    pub fn check_running(&mut self) -> io::Result<()> {
        if let Some(status) = self.child.try_wait()? {
            return Err(io::Error::other(format!(
                "{} worker exited: {status}",
                self.role
            )));
        }
        Ok(())
    }
    pub(crate) fn try_exit(&mut self) -> io::Result<Option<ExitStatus>> {
        self.child.try_wait()
    }
    /// Wait at most 300 ms for cooperative stop, then kill and reap. A forced
    /// termination or abnormal exit is a failure even when cleanup succeeds.
    /// The grace/poll loop is bounded; kernel kill/wait has no userspace hard
    /// deadline. Drop remains best-effort cleanup, never evidence of success.
    pub fn shutdown(&mut self) -> io::Result<()> {
        match self.child.try_wait() {
            Ok(Some(status)) => return self.exit_result(status),
            Ok(None) => {}
            Err(error) => {
                return Err(self.terminate_and_reap(
                    io::ErrorKind::Other,
                    format!("status check before shutdown failed: {error}"),
                ));
            }
        }
        let stop_error = self.channels.control.send(Control::Stop).err();
        let deadline = Instant::now() + Duration::from_millis(300);
        loop {
            match self.child.try_wait() {
                Ok(Some(status)) => return self.exit_result(status),
                Ok(None) => {}
                Err(error) => {
                    return Err(self.terminate_and_reap(
                        io::ErrorKind::Other,
                        format!("status check during shutdown failed: {error}"),
                    ));
                }
            }
            if Instant::now() >= deadline {
                break;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        let mut reason = "did not stop within 300 ms; forced termination required".to_owned();
        if let Some(error) = stop_error {
            reason.push_str(&format!("; stop delivery failed: {error}"));
        }
        Err(self.terminate_and_reap(io::ErrorKind::TimedOut, reason))
    }

    pub(crate) fn exit_result(&self, status: ExitStatus) -> io::Result<()> {
        if status.success() {
            Ok(())
        } else {
            Err(io::Error::other(format!(
                "{} worker exited abnormally: {status}",
                self.role
            )))
        }
    }

    /// Always attempt wait, even if kill races a child that has already exited.
    /// Preserve both cleanup errors rather than returning early and losing a reap.
    pub(crate) fn terminate_and_reap(&mut self, kind: io::ErrorKind, reason: String) -> io::Error {
        let killed = self.child.kill();
        let reaped = self.child.wait();
        let mut message = format!("{} worker {reason}", self.role);
        if let Err(error) = killed {
            message.push_str(&format!("; kill failed: {error}"));
        }
        match reaped {
            Ok(status) => message.push_str(&format!("; reaped with {status}")),
            Err(error) => message.push_str(&format!("; reap failed: {error}")),
        }
        io::Error::new(kind, message)
    }
}

/// `bind` publishes a pathname before Endpoint finishes setting mode 0600.
/// Do not race that finalization or connect one half of the channel pair first.
/// This runs only inside the existing bounded pre-readiness startup loop.
fn peer_sockets_ready(paths: &[&Path]) -> io::Result<bool> {
    let mut ready = true;
    for path in paths {
        let metadata = match fs::symlink_metadata(path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                ready = false;
                continue;
            }
            Err(error) => return Err(error),
        };
        if !metadata.file_type().is_socket()
            || metadata.uid() != rustix::process::geteuid().as_raw()
        {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "worker peer must be a socket owned by this user",
            ));
        }
        // A permanently unprivate socket is never connected; startup times out.
        ready &= metadata.mode() & 0o077 == 0;
    }
    Ok(ready)
}

#[cfg(test)]
mod startup_tests {
    use super::*;
    use std::os::unix::{fs::symlink, net::UnixDatagram};

    #[test]
    fn startup_waits_for_both_sockets_to_finish_becoming_private() {
        let directory = SessionDirectory::create().unwrap();
        let control = directory.path.join("c");
        let data = directory.path.join("d");
        let paths = [control.as_path(), data.as_path()];
        assert!(!peer_sockets_ready(&paths).unwrap());
        let c = UnixDatagram::bind(&control).unwrap();
        let d = UnixDatagram::bind(&data).unwrap();
        fs::set_permissions(&control, fs::Permissions::from_mode(0o666)).unwrap();
        fs::set_permissions(&data, fs::Permissions::from_mode(0o666)).unwrap();
        assert!(!peer_sockets_ready(&paths).unwrap());
        fs::set_permissions(&control, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(!peer_sockets_ready(&paths).unwrap());
        fs::set_permissions(&data, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(peer_sockets_ready(&paths).unwrap());
        drop((c, d));
        fs::remove_file(control).unwrap();
        fs::remove_file(data).unwrap();
    }

    #[test]
    fn startup_rejects_files_and_symlinks_instead_of_calling_them_ready() {
        let directory = SessionDirectory::create().unwrap();
        let path = directory.path.join("peer");
        fs::write(&path, []).unwrap();
        assert_eq!(
            peer_sockets_ready(&[&path]).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
        fs::remove_file(&path).unwrap();
        let bound = directory.path.join("bound");
        let peer = UnixDatagram::bind(&bound).unwrap();
        fs::set_permissions(&bound, fs::Permissions::from_mode(0o600)).unwrap();
        symlink(&bound, &path).unwrap();
        assert_eq!(
            peer_sockets_ready(&[&path]).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
        drop(peer);
        fs::remove_file(path).unwrap();
        fs::remove_file(bound).unwrap();
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        // Never leave microphone/provider children behind after startup errors.
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.child.kill();
        }
        let _ = self.child.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn child_worker(directory: &SessionDirectory, script: &str) -> Worker {
        let parent = new_boot().unwrap();
        let boot = new_boot().unwrap();
        let channels = WorkerChannels::bind(&directory.path, "probe", parent, boot, false).unwrap();
        let child = Command::new("sh")
            .args(["-c", script])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        Worker {
            channels,
            child,
            boot,
            role: "probe".into(),
            diagnostic_start: None,
        }
    }

    #[test]
    fn shutdown_reports_an_already_exited_nonzero_child() {
        let directory = SessionDirectory::create().unwrap();
        let mut worker = child_worker(&directory, "exit 23");
        // Reap explicitly here to make the pre-existing-exit case deterministic.
        assert_eq!(worker.child.wait().unwrap().code(), Some(23));
        let error = worker.shutdown().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::Other);
        assert!(error.to_string().contains("exited abnormally"));
        assert_eq!(worker.child.try_wait().unwrap().unwrap().code(), Some(23));
    }

    #[test]
    fn shutdown_kills_and_reaps_a_hung_child_but_never_reports_success() {
        let directory = SessionDirectory::create().unwrap();
        // exec leaves one owned child, not a shell with an orphaned sleep process.
        let mut worker = child_worker(&directory, "exec sleep 60");
        let started = Instant::now();
        let error = worker.shutdown().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        assert!(error.to_string().contains("forced termination required"));
        assert!(error.to_string().contains("reaped with"));
        assert!(!worker.child.try_wait().unwrap().unwrap().success());
        // This is observed test behavior with slack, not a hard OS deadline claim.
        assert!(started.elapsed() < Duration::from_secs(2));
    }
}
