use lamp_ipc::{Endpoint, MAX_DATAGRAM_BYTES, monotonic_ns};
use serde_json::Value;
use std::fs::{self, DirBuilder};
use std::io::ErrorKind;
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::time::{Duration, Instant};

struct TestChild(Option<Child>);
impl TestChild {
    fn spawn(args: &[&std::ffi::OsStr]) -> Self {
        Self(Some(
            Command::new(env!("CARGO_BIN_EXE_lamp-ipc-probe"))
                .args(args)
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .unwrap(),
        ))
    }

    fn output(mut self) -> Output {
        let deadline = Instant::now() + Duration::from_secs(30);
        loop {
            if self.0.as_mut().unwrap().try_wait().unwrap().is_some() {
                return self.0.take().unwrap().wait_with_output().unwrap();
            }
            assert!(
                Instant::now() < deadline,
                "probe exceeded its functional test deadline"
            );
            std::thread::sleep(Duration::from_millis(1));
        }
    }
}
impl Drop for TestChild {
    fn drop(&mut self) {
        if let Some(child) = &mut self.0 {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

#[test]
fn control_roundtrips_survive_a_full_bulk_queue_in_another_process() {
    let output = TestChild::spawn(&["--samples".as_ref(), "32".as_ref()]).output();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["status"], "ok");
    assert_eq!(
        report["measurement"],
        "ipc_only_control_roundtrip_with_full_bulk_queue"
    );
    assert_eq!(report["requested_samples"], 32);
    assert_eq!(report["completed_samples"], 32);
    assert_eq!(report["bulk"]["full_queue_checks_during_samples"], 32);
    assert_eq!(report["bulk"]["backpressure_count"], 33);
    assert!(
        report["bulk"]["initially_enqueued_packets"]
            .as_u64()
            .unwrap()
            > 0
    );
    assert_ne!(report["parent_pid"], report["child_pid"]);
    assert_eq!(report["ready"], true);
    assert_eq!(report["child_reaped"], true);
    assert_eq!(report["child_forced"], false);
    assert_eq!(report["child_exit_code"], 0);
    assert_eq!(report["cross_process_clock_errors"], 0);
    assert_eq!(report["errors"], serde_json::json!([]));
    // Verify complete statistics without imposing a host-dependent latency gate.
    for percentile in ["first", "p50", "p95", "p99", "max"] {
        assert!(report["roundtrip_us"][percentile].is_number());
    }
}

#[test]
fn invalid_sample_counts_fail_before_spawning_a_child() {
    for samples in ["0", "10001", "-1", "unbounded"] {
        let output = TestChild::spawn(&["--samples".as_ref(), samples.as_ref()]).output();
        assert!(!output.status.success());
        let report: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(report["status"], "error");
        assert_eq!(report["completed_samples"], 0);
        assert!(report["child_pid"].is_null());
        assert!(!report["errors"].as_array().unwrap().is_empty());
    }
}

struct TestDirectory(PathBuf);
impl TestDirectory {
    fn create() -> Self {
        let path = PathBuf::from("/tmp").join(format!(
            "lamp-ipc-test-{}-{:x}",
            std::process::id(),
            monotonic_ns()
        ));
        DirBuilder::new().mode(0o700).create(&path).unwrap();
        Self(path.canonicalize().unwrap())
    }
}
impl Drop for TestDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn connect_before(endpoint: &mut Endpoint, path: &Path, deadline: Instant) {
    loop {
        match endpoint.connect(path) {
            Ok(()) => return,
            Err(error)
                if matches!(
                    error.kind(),
                    ErrorKind::NotFound | ErrorKind::PermissionDenied
                ) => {}
            Err(error) => panic!("connect failed: {error}"),
        }
        assert!(
            Instant::now() < deadline,
            "worker failed to bind its endpoint"
        );
        std::thread::sleep(Duration::from_millis(1));
    }
}

#[test]
fn worker_rejects_invalid_control_and_removes_its_socket_paths() {
    let directory = TestDirectory::create();
    let mut control = Endpoint::bind(directory.0.join("parent-control")).unwrap();
    let _bulk = Endpoint::bind(directory.0.join("parent-bulk")).unwrap();
    let child = TestChild::spawn(&["--worker".as_ref(), directory.0.as_os_str()]);
    let deadline = Instant::now() + Duration::from_secs(5);
    connect_before(&mut control, &directory.0.join("child-control"), deadline);
    let mut buffer = [0; MAX_DATAGRAM_BYTES];
    loop {
        match control.try_recv(&mut buffer) {
            Ok(size) => {
                assert_eq!(size, 5);
                assert_eq!(buffer[0], b'R');
                break;
            }
            Err(error) if error.kind() == ErrorKind::WouldBlock => {}
            Err(error) => panic!("readiness failed: {error}"),
        }
        assert!(Instant::now() < deadline, "worker failed to become ready");
        std::thread::sleep(Duration::from_millis(1));
    }
    control.try_send(b"invalid control").unwrap();
    let output = child.output();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("invalid parent control"));
    assert!(!directory.0.join("child-control").exists());
    assert!(!directory.0.join("child-bulk").exists());
}
