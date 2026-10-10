//! Bounded, nonblocking local process transport. Payload encoding and interaction
//! ownership belong to the caller. Use separate endpoints for urgent control and
//! bulk data so an audio queue cannot delay cancellation.
//!
//! Named endpoints require a directory owned by the effective user with no group
//! or other access (normally mode 0700). Existing paths are never removed on bind.
//! Connected peers are checked again on receive, including packets queued before
//! connection. This is local process isolation, not authentication against other
//! processes running as the same user. Timestamps are valid only on this host in
//! the current boot; they must not be persisted across boots or compared remotely.
//!
//! ```
//! use lamp_ipc::{Endpoint, MAX_DATAGRAM_BYTES};
//! let (control, worker) = Endpoint::pair()?;
//! control.try_send(b"cancel")?;
//! let mut bytes = [0; MAX_DATAGRAM_BYTES];
//! let size = worker.try_recv(&mut bytes)?;
//! assert_eq!(&bytes[..size], b"cancel");
//! # Ok::<(), std::io::Error>(())
//! ```

use std::fs::{self, Permissions};
use std::io;
use std::os::fd::{AsFd, BorrowedFd};
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::os::unix::net::UnixDatagram;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// Maximum accepted application payload, including its caller-defined envelope.
pub const MAX_DATAGRAM_BYTES: usize = 8192;
/// Requested kernel buffer sizes. The OS may clamp or account these differently;
/// queue capacity is finite, and a full queue is returned as `WouldBlock`.
const SOCKET_BUFFER_BYTES: usize = 64 * 1024;

/// A system-wide monotonic clock shared by processes on Linux and macOS.
///
/// This clock measures elapsed host time, not wall time. Suspend behavior is OS
/// dependent. Pair it with a boot identifier when storing or exchanging envelopes.
#[must_use]
pub fn monotonic_ns() -> u64 {
    let now = rustix::time::clock_gettime(rustix::time::ClockId::Monotonic);
    let seconds = u64::try_from(now.tv_sec).expect("monotonic seconds are nonnegative");
    let nanos = u64::try_from(now.tv_nsec).expect("monotonic nanoseconds are nonnegative");
    seconds
        .checked_mul(1_000_000_000)
        .and_then(|value| value.checked_add(nanos))
        .expect("monotonic nanoseconds fit u64 for over 584 years")
}

/// The same shared clock as [`monotonic_ns`], truncated to microseconds.
#[must_use]
pub fn monotonic_us() -> u64 {
    monotonic_ns() / 1000
}

/// Reject future or over-age timestamps. Boot, sequence, and owner checks remain
/// the caller's responsibility. Pass a newly sampled `now_ns` at the use boundary.
#[must_use]
pub fn is_fresh(sent_ns: u64, now_ns: u64, max_age: Duration) -> bool {
    now_ns
        .checked_sub(sent_ns)
        .is_some_and(|age| u128::from(age) <= max_age.as_nanos())
}

#[derive(Debug)]
struct Binding {
    path: PathBuf,
    device: u64,
    inode: u64,
}

impl Drop for Binding {
    fn drop(&mut self) {
        // Never remove a replacement file or socket created after ours disappeared.
        if let Ok(metadata) = fs::symlink_metadata(&self.path)
            && metadata.file_type().is_socket()
            && metadata.dev() == self.device
            && metadata.ino() == self.inode
        {
            let _ = fs::remove_file(&self.path);
        }
    }
}

#[derive(Debug)]
enum Peer {
    Unconnected,
    UnnamedPair,
    Named(PathBuf),
}

/// One nonblocking connected datagram endpoint. It owns no application queue.
///
/// A named endpoint is initially unconnected and rejects send/receive until
/// [`connect`](Self::connect) succeeds. Connection may be made only once. Dropping
/// an endpoint removes its own socket pathname, but never the containing directory.
#[derive(Debug)]
pub struct Endpoint {
    socket: UnixDatagram,
    peer: Peer,
    _binding: Option<Binding>,
}

impl Endpoint {
    /// An unnamed connected pair, useful within a supervisor and in local tests.
    pub fn pair() -> io::Result<(Self, Self)> {
        let (left, right) = UnixDatagram::pair()?;
        configure(&left)?;
        configure(&right)?;
        Ok((
            Self {
                socket: left,
                peer: Peer::UnnamedPair,
                _binding: None,
            },
            Self {
                socket: right,
                peer: Peer::UnnamedPair,
                _binding: None,
            },
        ))
    }

    /// Bind a mode-0600 socket in a private directory owned by the current user.
    /// Does not replace stale socket paths. The caller manages startup recovery.
    pub fn bind(path: impl AsRef<Path>) -> io::Result<Self> {
        let path = private_path(path.as_ref())?;
        let socket = UnixDatagram::bind(&path)?;
        let metadata = fs::symlink_metadata(&path)?;
        let binding = Binding {
            path,
            device: metadata.dev(),
            inode: metadata.ino(),
        };
        fs::set_permissions(&binding.path, Permissions::from_mode(0o600))?;
        configure(&socket)?;
        Ok(Self {
            socket,
            peer: Peer::Unconnected,
            _binding: Some(binding),
        })
    }

