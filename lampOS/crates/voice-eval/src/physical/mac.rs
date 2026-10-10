//! Runner-side physical backend: drives one `lamp-session` per attempt through
//! an operator-supplied command, maps Lamp time onto the runner clock with
//! ping/pong, and starts cached stimuli on the explicit iMac speakers.
//!
//! Runner software times (start requests, receipts) only locate stimuli; they
//! are not acoustic onsets. Room audio comes from a separate recorder.
use crate::{
    Result,
    events::{ClockDomain, Cue, EventKind, EventSource, RuntimeEvent, from_trace, parse_cue},
    import::load_room_evidence,
    invalid,
    physical::{
        fake_lamp::ROOM_MAX_BYTES,
        protocol::{RunnerLine, SessionLine},
    },
    plan::{ProviderKind, Scenario, Step},
    record::{ClockMapping, StimulusSource, Stratum},
    runner::{AttemptContext, Backend, Begin, Finished, Injection},
    stimulus::{AssetIndex, SceneTiming, StimulusCatalog},
};
use lamp_ipc::monotonic_us;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixDatagram,
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc::{self, Receiver, RecvTimeoutError},
    thread::{self, JoinHandle},
    time::Duration,
};

/// Starts one stimulus. Implementations must return promptly; playback runs
/// on its own thread and its delivery report is collected at finish.
pub trait Player {
    fn start(
        &mut self,
        wav: &Path,
        timing: &SceneTiming,
        report_directory: &Path,
        cancel_file: &Path,
    ) -> Result<JoinHandle<Value>>;
    fn describe(&self) -> Value;
}

/// Cached-WAV playback through `lamp-observer` on the explicit iMac speakers.
pub struct ObserverPlayer {
    pub output_device: String,
}

impl Player for ObserverPlayer {
    fn start(
        &mut self,
        wav: &Path,
        _: &SceneTiming,
        report_directory: &Path,
        cancel_file: &Path,
    ) -> Result<JoinHandle<Value>> {
        lamp_observer::playback::validate_output_name(&self.output_device)?;
        let options = lamp_observer::playback::PlayOptions {
            output_device: self.output_device.clone(),
            wav: wav.to_path_buf(),
            output: report_directory.to_path_buf(),
            cancel_file: Some(cancel_file.to_path_buf()),
        };
        Ok(thread::spawn(move || {
            match lamp_observer::playback::play(&options) {
                Ok(report) => report,
                Err(error) => {
                    json!({"valid": false, "status": "failed", "error": error.to_string()})
                }
            }
        }))
    }
    fn describe(&self) -> Value {
        json!({"kind": "lamp-observer playback", "output_device": self.output_device,
            "boundary": "start request and CoreAudio callback times; not acoustic onset"})
    }
}

/// Direct-human prompts: at the trigger the operator console shows the line a
/// person in the room should say now. Nothing is played and no sound is made.
/// The prompt time is the only software boundary; reaction time and the actual
/// speech onset must come from room audio.
pub struct HumanPrompter {
    pub speaker_label: String,
}

impl Player for HumanPrompter {
    fn start(
        &mut self,
        _: &Path,
        timing: &SceneTiming,
        _: &Path,
        _: &Path,
    ) -> Result<JoinHandle<Value>> {
        let shown = monotonic_us();
        let lines: Vec<String> = timing.clips.iter().map(|clip| clip.text.clone()).collect();
        eprintln!(
            "\n>>> {} SAY NOW: {}\n",
            self.speaker_label,
            lines.join(" ... ")
        );
        let label = self.speaker_label.clone();
        Ok(thread::spawn(move || {
            json!({"valid": true, "status": "prompted_direct_human", "speaker_label": label,
                "speech_delivery_verified": false,
                "prompt_shown_host_monotonic_ns": shown * 1000, "lines": lines,
                "boundary": "prompt shown; human speech onset is later and unmeasured in software"})
        }))
    }
    fn describe(&self) -> Value {
        json!({"kind": "direct human prompts", "speaker_label": self.speaker_label,
            "note": "no playback; speech timing comes only from room audio"})
    }
}

/// Loopback player for the fake lamp-live: sends the stimulus timing to the
/// fake room socket instead of producing sound.
pub struct FakeRoomPlayer {
    pub room: PathBuf,
}

