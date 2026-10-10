//! IPC-only qualification across separate processes. This is not an acoustic,
//! model, actuator, or end-to-end interaction latency measurement.

use lamp_ipc::{Endpoint, MAX_DATAGRAM_BYTES, monotonic_ns};
use rustix::event::{PollFd, PollFlags, Timespec, poll};
use serde_json::{Value, json};
use std::fs::{self, DirBuilder, File, OpenOptions};
use std::io::{self, ErrorKind, Read};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::Duration;

const DEFAULT_SAMPLES: usize = 1000;
const MAX_SAMPLES: usize = 10_000;
const SECOND_NS: u64 = 1_000_000_000;
const MAX_BULK_PACKETS: usize = 4096;

fn deadline_after(seconds: u64) -> u64 {
    monotonic_ns().saturating_add(seconds * SECOND_NS)
}

fn timed_out(message: &str) -> io::Error {
    io::Error::new(ErrorKind::TimedOut, message)
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

fn wait_for(endpoint: &Endpoint, interest: PollFlags, deadline: u64) -> io::Result<()> {
    loop {
        let remaining = deadline
            .checked_sub(monotonic_ns())
            .filter(|value| *value > 0)
            .ok_or_else(|| timed_out("IPC readiness deadline expired"))?;
        let timeout = Timespec {
            tv_sec: (remaining / SECOND_NS) as _,
            tv_nsec: (remaining % SECOND_NS) as _,
        };
        let mut descriptors = [PollFd::new(endpoint, interest)];
        match poll(&mut descriptors, Some(&timeout)) {
            Ok(0) => return Err(timed_out("IPC readiness deadline expired")),
            Ok(_) => {
                let events = descriptors[0].revents();
                if events.intersects(interest) {
                    return Ok(());
                }
                if events.intersects(PollFlags::ERR | PollFlags::HUP | PollFlags::NVAL) {
                    return Err(io::Error::new(
                        ErrorKind::BrokenPipe,
                        "IPC peer unavailable",
                    ));
                }
            }
            Err(rustix::io::Errno::INTR) => {}
            Err(error) => return Err(error.into()),
        }
    }
}

fn send_until(endpoint: &Endpoint, message: &[u8], deadline: u64) -> io::Result<usize> {
    let mut backpressure = 0;
    loop {
        if monotonic_ns() >= deadline {
            return Err(timed_out("control send deadline expired"));
        }
        match endpoint.try_send(message) {
            Ok(()) => return Ok(backpressure),
            Err(error) if error.kind() == ErrorKind::WouldBlock => {
                backpressure += 1;
                wait_for(endpoint, PollFlags::OUT, deadline)?;
            }
            Err(error) if error.kind() == ErrorKind::Interrupted => {}
            Err(error) => return Err(error),
        }
    }
}

fn recv_until(
    endpoint: &Endpoint,
    buffer: &mut [u8; MAX_DATAGRAM_BYTES],
    deadline: u64,
) -> io::Result<usize> {
    loop {
        if monotonic_ns() >= deadline {
            return Err(timed_out("control receive deadline expired"));
        }
        match endpoint.try_recv(buffer) {
            Ok(size) => return Ok(size),
            Err(error) if error.kind() == ErrorKind::WouldBlock => {
                wait_for(endpoint, PollFlags::IN, deadline)?
            }
            Err(error) if error.kind() == ErrorKind::Interrupted => {}
            Err(error) => return Err(error),
        }
    }
}

struct PrivateDirectory(PathBuf);
impl PrivateDirectory {
    fn create() -> io::Result<Self> {
        // Avoid long macOS TMPDIR paths exceeding sockaddr_un's 104-byte limit.
        let path = PathBuf::from("/tmp").join(format!(
            "lamp-ipc-{}-{:x}",
            std::process::id(),
            monotonic_ns()
        ));
        DirBuilder::new().mode(0o700).create(&path)?;
        let mut directory = Self(path);
        directory.0 = directory.0.canonicalize()?;
        Ok(directory)
    }
}
impl Drop for PrivateDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// Every normal return or unwinding path kills/reaps an outstanding child. The
/// child additionally has its own idle and overall deadlines if its parent dies.
struct ChildGuard(Option<Child>);
impl ChildGuard {
    fn terminate(&mut self) -> io::Result<ExitStatus> {
        let child = self.0.as_mut().expect("child is not yet reaped");
        let _ = child.kill(); // Already-exited children still need wait/reaping.
        let status = child.wait()?;
        self.0 = None;
        Ok(status)
    }

    fn finish(&mut self, deadline: u64) -> io::Result<(ExitStatus, bool)> {
        loop {
            if let Some(status) = self
                .0
                .as_mut()
                .expect("child is not yet reaped")
                .try_wait()?
            {
                self.0 = None;
                return Ok((status, false));
            }
            if monotonic_ns() >= deadline {
                return self.terminate().map(|status| (status, true));
            }
            std::thread::sleep(Duration::from_millis(1));
        }
    }
}
impl Drop for ChildGuard {
    fn drop(&mut self) {
        if self.0.is_some() {
            let _ = self.terminate();
        }
    }
}

#[derive(Default)]
struct Measurements {
    requested: usize,
    rtts: Vec<u64>,
    errors: Vec<String>,
    child_pid: Option<u32>,
    child_reaped: bool,
    child_forced: bool,
    child_exit: Option<i32>,
    ready: bool,
    bulk_packets: usize,
    bulk_backpressure: usize,
    bulk_checks: usize,
    control_backpressure: usize,
    clock_errors: usize,
}
impl Measurements {
    fn report(&self, elapsed_ns: u64) -> Value {
        let mut sorted = self.rtts.clone();
        sorted.sort_unstable();
        let percentile = |percent: usize| {
            sorted
                .get((sorted.len() * percent).div_ceil(100).saturating_sub(1))
                .map(|value| *value as f64 / 1000.0)
        };
        json!({
            "schema_version": 1,
            "measurement": "ipc_only_control_roundtrip_with_full_bulk_queue",
            "not_measured": ["acoustic_latency", "model_latency", "actuator_latency", "end_to_end_interaction"],
            "status": if self.errors.is_empty() { "ok" } else { "error" },
            "os": std::env::consts::OS, "arch": std::env::consts::ARCH,
            "build_profile": if cfg!(debug_assertions) { "debug" } else { "release" },
            "parent_pid": std::process::id(), "child_pid": self.child_pid,
            "ready": self.ready, "child_reaped": self.child_reaped,
            "child_forced": self.child_forced, "child_exit_code": self.child_exit,
            "requested_samples": self.requested, "completed_samples": self.rtts.len(),
            "warmup_samples": 0, "percentile_method": "nearest_rank",
            "clock": "CLOCK_MONOTONIC",
            "roundtrip_boundary": "parent first send attempt through receipt of matching child acknowledgment",
            "roundtrip_us": {
                "first": self.rtts.first().map(|value| *value as f64 / 1000.0),
                "p50": percentile(50), "p95": percentile(95), "p99": percentile(99),
                "max": sorted.last().map(|value| *value as f64 / 1000.0),
            },
            "bulk": { "payload_bytes": MAX_DATAGRAM_BYTES, "initially_enqueued_packets": self.bulk_packets,
                "backpressure_count": self.bulk_backpressure, "full_queue_checks_during_samples": self.bulk_checks },
            "control_send_backpressure_count": self.control_backpressure,
            "cross_process_clock_errors": self.clock_errors,
            "elapsed_ms": elapsed_ns as f64 / 1_000_000.0,
            "errors": self.errors,
        })
    }
}

fn connect_when_bound(
    endpoint: &mut Endpoint,
    path: &Path,
    deadline: u64,
    child: &mut ChildGuard,
) -> io::Result<()> {
    loop {
        // A newly bound socket may be visible just before bind finishes chmod.
        // This private startup directory belongs only to this parent/child pair.
        match endpoint.connect(path) {
            Ok(()) => return Ok(()),
            Err(error)
                if matches!(
                    error.kind(),
                    ErrorKind::NotFound
                        | ErrorKind::ConnectionRefused
                        | ErrorKind::PermissionDenied
                ) => {}
            Err(error) => return Err(error),
        }
        if child
            .0
            .as_mut()
            .expect("child is not yet reaped")
            .try_wait()?
            .is_some()
        {
            return Err(io::Error::new(
                ErrorKind::BrokenPipe,
                "child exited before binding its endpoints",
            ));
        }
        if monotonic_ns() >= deadline {
            return Err(timed_out("child endpoint readiness deadline expired"));
        }
        std::thread::sleep(Duration::from_millis(1));
    }
}

fn exercise(
    control: &mut Endpoint,
    bulk: &mut Endpoint,
    directory: &Path,
    child: &mut ChildGuard,
    measurements: &mut Measurements,
) -> io::Result<()> {
    let overall_deadline = deadline_after(60);
    let startup_deadline = deadline_after(5);
    connect_when_bound(
        control,
        &directory.join("child-control"),
        startup_deadline,
        child,
    )?;
    connect_when_bound(bulk, &directory.join("child-bulk"), startup_deadline, child)?;
    let mut buffer = [0; MAX_DATAGRAM_BYTES];
    let size = recv_until(control, &mut buffer, startup_deadline)?;
    if size != 5
        || buffer[0] != b'R'
        || u32::from_le_bytes(buffer[1..5].try_into().expect("fixed length"))
            != measurements.child_pid.expect("spawned child")
    {
        return Err(invalid("invalid child readiness response"));
    }
    measurements.ready = true;
    let bulk_payload = [0xBC; MAX_DATAGRAM_BYTES];
    let fill_deadline = deadline_after(2);
    for _ in 0..MAX_BULK_PACKETS {
        if monotonic_ns() >= fill_deadline {
            return Err(timed_out("bulk queue fill deadline expired"));
        }
        match bulk.try_send(&bulk_payload) {
            Ok(()) => measurements.bulk_packets += 1,
            Err(error) if error.kind() == ErrorKind::WouldBlock => {
                measurements.bulk_backpressure += 1;
                break;
            }
            Err(error) => return Err(error),
        }
    }
    if measurements.bulk_backpressure == 0 || measurements.bulk_packets == 0 {
        return Err(invalid("bulk queue did not reach bounded backpressure"));
    }
    for sequence in 0..measurements.requested {
        match bulk.try_send(&bulk_payload) {
            Err(error) if error.kind() == ErrorKind::WouldBlock => {
                measurements.bulk_backpressure += 1;
                measurements.bulk_checks += 1;
            }
            Ok(()) => {
                return Err(invalid(
                    "bulk queue unexpectedly accepted data during control qualification",
                ));
            }
            Err(error) => return Err(error),
        }
        let mut request = [b'P', 0, 0, 0, 0];
        request[1..].copy_from_slice(&(sequence as u32).to_le_bytes());
        let started = monotonic_ns();
        let deadline = started.saturating_add(2 * SECOND_NS).min(overall_deadline);
        measurements.control_backpressure += send_until(control, &request, deadline)?;
        let size = recv_until(control, &mut buffer, deadline)?;
        let finished = monotonic_ns();
        if size != 13 || buffer[0] != b'A' || buffer[1..5] != request[1..] {
            return Err(invalid("invalid or out-of-order child acknowledgment"));
        }
        let child_received = u64::from_le_bytes(buffer[5..13].try_into().expect("fixed length"));
        if child_received < started || child_received > finished {
            measurements.clock_errors += 1;
            return Err(invalid(
                "child monotonic timestamp falls outside the parent roundtrip",
            ));
        }
        measurements.rtts.push(finished - started);
    }
    let stop_deadline = deadline_after(2).min(overall_deadline);
    send_until(control, b"S", stop_deadline)?;
    let size = recv_until(control, &mut buffer, stop_deadline)?;
    if &buffer[..size] != b"B" {
        return Err(invalid("invalid child shutdown acknowledgment"));
    }
    Ok(())
}

fn probe(measurements: &mut Measurements) -> io::Result<()> {
    let directory = PrivateDirectory::create()?;
    let mut control = Endpoint::bind(directory.0.join("parent-control"))?;
    let mut bulk = Endpoint::bind(directory.0.join("parent-bulk"))?;
    let stderr_path = directory.0.join("child-stderr");
    let stderr = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&stderr_path)?;
    let process = Command::new(std::env::current_exe()?)
        .arg("--worker")
        .arg(&directory.0)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::from(stderr))
        .spawn()?;
    measurements.child_pid = Some(process.id());
    let mut child = ChildGuard(Some(process));
    let mut result = exercise(
        &mut control,
        &mut bulk,
        &directory.0,
        &mut child,
        measurements,
    );
    let cleanup = if result.is_err() {
        child.terminate().map(|status| (status, true))
    } else {
        child.finish(deadline_after(2))
    };
    match cleanup {
        Ok((status, forced)) => {
            measurements.child_reaped = true;
            measurements.child_forced = forced;
            measurements.child_exit = status.code();
            if result.is_ok() && (forced || !status.success()) {
                result = Err(invalid(
                    "child required termination or exited unsuccessfully",
                ));
            }
        }
        Err(error) => result = Err(io::Error::other(format!("child cleanup failed: {error}"))),
    }
    if result.is_err() {
        let mut child_error = String::new();
        if let Ok(file) = File::open(stderr_path) {
            let _ = file.take(4096).read_to_string(&mut child_error);
        }
        if !child_error.trim().is_empty() {
            measurements
                .errors
                .push(format!("child: {}", child_error.trim()));
        }
    }
    result
}