    /// Connect to a private named peer. Failure leaves the endpoint available for
    /// a later retry. Reconnecting an established endpoint is deliberately rejected.
    pub fn connect(&mut self, peer: impl AsRef<Path>) -> io::Result<()> {
        if !matches!(self.peer, Peer::Unconnected) {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "endpoint is already connected",
            ));
        }
        let path = private_path(peer.as_ref())?;
        let metadata = fs::symlink_metadata(&path)?;
        if !metadata.file_type().is_socket()
            || metadata.uid() != rustix::process::geteuid().as_raw()
            || metadata.mode() & 0o077 != 0
        {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "peer must be a private socket owned by this user",
            ));
        }
        self.socket.connect(path)?;
        // Use the peer's bound address, not the spelling used to locate its path.
        let address = self.socket.peer_addr()?;
        let path = address.as_pathname().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "named peer has no pathname")
        })?;
        self.peer = Peer::Named(path.to_owned());
        Ok(())
    }

    /// Send one complete payload or return an error. Never split or queue payloads.
    /// `WouldBlock` means the kernel cannot accept the payload yet; retry policy is
    /// external. macOS queue-full `ENOBUFS` is normalized with its cause preserved.
    /// Success acknowledges kernel enqueueing, not peer receipt or physical action.
    pub fn try_send(&self, payload: &[u8]) -> io::Result<()> {
        self.require_connected()?;
        if payload.len() > MAX_DATAGRAM_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "datagram exceeds 8192 bytes",
            ));
        }
        let written = self.socket.send(payload).map_err(normalize_send_error)?;
        if written != payload.len() {
            return Err(io::Error::new(
                io::ErrorKind::WriteZero,
                "incomplete datagram send",
            ));
        }
        Ok(())
    }

    /// Receive one payload. `Ok(0)` is an empty datagram; no pending datagram returns
    /// `WouldBlock`. Oversized, truncated, or wrong-peer datagrams are consumed and
    /// rejected as `InvalidData`, and the caller's output buffer remains unchanged.
    pub fn try_recv(&self, output: &mut [u8; MAX_DATAGRAM_BYTES]) -> io::Result<usize> {
        self.require_connected()?;
        // The extra byte detects every payload above the limit, including one the
        // kernel truncated to this buffer. No truncated prefix is ever accepted.
        let mut incoming = [0_u8; MAX_DATAGRAM_BYTES + 1];
        let (length, address) = self.socket.recv_from(&mut incoming)?;
        let correct_peer = match &self.peer {
            Peer::Named(expected) => address.as_pathname() == Some(expected.as_path()),
            Peer::UnnamedPair => address.is_unnamed(),
            Peer::Unconnected => false,
        };
        if !correct_peer {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "datagram came from an unexpected peer",
            ));
        }
        if length > MAX_DATAGRAM_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "oversized or truncated datagram",
            ));
        }
        output[..length].copy_from_slice(&incoming[..length]);
        Ok(length)
    }

    fn require_connected(&self) -> io::Result<()> {
        if matches!(self.peer, Peer::Unconnected) {
            Err(io::Error::new(
                io::ErrorKind::NotConnected,
                "endpoint is not connected",
            ))
        } else {
            Ok(())
        }
    }
}

/// Borrow the descriptor for OS readiness polling; this does not clone ownership.
impl AsFd for Endpoint {
    fn as_fd(&self) -> BorrowedFd<'_> {
        self.socket.as_fd()
    }
}

fn normalize_send_error(error: io::Error) -> io::Error {
    // Darwin AF_UNIX datagrams return ENOBUFS for a full peer queue instead of
    // EAGAIN. Preserve the original errno as the cause; do not mask other failures.
    #[cfg(target_os = "macos")]
    if error.raw_os_error() == Some(rustix::io::Errno::NOBUFS.raw_os_error()) {
        return io::Error::new(io::ErrorKind::WouldBlock, error);
    }
    error
}

fn configure(socket: &UnixDatagram) -> io::Result<()> {
    socket.set_nonblocking(true)?;
    rustix::net::sockopt::set_socket_send_buffer_size(socket, SOCKET_BUFFER_BYTES)?;
    rustix::net::sockopt::set_socket_recv_buffer_size(socket, SOCKET_BUFFER_BYTES)?;
    Ok(())
}

fn private_path(path: &Path) -> io::Result<PathBuf> {
    let name = path
        .file_name()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "socket needs a filename"))?;
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "socket needs a private containing directory",
            )
        })?;
    let metadata = fs::symlink_metadata(parent)?;
    if !metadata.is_dir()
        || metadata.uid() != rustix::process::geteuid().as_raw()
        || metadata.mode() & 0o077 != 0
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "socket directory must be private and owned by this user",
        ));
    }
    Ok(parent.canonicalize()?.join(name))
}
