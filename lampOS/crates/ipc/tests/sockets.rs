use lamp_ipc::{Endpoint, MAX_DATAGRAM_BYTES, is_fresh, monotonic_ns, monotonic_us};
use std::fs::{self, DirBuilder, Permissions};
use std::io::ErrorKind;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt, symlink};
use std::os::unix::net::UnixDatagram;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

static NEXT_DIRECTORY: AtomicU64 = AtomicU64::new(0);

struct TestDirectory(PathBuf);

impl TestDirectory {
    fn new() -> Self {
        // Keep AF_UNIX paths short on macOS, where sockaddr_un is only 104 bytes.
        let path = PathBuf::from("/tmp").join(format!(
            "lamp-ipc-{}-{}-{}",
            std::process::id(),
            monotonic_ns(),
            NEXT_DIRECTORY.fetch_add(1, Ordering::Relaxed)
        ));
        DirBuilder::new().mode(0o700).create(&path).unwrap();
        Self(path.canonicalize().unwrap())
    }

    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
}

impl Drop for TestDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn raw_peer(directory: &TestDirectory) -> (UnixDatagram, Endpoint) {
    let raw_path = directory.path("raw");
    let raw = UnixDatagram::bind(&raw_path).unwrap();
    fs::set_permissions(&raw_path, Permissions::from_mode(0o600)).unwrap();
    raw.set_nonblocking(true).unwrap();
    rustix::net::sockopt::set_socket_send_buffer_size(&raw, 64 * 1024).unwrap();
    let mut receiver = Endpoint::bind(directory.path("receiver")).unwrap();
    receiver.connect(&raw_path).unwrap();
    raw.connect(directory.path("receiver")).unwrap();
    (raw, receiver)
}

#[test]
fn pair_preserves_empty_and_maximum_payloads() {
    let (sender, receiver) = Endpoint::pair().unwrap();
    let mut received = [99; MAX_DATAGRAM_BYTES];
    assert_eq!(
        receiver.try_recv(&mut received).unwrap_err().kind(),
        ErrorKind::WouldBlock
    );
    sender.try_send(&[]).unwrap();
    assert_eq!(receiver.try_recv(&mut received).unwrap(), 0);
    assert_eq!(received, [99; MAX_DATAGRAM_BYTES]);
    sender.try_send(&[42; MAX_DATAGRAM_BYTES]).unwrap();
    assert_eq!(
        receiver.try_recv(&mut received).unwrap(),
        MAX_DATAGRAM_BYTES
    );
    assert_eq!(received, [42; MAX_DATAGRAM_BYTES]);
    assert_eq!(
        receiver.try_recv(&mut received).unwrap_err().kind(),
        ErrorKind::WouldBlock
    );
}

#[test]
fn oversized_send_is_rejected_without_queueing_a_prefix() {
    let (sender, receiver) = Endpoint::pair().unwrap();
    assert_eq!(
        sender
            .try_send(&[7; MAX_DATAGRAM_BYTES + 1])
            .unwrap_err()
            .kind(),
        ErrorKind::InvalidInput
    );
    assert_eq!(
        receiver
            .try_recv(&mut [0; MAX_DATAGRAM_BYTES])
            .unwrap_err()
            .kind(),
        ErrorKind::WouldBlock
    );
}

#[test]
fn oversized_and_truncated_raw_datagrams_are_consumed_and_rejected() {
    let directory = TestDirectory::new();
    let (raw, receiver) = raw_peer(&directory);
    let mut received = [99; MAX_DATAGRAM_BYTES];
    for length in [MAX_DATAGRAM_BYTES + 1, MAX_DATAGRAM_BYTES * 2] {
        raw.send(&vec![7; length]).unwrap();
        assert_eq!(
            receiver.try_recv(&mut received).unwrap_err().kind(),
            ErrorKind::InvalidData
        );
        assert_eq!(received, [99; MAX_DATAGRAM_BYTES]);
        assert_eq!(
            receiver.try_recv(&mut received).unwrap_err().kind(),
            ErrorKind::WouldBlock
        );
    }
    raw.send(b"valid after rejection").unwrap();
    let length = receiver.try_recv(&mut received).unwrap();
    assert_eq!(&received[..length], b"valid after rejection");
}

