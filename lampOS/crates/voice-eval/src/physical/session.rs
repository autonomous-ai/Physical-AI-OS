//! Lamp-side relay for one finite lamp-live session.
//!
//! It creates a fresh private work directory, binds the cue socket that
//! `lamp-live directed` or `directed-fixture --cue-socket` connects to, starts the runtime,
//! and relays cues, stdout lines and ping replies as JSON lines. After the
//! runtime exits (or is killed at the hard deadline) it streams the final
//! `events.jsonl`. It never stops or starts services and never touches mixers
//! or motors; lamp-live itself refuses to run while legacy owners are active.
use crate::{
    Result,
    events::parse_cue,
    invalid,
    physical::protocol::{MAX_LINE_BYTES, MAX_TRACE_LINES, RunnerLine, SCHEMA, SessionLine},
};
use lamp_acoustic::hash;
use lamp_ipc::monotonic_us;
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{fs::DirBuilderExt, net::UnixDatagram},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::mpsc::{self, Receiver, TryRecvError},
    thread,
    time::Duration,
};

#[derive(Clone, Debug)]
pub enum SessionMode {
    Fixture { reply: PathBuf, sha256: String },
    Directed { provider_config: PathBuf },
}

#[derive(Clone, Debug)]
pub struct SessionOptions {
    pub runtime: PathBuf,
    /// Extra leading arguments (used to substitute the fake runtime in tests).
    pub runtime_prefix: Vec<String>,
    pub work: PathBuf,
    pub seconds: u16,
    pub mode: SessionMode,
    pub noise_suppression: String,
    pub diagnostics: bool,
    /// Startup before readiness plus shutdown; added to `seconds` for the
    /// hard kill deadline. lamp-live's own startup bound is 25 s.
    pub allowance_seconds: u16,
    /// Passed as `--ring-channel-ceiling`; without it no ring device opens.
    pub ring_channel_ceiling: Option<u16>,
}

impl SessionOptions {
    pub fn parse(arguments: &[String]) -> Result<Self> {
        let mut runtime = None;
        let mut runtime_prefix = Vec::new();
        let mut work = None;
        let mut seconds = None;
        let mut reply = None;
        let mut sha256 = None;
        let mut provider_config = None;
        let mut noise_suppression = "on".to_owned();
        let mut diagnostics = false;
        let mut allowance_seconds = 40;
        let mut ring_channel_ceiling = None;
        let mut iter = arguments.iter();
        while let Some(flag) = iter.next() {
            let mut value = || {
                iter.next()
                    .cloned()
                    .ok_or_else(|| invalid(&format!("{flag} needs a value")))
            };
            match flag.as_str() {
                "--runtime" => runtime = Some(PathBuf::from(value()?)),
                "--runtime-prefix-arg" => runtime_prefix.push(value()?),
                "--work" => work = Some(PathBuf::from(value()?)),
                "--seconds" => seconds = Some(value()?.parse()?),
                "--fixture-reply" => reply = Some(PathBuf::from(value()?)),
                "--fixture-sha256" => sha256 = Some(value()?),
                "--provider-config" => provider_config = Some(PathBuf::from(value()?)),
                "--noise-suppression" => noise_suppression = value()?,
                "--allowance-seconds" => allowance_seconds = value()?.parse()?,
                "--diagnostics" => diagnostics = true,
                "--ring-channel-ceiling" => ring_channel_ceiling = Some(value()?.parse()?),
                _ => return Err(invalid(&format!("unknown lamp-session option {flag}"))),
            }
        }
        let mode = match (reply, sha256, provider_config) {
            (Some(reply), Some(sha256), None) => SessionMode::Fixture { reply, sha256 },
            (None, None, Some(provider_config)) => SessionMode::Directed { provider_config },
            _ => {
                return Err(invalid(
                    "give either --fixture-reply with --fixture-sha256, or --provider-config",
                ));
            }
        };
        if !matches!(noise_suppression.as_str(), "on" | "off") {
            return Err(invalid("--noise-suppression must be on or off"));
        }
        let options = Self {
            runtime: runtime.ok_or_else(|| invalid("--runtime is required"))?,
            runtime_prefix,
            work: work.ok_or_else(|| invalid("--work is required"))?,
            seconds: seconds.ok_or_else(|| invalid("--seconds is required"))?,
            mode,
            noise_suppression,
            diagnostics,
            allowance_seconds,
            ring_channel_ceiling,
        };
        if !(1..=600).contains(&options.seconds) || !(5..=120).contains(&options.allowance_seconds)
        {
            return Err(invalid("seconds must be 1..600 and allowance 5..120"));
        }
        if !options.work.is_absolute() {
            return Err(invalid("--work must be absolute"));
        }
        Ok(options)
    }