fn worker(directory: &Path) -> io::Result<()> {
    let overall_deadline = deadline_after(65);
    let mut control = Endpoint::bind(directory.join("child-control"))?;
    let mut bulk = Endpoint::bind(directory.join("child-bulk"))?;
    control.connect(directory.join("parent-control"))?;
    bulk.connect(directory.join("parent-bulk"))?;
    // Keep the bulk endpoint alive without reading it: its queue must stay full.
    let _bulk = bulk;
    let mut ready = [b'R', 0, 0, 0, 0];
    ready[1..].copy_from_slice(&std::process::id().to_le_bytes());
    send_until(&control, &ready, deadline_after(5))?;
    let mut buffer = [0; MAX_DATAGRAM_BYTES];
    let mut expected_sequence = 0_u32;
    loop {
        let deadline = deadline_after(5).min(overall_deadline);
        let size = recv_until(&control, &mut buffer, deadline)?;
        let received = monotonic_ns();
        match &buffer[..size] {
            [b'S'] => {
                send_until(&control, b"B", deadline)?;
                return Ok(());
            }
            [b'P', a, b, c, d] if expected_sequence < MAX_SAMPLES as u32 => {
                let sequence = u32::from_le_bytes([*a, *b, *c, *d]);
                if sequence != expected_sequence {
                    return Err(invalid("parent sent an out-of-order sequence"));
                }
                let mut response = [0; 13];
                response[0] = b'A';
                response[1..5].copy_from_slice(&sequence.to_le_bytes());
                response[5..13].copy_from_slice(&received.to_le_bytes());
                send_until(&control, &response, deadline)?;
                expected_sequence += 1;
            }
            _ => {
                return Err(invalid(
                    "invalid parent control message or sample limit exceeded",
                ));
            }
        }
    }
}