#[test]
fn named_endpoints_are_private_and_require_one_connection() {
    let directory = TestDirectory::new();
    let left_path = directory.path("left");
    let right_path = directory.path("right");
    let mut left = Endpoint::bind(&left_path).unwrap();
    let mut right = Endpoint::bind(&right_path).unwrap();
    assert_eq!(fs::metadata(&left_path).unwrap().mode() & 0o777, 0o600);
    assert_eq!(
        left.try_send(b"unconnected").unwrap_err().kind(),
        ErrorKind::NotConnected
    );
    assert_eq!(
        left.try_recv(&mut [0; MAX_DATAGRAM_BYTES])
            .unwrap_err()
            .kind(),
        ErrorKind::NotConnected
    );
    left.connect(&right_path).unwrap();
    right.connect(&left_path).unwrap();
    assert_eq!(
        left.connect(&right_path).unwrap_err().kind(),
        ErrorKind::AlreadyExists
    );
    left.try_send(b"hello").unwrap();
    let mut received = [0; MAX_DATAGRAM_BYTES];
    let length = right.try_recv(&mut received).unwrap();
    assert_eq!(&received[..length], b"hello");
    drop(left);
    assert!(!left_path.exists());
    assert!(right_path.exists());
}

#[test]
fn failed_connection_can_be_retried() {
    let directory = TestDirectory::new();
    let mut left = Endpoint::bind(directory.path("left")).unwrap();
    let right_path = directory.path("right");
    assert_eq!(
        left.connect(&right_path).unwrap_err().kind(),
        ErrorKind::NotFound
    );
    let _right = Endpoint::bind(&right_path).unwrap();
    left.connect(&right_path).unwrap();
}

#[test]
fn connected_endpoint_never_accepts_another_sender() {
    let directory = TestDirectory::new();
    let left_path = directory.path("left");
    let right_path = directory.path("right");
    let mut left = Endpoint::bind(&left_path).unwrap();
    let mut right = Endpoint::bind(&right_path).unwrap();
    let stranger = UnixDatagram::bind(directory.path("stranger")).unwrap();
    stranger.set_nonblocking(true).unwrap();
    // An unconnected kernel socket may have already queued an unrelated packet.
    stranger.send_to(b"before connection", &right_path).unwrap();
    left.connect(&right_path).unwrap();
    right.connect(&left_path).unwrap();
    let mut received = [99; MAX_DATAGRAM_BYTES];
    let error = right.try_recv(&mut received).unwrap_err();
    assert!(matches!(
        error.kind(),
        ErrorKind::InvalidData | ErrorKind::WouldBlock
    ));
    assert_eq!(received, [99; MAX_DATAGRAM_BYTES]);
    // Linux rejects this at send; other supported kernels may let receive reject it.
    let _ = stranger.send_to(b"after connection", &right_path);
    let error = right.try_recv(&mut received).unwrap_err();
    assert!(matches!(
        error.kind(),
        ErrorKind::InvalidData | ErrorKind::WouldBlock
    ));
    left.try_send(b"authorized peer").unwrap();
    let length = right.try_recv(&mut received).unwrap();
    assert_eq!(&received[..length], b"authorized peer");
}

#[test]
fn full_data_queue_surfaces_backpressure_without_blocking_control() {
    let (data_sender, data_receiver) = Endpoint::pair().unwrap();
    let (control_sender, control_receiver) = Endpoint::pair().unwrap();
    let mut queued = 0;
    for _ in 0..1024 {
        match data_sender.try_send(&[1; MAX_DATAGRAM_BYTES]) {
            Ok(()) => queued += 1,
            Err(error) => {
                assert_eq!(error.kind(), ErrorKind::WouldBlock, "{error:?}");
                break;
            }
        }
    }
    assert!(queued > 0 && queued < 1024, "kernel queue must be bounded");
    control_sender.try_send(b"cancel").unwrap();
    let mut received = [0; MAX_DATAGRAM_BYTES];
    assert_eq!(control_receiver.try_recv(&mut received).unwrap(), 6);
    assert_eq!(&received[..6], b"cancel");
    for _ in 0..queued {
        assert_eq!(
            data_receiver.try_recv(&mut received).unwrap(),
            MAX_DATAGRAM_BYTES
        );
    }
    assert_eq!(
        data_receiver.try_recv(&mut received).unwrap_err().kind(),
        ErrorKind::WouldBlock
    );
    data_sender.try_send(b"queue recovered").unwrap();
}

