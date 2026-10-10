//! A real-time stand-in for `lamp-live` used to test the physical orchestration
//! path (relay, cue socket, clock mapping, triggered playback, trace import)
//! without hardware. It speaks lamp-live's CLI, emits cues through lamp-live's
//! own `CueSink`, and writes an `events.jsonl` in lamp-live's vocabulary. The
//! conversation is the fake runtime; "playback" arrives as datagrams on a fake
//! room socket instead of sound.
use crate::{
    Result,
    events::{EventKind, to_trace},
    fake::FakeRuntime,
    invalid,
    plan::LoadedPlan,
    stimulus::{SceneTiming, StimulusCatalog},
};
use lamp_interaction::BootId;
use lamp_ipc::monotonic_us;
use lamp_live::fixture_provider::CueSink;
use serde_json::{Value, json};
use std::{
    fs,
    io::Write,
    os::unix::{fs::DirBuilderExt, net::UnixDatagram},
    path::PathBuf,
    thread,
    time::Duration,
};

pub const ROOM_MAX_BYTES: usize = 2048;
const BOOT_BYTES: [u8; 16] = [7; 16];

/// `fake-lamp-live --fake-scenario ID --fake-profile ID --fake-room SOCKET
/// [--fake-seed N] [--fake-session-seconds N] directed-fixture WAV SHA SECONDS OUT ...`
pub fn run(arguments: &[String]) -> Result<i32> {
    let mut scenario = None;
    let mut profile = None;
    let mut room = None;
    let mut seed = 1;
    let mut session_override = None;
    let mut index = 0;
    while index < arguments.len() && arguments[index].starts_with("--fake-") {
        let value = arguments
            .get(index + 1)
            .cloned()
            .ok_or_else(|| invalid("fake option needs a value"))?;
        match arguments[index].as_str() {
            "--fake-scenario" => scenario = Some(value),
            "--fake-profile" => profile = Some(value),
            "--fake-room" => room = Some(PathBuf::from(value)),
            "--fake-seed" => seed = value.parse()?,
            "--fake-session-seconds" => session_override = Some(value.parse::<u16>()?),
            other => return Err(invalid(&format!("unknown fake option {other}"))),
        }
        index += 2;
    }
    let rest = &arguments[index..];
    let (seconds, output, cue_path) = match rest.first().map(String::as_str) {
        Some("directed-fixture") if rest.len() >= 5 => {
            let cue = rest
                .iter()
                .position(|a| a == "--cue-socket")
                .and_then(|i| rest.get(i + 1))
                .map(PathBuf::from);
            (rest[3].parse::<u16>()?, PathBuf::from(&rest[4]), cue)
        }
        Some("directed") if rest.len() >= 4 => {
            (rest[2].parse::<u16>()?, PathBuf::from(&rest[3]), None)
        }
        _ => {
            return Err(invalid(
                "expected lamp-live directed-fixture or directed arguments",
            ));
        }
    };
    let catalog = StimulusCatalog::load()?;
    let plan = LoadedPlan::default_plan(&catalog)?;
    let mut scenario = plan
        .scenario(&scenario.ok_or_else(|| invalid("--fake-scenario required"))?)?
        .clone();
    scenario.session_seconds = session_override.unwrap_or(seconds);
    let profile = plan
        .profile(&profile.ok_or_else(|| invalid("--fake-profile required"))?)?
        .clone();
    fs::DirBuilder::new().mode(0o700).create(&output)?;
    let room_path = room.clone();
    let room = match room {
        Some(path) => {
            let socket = UnixDatagram::bind(path)?;
            socket.set_nonblocking(true)?;
            Some(socket)
        }
        None => None,
    };
    let boot = BootId::new(BOOT_BYTES).map_err(|e| invalid(&format!("{e:?}")))?;
    let mut cues = match cue_path {
        Some(path) => {
            let mut sink = CueSink::connect(&path)?;
            sink.set_boot(boot);
            Some(sink)
        }
        None => None,
    };
    let mut runtime = FakeRuntime::new(
        profile,
        &scenario,
        plan.plan.fixed_reply.clone(),
        false,
        seed,
    );
    let base = monotonic_us();
    let mut trace: Vec<Value> = Vec::new();
    let mut buffer = [0_u8; ROOM_MAX_BYTES];
    let mut failed = false;
    let stdout = std::io::stdout();
    loop {
        // Never stamp an event in the future: CueSink rejects such cues.
        let now = monotonic_us();
        while !runtime.ended() && base + runtime.now_us() + crate::fake::TICK_US <= now {
            runtime.tick();
        }
        while let Some(event) = runtime.pop() {
            let mut event = event;
            event.at_us = event.at_us.map(|at| base + at);
            let owner = event
                .turn
                .map(|turn| json!({"boot": BOOT_BYTES, "turn": turn, "generation": turn + 1}));
            if matches!(event.kind, EventKind::ListeningReady) {
                let mut lock = stdout.lock();
                writeln!(
                    lock,
                    "{}",
                    json!({"status": "listening_ready", "seconds": seconds})
                )?;
                lock.flush()?;
            }
            if let EventKind::RunEnd {
                status: Some(status),
                ..
            } = &event.kind
            {
                failed = status == "failed";
            }
            trace.push(to_trace(&event, owner));
        }
        if let Some(sink) = cues.as_mut() {
            let _ = sink.scan(&trace, monotonic_us());
        }
        if let Some(socket) = &room {
            while let Ok(count) = socket.recv(&mut buffer) {
                let timing: SceneTiming = serde_json::from_slice(&buffer[..count])?;
                runtime.inject(timing, None, runtime.now_us());
            }
        }
        if runtime.ended() {
            break;
        }
        thread::sleep(Duration::from_millis(2));
    }
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output.join("events.jsonl"))?;
    for record in &trace {
        serde_json::to_writer(&mut file, record)?;
        file.write_all(b"\n")?;
    }
    file.sync_all()?;
    if let Some(path) = room_path {
        let _ = fs::remove_file(path);
    }
    Ok(if failed { 1 } else { 0 })
}