impl Player for FakeRoomPlayer {
    fn start(
        &mut self,
        _: &Path,
        timing: &SceneTiming,
        _: &Path,
        _: &Path,
    ) -> Result<JoinHandle<Value>> {
        let bytes = serde_json::to_vec(timing)?;
        if bytes.len() > ROOM_MAX_BYTES {
            return Err(invalid("stimulus timing exceeds the fake room datagram"));
        }
        let requested = monotonic_us();
        UnixDatagram::unbound()?.send_to(&bytes, &self.room)?;
        Ok(thread::spawn(
            move || json!({"valid": true, "status": "fake_room_delivery", "start_requested_host_monotonic_ns": requested * 1000}),
        ))
    }
    fn describe(&self) -> Value {
        json!({"kind": "fake room datagrams", "note": "loopback test; no audio"})
    }
}

#[derive(Clone, Debug)]
pub enum LampMode {
    Fixture { reply: String, sha256: String },
    Directed { provider_config: String },
}

pub struct PhysicalConfig {
    /// Argument vector that starts `lamp-voice-eval lamp-session ... --runtime
    /// LAMP_LIVE` on the Lamp, for example through the operator's SSH command.
    /// The runner appends `--work`, `--seconds` and mode arguments.
    pub lamp_command: Vec<String>,
    /// Absolute directory on the Lamp under which per-attempt work dirs are made.
    pub lamp_work_root: String,
    pub mode: LampMode,
    pub noise_suppression: String,
    pub diagnostics: bool,
    /// Optional recorder argv with `{out}` and `{seconds}` placeholders, e.g.
    /// the signed observer bundle. It must write `metadata.json` in `{out}`.
    pub room_recorder: Option<Vec<String>>,
    pub room_independent: bool,
    pub allowance_seconds: u16,
    /// Forwarded to lamp-live so ring cues are traced and checked.
    pub ring_channel_ceiling: Option<u16>,
    /// Loudspeaker replay or direct human speech; never pooled.
    pub stimulus_source: StimulusSource,
}

/// Keep refining the clock map during the session, within a bound.
const PING_INTERVAL_US: u64 = 250_000;
const MAX_PINGS: u64 = 400;

/// Scheduling identity only; this never changes Lamp conversation authority.
#[derive(Default)]
struct CueOrder {
    boot: Option<[u8; 16]>,
    sequence: u64,
    sent_us: u64,
    owners: BTreeMap<u64, CueOwner>,
    ended: bool,
}
struct CueOwner {
    generation: u64,
    endpoint: bool,
    playback: bool,
    retired: bool,
    cancelled: bool,
}
impl CueOrder {
    fn accept(&mut self, cue: &Cue, lamp_received_us: u64) -> Result<()> {
        if lamp_received_us < cue.sent_us || lamp_received_us >= cue.expires_us {
            return Err(invalid("stale/future cue at Lamp relay"));
        }
        if self.ended
            || self.sequence.checked_add(1) != Some(cue.sequence)
            || self.boot.is_some_and(|boot| boot != cue.boot)
            || cue.sent_us < self.sent_us
        {
            return Err(invalid("cue boot/sequence/time order changed"));
        }
        if let (Some(turn), Some(generation)) = (cue.turn, cue.generation) {
            if cue.kind == "input_admitted" {
                if self
                    .owners
                    .last_key_value()
                    .is_some_and(|(&old, state)| turn <= old || generation <= state.generation)
                {
                    return Err(invalid("cue admission did not advance its owner"));
                }
                self.owners.insert(
                    turn,
                    CueOwner {
                        generation,
                        endpoint: false,
                        playback: false,
                        retired: false,
                        cancelled: false,
                    },
                );
            } else {
                let owner = self
                    .owners
                    .get_mut(&turn)
                    .ok_or_else(|| invalid("cue owner has no admitted input"))?;
                if owner.generation != generation || owner.cancelled {
                    return Err(invalid("cue relabeled or revived a terminal owner"));
                }
                match cue.kind.as_str() {
                    "local_endpoint" if !owner.endpoint && !owner.playback => owner.endpoint = true,
                    "speaker_first_write"
                        if owner.endpoint && !owner.playback && !owner.retired =>
                    {
                        owner.playback = true
                    }
                    "speech_retired" if owner.playback && !owner.retired => owner.retired = true,
                    // Playback can retire before the provider finishes. A later
                    // cancellation of that same owner remains a real event.
                    "cancelled" => owner.cancelled = true,
                    _ => return Err(invalid("cue phase out of order or duplicated")),
                }
            }
        }
        self.boot = Some(cue.boot);
        self.sequence = cue.sequence;
        self.sent_us = cue.sent_us;
        self.ended = cue.kind == "run_end";
        Ok(())
    }
}