    pub fn runtime_output(&self) -> PathBuf {
        self.work.join("runtime")
    }
    pub fn cue_path(&self) -> PathBuf {
        self.work.join("cue.sock")
    }

    pub fn argv(&self) -> Vec<String> {
        let text = |path: &PathBuf| path.display().to_string();
        let mut argv = vec![text(&self.runtime)];
        argv.extend(self.runtime_prefix.iter().cloned());
        let output = text(&self.runtime_output());
        match &self.mode {
            SessionMode::Fixture { reply, sha256 } => {
                // lamp-live requires --diagnostics for fixture experiments.
                argv.extend([
                    "directed-fixture".into(),
                    text(reply),
                    sha256.clone(),
                    self.seconds.to_string(),
                    output,
                    "--diagnostics".into(),
                    "--noise-suppression".into(),
                    self.noise_suppression.clone(),
                ]);
            }
            SessionMode::Directed { provider_config } => {
                argv.extend([
                    "directed".into(),
                    text(provider_config),
                    self.seconds.to_string(),
                    output,
                    "--noise-suppression".into(),
                    self.noise_suppression.clone(),
                ]);
                if self.diagnostics {
                    argv.push("--diagnostics".into());
                }
            }
        }
        // Both modes explicitly request the scheduling capability. An older
        // runtime must fail its CLI, never fall back to guessed delays.
        argv.extend(["--cue-socket".into(), text(&self.cue_path())]);
        if let Some(ceiling) = self.ring_channel_ceiling {
            argv.extend(["--ring-channel-ceiling".into(), ceiling.to_string()]);
        }
        argv
    }
}

fn emit(out: &mut impl Write, line: &SessionLine) -> Result<()> {
    serde_json::to_writer(&mut *out, line)?;
    out.write_all(b"\n")?;
    out.flush()?;
    Ok(())
}

fn line_reader<R: Read + Send + 'static>(source: R) -> Receiver<String> {
    let (sender, receiver) = mpsc::sync_channel(256);
    thread::spawn(move || {
        for line in BufReader::new(source).lines() {
            let Ok(mut line) = line else { break };
            line.truncate(MAX_LINE_BYTES);
            if sender.send(line).is_err() {
                break;
            }
        }
    });
    receiver
}