#[test]
fn a_closed_peer_is_not_reported_as_backpressure() {
    let (sender, receiver) = Endpoint::pair().unwrap();
    drop(receiver);
    assert_ne!(
        sender.try_send(b"cannot deliver").unwrap_err().kind(),
        ErrorKind::WouldBlock
    );
}

#[test]
fn insecure_directory_or_peer_and_symlink_directory_are_rejected() {
    let directory = TestDirectory::new();
    fs::set_permissions(&directory.0, Permissions::from_mode(0o705)).unwrap();
    assert_eq!(
        Endpoint::bind(directory.path("bad")).unwrap_err().kind(),
        ErrorKind::PermissionDenied
    );
    fs::set_permissions(&directory.0, Permissions::from_mode(0o700)).unwrap();
    let subdirectory = directory.path("private");
    DirBuilder::new().mode(0o700).create(&subdirectory).unwrap();
    let linked = directory.path("linked");
    symlink(&subdirectory, &linked).unwrap();
    assert_eq!(
        Endpoint::bind(linked.join("bad")).unwrap_err().kind(),
        ErrorKind::PermissionDenied
    );
    let peer_path = directory.path("peer");
    let _peer = UnixDatagram::bind(&peer_path).unwrap();
    fs::set_permissions(&peer_path, Permissions::from_mode(0o666)).unwrap();
    let mut endpoint = Endpoint::bind(directory.path("endpoint")).unwrap();
    assert_eq!(
        endpoint.connect(&peer_path).unwrap_err().kind(),
        ErrorKind::PermissionDenied
    );
}

#[test]
fn existing_paths_and_replacement_files_are_preserved() {
    let directory = TestDirectory::new();
    let path = directory.path("socket");
    let endpoint = Endpoint::bind(&path).unwrap();
    assert_eq!(
        Endpoint::bind(&path).unwrap_err().kind(),
        ErrorKind::AddrInUse
    );
    fs::remove_file(&path).unwrap();
    fs::write(&path, b"replacement").unwrap();
    drop(endpoint);
    assert_eq!(fs::read(path).unwrap(), b"replacement");
}

#[test]
fn freshness_rejects_future_and_stale_values_without_unsigned_underflow() {
    assert!(is_fresh(1000, 1500, Duration::from_nanos(500)));
    assert!(!is_fresh(1000, 1501, Duration::from_nanos(500)));
    assert!(!is_fresh(1501, 1500, Duration::from_nanos(500)));
    assert!(is_fresh(0, u64::MAX, Duration::MAX));
    let before = monotonic_ns();
    let micros = monotonic_us();
    let after = monotonic_ns();
    assert!(after >= before);
    assert!(micros >= before / 1000 && micros <= after / 1000);
}

#[test]
fn child_clock_report() {
    if let Some(path) = std::env::var_os("LAMP_IPC_CLOCK_REPORT") {
        fs::write(path, monotonic_ns().to_string()).unwrap();
    }
}

#[test]
fn monotonic_origin_is_shared_with_a_fresh_child_process() {
    let directory = TestDirectory::new();
    let report = directory.path("clock");
    let before = monotonic_ns();
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "child_clock_report"])
        .env("LAMP_IPC_CLOCK_REPORT", &report)
        .stdout(Stdio::null())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("clock test child did not exit within 10 seconds");
        }
        std::thread::sleep(Duration::from_millis(5));
    };
    let after = monotonic_ns();
    assert!(status.success());
    let child_time: u64 = fs::read_to_string(report).unwrap().parse().unwrap();
    assert!(child_time >= before && child_time <= after);
}