struct Session {
    child: Child,
    next_ping_us: u64,
    stdin: Option<ChildStdin>,
    lines: Receiver<std::result::Result<SessionLine, String>>,
    pings: Vec<(u64, u64)>,
    best: Option<ClockMapping>,
    trace: Vec<Value>,
    end: Option<SessionLine>,
    /// The transport closed without a relayed `session_end`.
    end_synthesized: bool,
    ready_seen: bool,
    protocol_errors: Vec<String>,
    start: Option<Value>,
    cues: Vec<Value>,
    cue_order: CueOrder,
    cue_failed: bool,
}

struct Playback {
    step: String,
    requested_us: u64,
    cancel_file: PathBuf,
    handle: JoinHandle<Value>,
}

pub struct PhysicalBackend<'a> {
    config: PhysicalConfig,
    assets: &'a AssetIndex,
    player: Box<dyn Player + 'a>,
    session: Option<Session>,
    room: Option<(Child, PathBuf, u64)>,
    playbacks: Vec<Playback>,
    attempt_directory: Option<PathBuf>,
    seconds: u16,
}

/// End scheduling immediately, retaining the actual trace at finish. This is a
/// runner-generated protocol stop, not a Lamp failure or an acoustic event.
fn cue_failure(session: &mut Session, reason: String, received_us: u64) -> RuntimeEvent {
    session.cue_failed = true;
    session.protocol_errors.push(reason.clone());
    RuntimeEvent::new(
        EventKind::RunEnd {
            status: Some("cue_protocol_invalid".into()),
            error: Some(reason),
        },
        None,
        received_us,
        ClockDomain::RunnerMonotonic,
        EventSource::Cue,
    )
}

impl<'a> PhysicalBackend<'a> {
    pub fn new(
        config: PhysicalConfig,
        assets: &'a AssetIndex,
        player: Box<dyn Player + 'a>,
    ) -> Self {
        Self {
            config,
            assets,
            player,
            session: None,
            room: None,
            playbacks: Vec::new(),
            attempt_directory: None,
            seconds: 0,
        }
    }

    fn send_ping(&mut self, seq: u64) -> Result<()> {
        let Some(session) = self.session.as_mut() else {
            return Ok(());
        };
        let Some(stdin) = session.stdin.as_mut() else {
            return Ok(());
        };
        let sent = monotonic_us();
        serde_json::to_writer(&mut *stdin, &RunnerLine::Ping { seq })?;
        stdin.write_all(b"\n")?;
        stdin.flush()?;
        session.pings.push((seq, sent));
        session.next_ping_us = sent + PING_INTERVAL_US;
        Ok(())
    }