fn kill(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

/// Ensures an early error (for example a closed relay transport) never leaves
/// the runtime running or the cue socket behind.
struct Cleanup {
    child: Option<Child>,
    socket: Option<PathBuf>,
}
impl Drop for Cleanup {
    fn drop(&mut self) {
        if let Some(child) = self.child.as_mut()
            && matches!(child.try_wait(), Ok(None))
        {
            kill(child);
        }
        if let Some(path) = &self.socket {
            let _ = fs::remove_file(path);
        }
    }
}

/// Run one session; returns the process exit code to use.
pub fn run<R: Read + Send + 'static>(
    options: &SessionOptions,
    input: R,
    out: &mut impl Write,
) -> Result<i32> {
    fs::DirBuilder::new().mode(0o700).create(&options.work)?;
    let mut cleanup = Cleanup {
        child: None,
        socket: None,
    };
    let socket = UnixDatagram::bind(options.cue_path())?;
    cleanup.socket = Some(options.cue_path());
    socket.set_nonblocking(true)?;
    let cue = Some(socket);
    let argv = options.argv();
    emit(
        out,
        &SessionLine::SessionStart {
            schema: SCHEMA,
            lamp_us: monotonic_us(),
            mode: match options.mode {
                SessionMode::Fixture { .. } => "fixture",
                SessionMode::Directed { .. } => "directed",
            }
            .into(),
            cue_socket: cue.is_some(),
            runtime_argv: argv.clone(),
        },
    )?;
    let child = cleanup.child.insert(
        Command::new(&argv[0])
            .args(&argv[1..])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()?,
    );
    let stdout = line_reader(
        child
            .stdout
            .take()
            .ok_or_else(|| invalid("runtime stdout unavailable"))?,
    );
    let input = line_reader(input);
    let deadline = monotonic_us()
        + (u64::from(options.seconds) + u64::from(options.allowance_seconds)) * 1_000_000;
    let mut buffer = [0_u8; 1024];
    let mut killed = false;
    let mut input_open = true;
    let status = loop {
        if let Some(socket) = &cue {
            for _ in 0..64 {
                match socket.recv(&mut buffer) {
                    Ok(count) => {
                        let lamp_received_us = monotonic_us();
                        let line = match parse_cue(&buffer[..count]) {
                            Ok(_) => SessionLine::Cue {
                                lamp_received_us,
                                cue: serde_json::from_slice(&buffer[..count])?,
                            },
                            Err(error) => SessionLine::CueInvalid {
                                lamp_received_us,
                                error: error.to_string(),
                            },
                        };
                        emit(out, &line)?;
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => break,
                    Err(error) => return Err(error.into()),
                }
            }
        }
        while let Ok(line) = stdout.try_recv() {
            emit(
                out,
                &SessionLine::Stdout {
                    lamp_received_us: monotonic_us(),
                    line,
                },
            )?;
        }
        while input_open {
            match input.try_recv() {
                Ok(line) => {
                    if let Ok(RunnerLine::Ping { seq }) = serde_json::from_str(&line) {
                        emit(
                            out,
                            &SessionLine::Pong {
                                seq,
                                lamp_us: monotonic_us(),
                            },
                        )?;
                    }
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => input_open = false,
            }
        }
        if let Some(status) = child.try_wait()? {
            break Some(status);
        }
        if monotonic_us() >= deadline {
            kill(child);
            killed = true;
            break None;
        }
        thread::sleep(Duration::from_millis(2));
    };
    // Late stdout/cues after exit are still relayed, within a short bound.
    let drain_until = monotonic_us() + 200_000;
    while monotonic_us() < drain_until {
        match stdout.recv_timeout(Duration::from_millis(20)) {
            Ok(line) => emit(
                out,
                &SessionLine::Stdout {
                    lamp_received_us: monotonic_us(),
                    line,
                },
            )?,
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
        }
    }
    if let Some(socket) = &cue {
        for _ in 0..64 {
            let count = match socket.recv(&mut buffer) {
                Ok(count) => count,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(error) => return Err(error.into()),
            };
            let lamp_received_us = monotonic_us();
            let line = match parse_cue(&buffer[..count]) {
                Ok(_) => SessionLine::Cue {
                    lamp_received_us,
                    cue: serde_json::from_slice(&buffer[..count])?,
                },
                Err(error) => SessionLine::CueInvalid {
                    lamp_received_us,
                    error: error.to_string(),
                },
            };
            emit(out, &line)?;
        }
    }
    let events_path = options.runtime_output().join("events.jsonl");
    let mut events_sha256 = None;
    let mut events_lines = 0;
    let mut error = None;
    match fs::read(&events_path) {
        Ok(bytes) if bytes.len() <= 64 * 1024 * 1024 => {
            events_sha256 = Some(hash(&bytes));
            for line in String::from_utf8_lossy(&bytes)
                .lines()
                .filter(|l| !l.trim().is_empty())
            {
                if events_lines >= MAX_TRACE_LINES {
                    error = Some("trace exceeded the relay bound; truncated".into());
                    break;
                }
                match serde_json::from_str(line) {
                    Ok(record) => emit(out, &SessionLine::Trace { record })?,
                    Err(e) => error = Some(format!("malformed trace line: {e}")),
                }
                events_lines += 1;
            }
        }
        Ok(_) => error = Some("trace exceeds 64 MiB".into()),
        Err(e) => error = Some(format!("no runtime trace: {e}")),
    }
    let exit_code = status.and_then(|s| s.code());
    emit(
        out,
        &SessionLine::SessionEnd {
            lamp_us: monotonic_us(),
            exit_code,
            killed,
            events_sha256,
            events_lines,
            error,
        },
    )?;
    drop(cue);
    drop(cleanup);
    Ok(if killed { 3 } else { 0 })
}