fn main() {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() == 2 && args[0] == "--worker" {
        if let Err(error) = worker(Path::new(&args[1])) {
            eprintln!("IPC worker: {error}");
            std::process::exit(1);
        }
        return;
    }
    if args.len() == 1 && (args[0] == "--help" || args[0] == "-h") {
        println!(
            "Usage: lamp-ipc-probe [--samples 1..=10000]\nDefault: 1000 samples. Prints IPC-only JSON; does not measure acoustic latency."
        );
        return;
    }
    let samples = if args.is_empty() {
        Some(DEFAULT_SAMPLES)
    } else if args.len() == 2 && args[0] == "--samples" {
        args[1]
            .to_str()
            .and_then(|value| value.parse::<usize>().ok())
            .filter(|value| (1..=MAX_SAMPLES).contains(value))
    } else {
        None
    };
    let started = monotonic_ns();
    let mut measurements = Measurements {
        requested: samples.unwrap_or(0),
        ..Measurements::default()
    };
    match samples {
        Some(_) => {
            if let Err(error) = probe(&mut measurements) {
                measurements.errors.push(error.to_string());
            }
        }
        None => measurements
            .errors
            .push("expected --samples with an integer from 1 through 10000".to_owned()),
    }
    println!("{}", measurements.report(monotonic_ns() - started));
    if !measurements.errors.is_empty() {
        std::process::exit(1);
    }
}