    /// Convert one relay line into a runtime event, updating the clock map.
    fn handle(&mut self, line: SessionLine) -> Option<RuntimeEvent> {
        let received = monotonic_us();
        // Human speech has no inferred onset window. Its timing must be
        // reviewed; timed acceptance is withheld by the evaluator.
        let path_allowance = match self.config.stimulus_source {
            StimulusSource::DirectHuman => 0,
            _ => 250_000,
        };
        let session = self.session.as_mut()?;
        let mut event = match line {
            SessionLine::Pong { seq, lamp_us } => {
                if let Some(&(_, sent)) = session.pings.iter().find(|(s, _)| *s == seq) {
                    let rtt = received.saturating_sub(sent);
                    let midpoint = sent + rtt / 2;
                    let mapping = ClockMapping {
                        from: ClockDomain::RunnerMonotonic,
                        to: ClockDomain::LampMonotonic,
                        offset_us: lamp_us as i64 - midpoint as i64,
                        uncertainty_us: rtt / 2 + 1_000,
                        path_allowance_us: path_allowance,
                        method: format!(
                            "ping/pong over the session transport, best round trip {rtt} us"
                        ),
                    };
                    if session
                        .best
                        .as_ref()
                        .is_none_or(|best| mapping.uncertainty_us < best.uncertainty_us)
                    {
                        session.best = Some(mapping);
                    }
                }
                return None;
            }
            SessionLine::Cue {
                lamp_received_us,
                cue,
            } => {
                if session.cue_failed {
                    return None;
                }
                let result = (|| -> Result<Cue> {
                    if session.start.is_none() {
                        return Err(invalid("cue arrived without relay capability receipt"));
                    }
                    if session.cues.len() >= crate::physical::protocol::MAX_TRACE_LINES {
                        return Err(invalid("cue receipt limit exceeded"));
                    }
                    let parsed = parse_cue(&serde_json::to_vec(&cue)?)?;
                    // The Lamp relay validates local receipt; a clock map also
                    // rejects cues that expired while crossing the transport.
                    // The upper uncertainty bound must still be before expiry.
                    if let Some(map) = &session.best {
                        let upper_lamp_us = i128::from(received)
                            + i128::from(map.offset_us)
                            + i128::from(map.uncertainty_us);
                        if upper_lamp_us >= i128::from(parsed.expires_us) {
                            return Err(invalid(
                                "stale cue at runner (including clock uncertainty)",
                            ));
                        }
                    } else if parsed.turn.is_some() {
                        return Err(invalid("owned cue has no clock mapping to prove freshness"));
                    }
                    session.cue_order.accept(&parsed, lamp_received_us)?;
                    Ok(parsed)
                })();
                if session.cues.len() < crate::physical::protocol::MAX_TRACE_LINES {
                    session
                        .cues
                        .push(json!({"cue": cue, "lamp_received_us": lamp_received_us,
                        "runner_received_us": received, "accepted": result.is_ok(),
                        "error": result.as_ref().err().map(ToString::to_string)}));
                }
                match result {
                    Ok(cue) => cue.event()?,
                    Err(error) => cue_failure(session, error.to_string(), received),
                }
            }
            SessionLine::CueInvalid { error, .. } => {
                if session.cue_failed {
                    return None;
                }
                cue_failure(session, format!("relay rejected a cue: {error}"), received)
            }
            SessionLine::Stdout {
                lamp_received_us,
                line,
            } => {
                let status: Value = serde_json::from_str(&line).ok()?;
                if session.cue_failed
                    || session.start.is_none()
                    || status["status"] != "listening_ready"
                {
                    return None;
                }
                if std::mem::replace(&mut session.ready_seen, true) {
                    return None;
                }
                RuntimeEvent::new(
                    EventKind::ListeningReady,
                    None,
                    lamp_received_us,
                    ClockDomain::LampMonotonic,
                    EventSource::Stdout,
                )
            }
            SessionLine::Trace { record } => {
                if session.trace.len() < crate::physical::protocol::MAX_TRACE_LINES {
                    session.trace.push(record);
                }
                return None;
            }
            end @ SessionLine::SessionEnd { .. } => {
                let SessionLine::SessionEnd { lamp_us, .. } = end else {
                    unreachable!()
                };
                session.end = Some(end);
                // The authoritative relay end also closes attempts whose end cue was lost.
                RuntimeEvent::new(
                    EventKind::RunEnd {
                        status: None,
                        error: None,
                    },
                    None,
                    lamp_us,
                    ClockDomain::LampMonotonic,
                    EventSource::Stdout,
                )
            }
            start @ SessionLine::SessionStart { .. } => {
                let SessionLine::SessionStart {
                    schema,
                    ref mode,
                    cue_socket,
                    ..
                } = start
                else {
                    unreachable!()
                };
                let expected = match self.config.mode {
                    LampMode::Fixture { .. } => "fixture",
                    LampMode::Directed { .. } => "directed",
                };
                if session.start.is_some()
                    || schema != crate::physical::protocol::SCHEMA
                    || mode != expected
                    || !cue_socket
                {
                    cue_failure(
                        session,
                        "relay did not establish the requested cue capability".into(),
                        received,
                    )
                } else {
                    session.start = Some(serde_json::to_value(start).ok()?);
                    return None;
                }
            }
        };
        // Both modes report readiness on stdout and the cue socket.
        if matches!(event.kind, EventKind::ListeningReady)
            && event.source == EventSource::Cue
            && std::mem::replace(&mut session.ready_seen, true)
        {
            return None;
        }
        event.received_us = Some(received);
        Some(event)
    }

    fn wait_line(&mut self, until_us: u64) -> Option<std::result::Result<SessionLine, String>> {
        let session = self.session.as_mut()?;
        let now = monotonic_us();
        if now >= until_us {
            return session.lines.try_recv().ok();
        }
        match session
            .lines
            .recv_timeout(Duration::from_micros(until_us - now))
        {
            Ok(line) => Some(line),
            Err(RecvTimeoutError::Timeout) => None,
            Err(RecvTimeoutError::Disconnected) => {
                if session.end.is_none() {
                    session.end_synthesized = true;
                    session.end = Some(SessionLine::SessionEnd {
                        lamp_us: 0,
                        exit_code: None,
                        killed: false,
                        events_sha256: None,
                        events_lines: 0,
                        error: Some("session transport closed without session_end".into()),
                    });
                    return Some(Ok(session.end.clone()?));
                }
                None
            }
        }
    }
}

fn wait_child(child: &mut Child, deadline_us: u64) -> Option<std::process::ExitStatus> {
    loop {
        if let Ok(Some(status)) = child.try_wait() {
            return Some(status);
        }
        if monotonic_us() >= deadline_us {
            let _ = child.kill();
            let _ = child.wait();
            return None;
        }
        thread::sleep(Duration::from_millis(20));
    }
}

fn join_bounded(handle: JoinHandle<Value>, cancel_file: &Path, deadline_us: u64) -> Value {
    while !handle.is_finished() && monotonic_us() < deadline_us {
        thread::sleep(Duration::from_millis(20));
    }
    if !handle.is_finished() {
        let _ = fs::write(cancel_file, b"cancel\n");
        let grace = monotonic_us() + 2_000_000;
        while !handle.is_finished() && monotonic_us() < grace {
            thread::sleep(Duration::from_millis(20));
        }
    }
    if handle.is_finished() {
        handle
            .join()
            .unwrap_or_else(|_| json!({"valid": false, "status": "player thread panicked"}))
    } else {
        json!({"valid": false, "status": "playback did not finish within its bound; detached"})
    }
}

impl Backend for PhysicalBackend<'_> {
    fn describe(&self) -> Value {
        json!({
            "kind": "physical",
            "mode": match &self.config.mode {
                LampMode::Fixture { sha256, .. } => json!({"provider": "fixed_reply", "reply_sha256": sha256}),
                LampMode::Directed { .. } => json!({"provider": "gemini", "cues": "schema-1 socket required; software scheduling only"}),
            },
            "stimulus_source": self.config.stimulus_source,
            "noise_suppression": self.config.noise_suppression,
            "lamp_diagnostics": self.config.diagnostics,
            "player": self.player.describe(),
            "room_recorder": self.config.room_recorder.as_ref().map(|argv| argv.first().cloned()),
            "room_recorder_independent_of_lamp": self.config.room_independent,
            "assets": self.assets.scenes().map(|a| json!({"scene": a.scene, "wav_sha256": a.wav_sha256})).collect::<Vec<_>>(),
        })
    }
    fn stratum(&self, _: &Scenario) -> Stratum {
        match self.config.mode {
            LampMode::Fixture { .. } => Stratum::PhysicalFixture,
            LampMode::Directed { .. } => Stratum::PhysicalGemini,
        }
    }
    fn domain(&self) -> ClockDomain {
        ClockDomain::RunnerMonotonic
    }
    fn source(&self) -> StimulusSource {
        self.config.stimulus_source
    }
    fn unsupported(&self, scenario: &Scenario) -> Option<String> {
        if !scenario.physical {
            return Some("needs provider fault injection, which only the fake runtime has".into());
        }
        match (&self.config.mode, scenario.provider) {
            (LampMode::Fixture { .. }, ProviderKind::Gemini) => {
                return Some("Gemini scenario cannot run on the fixed-reply provider".into());
            }
            (LampMode::Directed { .. }, ProviderKind::FixedReply) => {
                return Some("fixed-reply scenario needs lamp-live directed-fixture".into());
            }
            _ => {}
        }
        scenario
            .steps
            .iter()
            .filter_map(|s| s.scene.as_deref())
            .find(|scene| self.assets.get(scene).is_none())
            .map(|scene| format!("no verified cached asset for scene {scene}; cached speech is not regenerated by the runner"))
    }
    fn begin(&mut self, context: &AttemptContext) -> Result<Begin> {
        let directory = context.run_directory.join(&context.attempt_id);
        crate::ledger::create_run_directory(&directory)?;
        self.attempt_directory = Some(directory.clone());
        self.seconds = context.scenario.session_seconds;
        self.playbacks.clear();
        let total_seconds = u64::from(self.seconds) + u64::from(self.config.allowance_seconds) + 10;
        if let Some(template) = &self.config.room_recorder {
            let out = directory.join("room");
            let argv: Vec<String> = template
                .iter()
                .map(|a| {
                    a.replace("{out}", &out.display().to_string())
                        .replace("{seconds}", &total_seconds.to_string())
                })
                .collect();
            let requested = monotonic_us();
            let child = Command::new(&argv[0])
                .args(&argv[1..])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .spawn()?;
            // Stimuli start only after the recorder reports readiness.
            let ready_deadline = monotonic_us() + 15_000_000;
            while !out.join("ready.json").exists() && monotonic_us() < ready_deadline {
                thread::sleep(Duration::from_millis(50));
            }
            if !out.join("ready.json").exists() {
                let mut child = child;
                let _ = child.kill();
                let _ = child.wait();
                return Ok(Begin::Withheld(
                    "room recorder did not report ready.json within 15 s".into(),
                ));
            }
            self.room = Some((child, out, requested));
        }
        let mut argv = self.config.lamp_command.clone();
        let work = format!(
            "{}/{}",
            self.config.lamp_work_root.trim_end_matches('/'),
            context.attempt_id
        );
        argv.extend([
            "--work".into(),
            work,
            "--seconds".into(),
            self.seconds.to_string(),
            "--noise-suppression".into(),
            self.config.noise_suppression.clone(),
            "--allowance-seconds".into(),
            self.config.allowance_seconds.to_string(),
        ]);
        match &self.config.mode {
            LampMode::Fixture { reply, sha256 } => {
                argv.extend([
                    "--fixture-reply".into(),
                    reply.clone(),
                    "--fixture-sha256".into(),
                    sha256.clone(),
                ]);
            }
            LampMode::Directed { provider_config } => {
                argv.extend(["--provider-config".into(), provider_config.clone()]);
                if self.config.diagnostics {
                    argv.push("--diagnostics".into());
                }
            }
        }
        if let Some(ceiling) = self.config.ring_channel_ceiling {
            argv.extend(["--ring-channel-ceiling".into(), ceiling.to_string()]);
        }
        let mut child = Command::new(&argv[0])
            .args(&argv[1..])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| invalid("session stdout unavailable"))?;
        let (sender, lines) = mpsc::sync_channel(4096);
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                let parsed = serde_json::from_str::<SessionLine>(&line)
                    .map_err(|e| format!("{e}: {line:.200}"));
                if sender.send(parsed).is_err() {
                    break;
                }
            }
        });
        self.session = Some(Session {
            stdin: child.stdin.take(),
            next_ping_us: 0,
            child,
            lines,
            pings: Vec::new(),
            best: None,
            trace: Vec::new(),
            end: None,
            end_synthesized: false,
            ready_seen: false,
            protocol_errors: Vec::new(),
            start: None,
            cues: Vec::new(),
            cue_order: CueOrder::default(),
            cue_failed: false,
        });
        self.send_ping(1)?;
        fs::write(
            directory.join("lamp-command.json"),
            serde_json::to_vec_pretty(&json!({"argv": argv}))?,
        )?;
        Ok(Begin::Ready)
    }
    fn now_us(&mut self) -> u64 {
        monotonic_us()
    }
    fn next_event(&mut self, until_us: u64) -> Result<Option<RuntimeEvent>> {
        loop {
            let (due, sent) = self.session.as_ref().map_or((u64::MAX, MAX_PINGS), |s| {
                (s.next_ping_us, s.pings.len() as u64)
            });
            if sent < MAX_PINGS && monotonic_us() >= due {
                // A failed write means the transport closed; session_end reports it.
                let _ = self.send_ping(sent + 1);
            }
            let wait_until = until_us.min(
                self.session
                    .as_ref()
                    .map_or(until_us, |s| s.next_ping_us.max(monotonic_us() + 1_000)),
            );
            let Some(line) = self.wait_line(wait_until) else {
                if monotonic_us() >= until_us {
                    return Ok(None);
                }
                continue;
            };
            match line {
                Ok(line) => {
                    if let Some(event) = self.handle(line) {
                        return Ok(Some(event));
                    }
                }
                Err(error) => {
                    if let Some(session) = self.session.as_mut() {
                        session.protocol_errors.push(error);
                    }
                }
            }
            if monotonic_us() >= until_us {
                return Ok(None);
            }
        }
    }
    fn runner_time(&self, event: &RuntimeEvent) -> (u64, String) {
        let mapping = self.session.as_ref().and_then(|s| s.best.as_ref());
        match (mapping, event.at_us, event.domain) {
            (Some(mapping), Some(at), ClockDomain::LampMonotonic) => (
                mapping.to_runner(at),
                format!(
                    "Lamp event time mapped to the runner clock (+/- {} us)",
                    mapping.uncertainty_us
                ),
            ),
            _ => (
                event.received_us.unwrap_or_else(monotonic_us),
                "runner receipt time".into(),
            ),
        }
    }
    fn timing(&mut self, catalog: &StimulusCatalog, scene: &str) -> Result<SceneTiming> {
        let mut timing = catalog.estimate(scene)?;
        timing.measure(&self.assets.pcm(scene)?);
        Ok(timing)
    }
    fn inject(
        &mut self,
        step: &Step,
        scene: &str,
        timing: &SceneTiming,
        at_us: u64,
    ) -> Result<Injection> {
        let asset = self
            .assets
            .get(scene)
            .ok_or_else(|| invalid("asset disappeared"))?;
        let directory = self
            .attempt_directory
            .clone()
            .ok_or_else(|| invalid("no attempt directory"))?;
        while monotonic_us() < at_us {
            thread::sleep(Duration::from_micros((at_us - monotonic_us()).min(1_000)));
        }
        let cancel_file = directory.join(format!("cancel-{}", step.id));
        let requested_us = monotonic_us();
        let handle = self.player.start(
            &asset.wav,
            timing,
            &directory.join(format!("play-{}", step.id)),
            &cancel_file,
        )?;
        self.playbacks.push(Playback {
            step: step.id.clone(),
            requested_us,
            cancel_file,
            handle,
        });
        Ok(Injection {
            started_us: requested_us,
            receipt: json!({"wav_sha256": asset.wav_sha256, "requested_runner_us": requested_us,
                "late_us": requested_us.saturating_sub(at_us),
                "boundary": "runner start request; the player's own start request and the acoustic onset are later"}),
        })
    }
    fn finish(&mut self, context: &AttemptContext) -> Result<Finished> {
        let bound = monotonic_us()
            + (u64::from(self.seconds) + u64::from(self.config.allowance_seconds) + 30) * 1_000_000;
        while self.session.as_ref().is_some_and(|s| s.end.is_none()) && monotonic_us() < bound {
            if self.next_event(bound)?.is_none() {
                break;
            }
        }
        // Keep the transport open until session_end has relayed the trace.
        while self.session.as_ref().is_some_and(|s| s.end.is_some()) {
            match self.wait_line(monotonic_us() + 300_000) {
                Some(Ok(line)) => {
                    let _ = self.handle(line);
                }
                _ => break,
            }
        }
        let Some(mut session) = self.session.take() else {
            // Session start failed: release the recorder and players, keep nothing.
            if let Some((mut child, _, _)) = self.room.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
            for playback in self.playbacks.drain(..) {
                let _ = join_bounded(playback.handle, &playback.cancel_file, monotonic_us());
            }
            return Ok(Finished {
                events: Vec::new(),
                clock: None,
                evidence: json!({"room_audio": null}),
                aborted: Some("no lamp session was running".into()),
                unmapped: 0,
                spoken_failure_notice: None,
                corrected_starts: Vec::new(),
                failed_deliveries: Vec::new(),
            });
        };
        drop(session.stdin.take());
        let exited = wait_child(&mut session.child, monotonic_us() + 5_000_000);
        // A partial trace must never be scored as a complete one.
        let aborted = match &session.end {
            _ if !session.protocol_errors.is_empty() => Some(format!(
                "invalid scheduling/transport evidence: {}",
                session.protocol_errors.join("; ")
            )),
            _ if session.start.is_none() => Some("no relay cue capability receipt".into()),
            None => Some(
                "lamp session did not report its end within the bound; transport killed".to_owned(),
            ),
            Some(_) if session.end_synthesized => {
                Some("session transport closed before session_end; the trace may be partial".into())
            }
            Some(SessionLine::SessionEnd { killed: true, .. }) => {
                Some("the relay killed the runtime at its hard deadline".into())
            }
            Some(SessionLine::SessionEnd {
                error: Some(error), ..
            }) => Some(format!("relay: {error}")),
            Some(SessionLine::SessionEnd { events_lines, .. })
                if *events_lines != session.trace.len() =>
            {
                Some(format!(
                    "received {} of {events_lines} trace lines",
                    session.trace.len()
                ))
            }
            _ => None,
        };
        let playback_bound = monotonic_us() + 25_000_000;
        let mut playbacks = Vec::new();
        let mut corrected = Vec::new();
        let mut failed = Vec::new();
        for playback in self.playbacks.drain(..) {
            let report = join_bounded(playback.handle, &playback.cancel_file, playback_bound);
            if report["valid"] != true {
                // Nothing reliable reached the room; the step tested nothing.
                failed.push((
                    playback.step.clone(),
                    format!(
                        "stimulus not delivered: {} {}",
                        report["status"], report["error"]
                    ),
                ));
            } else if let Some(ns) = report["start_requested_host_monotonic_ns"].as_u64() {
                corrected.push((playback.step.clone(), ns / 1000));
            }
            playbacks.push(json!({"step": playback.step, "runner_requested_us": playback.requested_us, "report": report}));
        }
        let room = match self.room.take() {
            Some((mut child, out, requested)) => {
                let status = wait_child(
                    &mut child,
                    monotonic_us() + (u64::from(self.seconds) + 60) * 1_000_000,
                );
                let mut evidence = load_room_evidence(
                    &out.join("metadata.json"),
                    self.config.room_independent,
                    Some(status.is_some_and(|s| s.success())),
                );
                evidence["recorder_spawn_runner_us"] = json!(requested);
                evidence["recorder_exit"] = json!(status.and_then(|s| s.code()));
                evidence
            }
            None => Value::Null,
        };
        let mut unmapped = 0;
        let mut events = Vec::new();
        for record in &session.trace {
            match from_trace(record) {
                Some(event) => events.push(event),
                None => unmapped += 1,
            }
        }
        let evidence = json!({
            "session_start": session.start,
            "session_end": session.end,
            "cue_receipts": session.cues,
            "transport_exit": exited.map(|s| s.code()),
            "protocol_errors": session.protocol_errors,
            "playbacks": playbacks,
            "room_audio": room,
            "attempt_directory": self.attempt_directory.as_ref().map(|d| d.display().to_string()),
        });
        let _ = context;
        Ok(Finished {
            events,
            clock: session.best.clone(),
            evidence,
            aborted,
            unmapped,
            spoken_failure_notice: None,
            corrected_starts: corrected,
            failed_deliveries: failed,
        })
    }
    fn reproduce(&self, context: &AttemptContext) -> String {
        format!(
            "cargo run -p lamp-voice-eval -- physical-run --scenario {} --attempt-seed {} (with the same physical options as run {})",
            context.scenario.id, context.seed, context.run_id
        )
    }
}

#[cfg(test)]
mod cue_tests {
    use super::*;

    fn cue(sequence: u64, kind: &str, turn: u64) -> Cue {
        Cue {
            schema: 1,
            sequence,
            kind: kind.into(),
            boot: [7; 16],
            turn: Some(turn),
            generation: Some(turn + 1),
            capture_epoch: Some(1),
            reference_epoch_context: Some(1),
            event_us: sequence * 10,
            sent_us: sequence * 10 + 1,
            expires_us: sequence * 10 + 100_000,
            reason: None,
        }
    }
    #[test]
    fn cue_order_rejects_missing_duplicate_relabelled_and_out_of_order_owners() {
        let mut order = CueOrder::default();
        order.accept(&cue(1, "input_admitted", 1), 12).unwrap();
        for bad in [
            cue(1, "input_admitted", 1),
            cue(3, "local_endpoint", 1),
            cue(2, "local_endpoint", 2),
            cue(2, "speech_retired", 1),
        ] {
            assert!(order.accept(&bad, 40).is_err(), "{bad:?}");
        }
        let mut bad_boot = cue(2, "local_endpoint", 1);
        bad_boot.boot = [8; 16];
        assert!(order.accept(&bad_boot, 25).is_err());
        let mut relabel = cue(2, "local_endpoint", 1);
        relabel.generation = Some(9);
        assert!(order.accept(&relabel, 25).is_err());
        order.accept(&cue(2, "local_endpoint", 1), 25).unwrap();
        order.accept(&cue(3, "speaker_first_write", 1), 35).unwrap();
        order.accept(&cue(4, "cancelled", 1), 45).unwrap();
        order.accept(&cue(5, "input_admitted", 2), 55).unwrap();
        assert!(order.accept(&cue(6, "speech_retired", 1), 65).is_err());
    }
    #[test]
    fn cue_receipt_at_expiry_or_before_send_is_not_fresh() {
        for received in [10, 100010] {
            assert!(
                CueOrder::default()
                    .accept(&cue(1, "input_admitted", 1), received)
                    .is_err()
            );
        }
    }
    #[test]
    fn retired_audio_can_be_cancelled_without_reviving_playback() {
        let mut order = CueOrder::default();
        for (index, kind) in [
            "input_admitted",
            "local_endpoint",
            "speaker_first_write",
            "speech_retired",
        ]
        .iter()
        .enumerate()
        {
            let c = cue(index as u64 + 1, kind, 1);
            order.accept(&c, c.sent_us + 1).unwrap();
        }
        assert!(order.accept(&cue(5, "speech_retired", 1), 55).is_err());
        assert!(order.accept(&cue(5, "speaker_first_write", 1), 55).is_err());
        order.accept(&cue(5, "cancelled", 1), 55).unwrap();
        assert!(order.accept(&cue(6, "cancelled", 1), 65).is_err());
        order.accept(&cue(6, "input_admitted", 2), 65).unwrap();
        assert!(order.accept(&cue(7, "speaker_first_write", 1), 75).is_err());
    }

    #[test]
    fn fresh_delayed_child_observation_keeps_its_earlier_event_time() {
        let mut order = CueOrder::default();
        order.accept(&cue(1, "input_admitted", 1), 12).unwrap();
        order.accept(&cue(2, "local_endpoint", 1), 22).unwrap();
        let mut observed = cue(3, "speaker_first_write", 1);
        // Child observation and coordinator event times are not a global
        // ordered clock: only the producer's send/sequence order is promised.
        observed.event_us = 19;
        observed.expires_us = 100019;
        order.accept(&observed, 32).unwrap();
        assert_eq!(observed.event().unwrap().at_us, Some(19));
        let mut regressed_send = cue(4, "speech_retired", 1);
        regressed_send.sent_us = 30;
        assert!(order.accept(&regressed_send, 42).is_err());
    }
}
