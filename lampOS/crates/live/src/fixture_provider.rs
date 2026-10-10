//! Opt-in, one-reply offline provider for finite acoustic experiments.
//! This supplies PCM, never interaction authority, speaker permits, or a score.
use crate::{
    options::DirectedOptions,
    provider_flow::{MAX_REPLY_SAMPLES, OutputSender, PACKET_SAMPLES},
    provider_worker::{ProviderInput, ProviderOutput},
    transport::{Channel, WorkerChannels},
    wire::{Control, WorkerEvent},
};
use lamp_interaction::{BootId, BoundaryGuard, MonoTime, Permission, Snapshot, TurnOwner};
use lamp_ipc::monotonic_us;
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::VecDeque,
    fs::{self, File},
    io::{self, Cursor, Read, Write},
    os::unix::{
        fs::{FileTypeExt, MetadataExt},
        net::UnixDatagram,
    },
    path::{Path, PathBuf},
    time::Duration,
};

const MAX_WAV_BYTES: u64 = 2 * 1024 * 1024;
pub const MAX_REPLY_FRAMES: usize = MAX_REPLY_SAMPLES;
const PACKET_FRAMES: usize = PACKET_SAMPLES;
const MAX_EVENTS: usize = 8;
const CONTROL_LEASE_US: u64 = 250_000;
const CONTROL_SLICE: usize = 16;
const RETAINED_INPUT_US: u64 = 100_000;
pub const CUE_MAX_BYTES: usize = 512;
pub const CUE_FRESH_US: u64 = 100_000;

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

#[derive(Clone, Debug, Serialize)]
pub struct FixtureInfo {
    pub wav_sha256: String,
    pub pcm_sha256: String,
    pub rate: u32,
    pub channels: u16,
    pub frames: usize,
    pub rail_samples: usize,
}

pub struct Fixture {
    pub info: FixtureInfo,
    samples: Vec<i16>,
}
impl Fixture {
    /// Validate and own the entire immutable source before activating workers.
    /// No resampling, gain adjustment, silence insertion, or TTS regeneration.
    pub fn load(path: &Path, expected_sha256: &str) -> io::Result<Self> {
        if expected_sha256.len() != 64
            || !expected_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(invalid(
                "fixture SHA256 must be 64 lowercase hexadecimal characters",
            ));
        }
        let fd = rustix::fs::open(
            path,
            rustix::fs::OFlags::RDONLY
                | rustix::fs::OFlags::CLOEXEC
                | rustix::fs::OFlags::NOFOLLOW
                | rustix::fs::OFlags::NONBLOCK,
            rustix::fs::Mode::empty(),
        )?;
        let mut file = File::from(fd);
        let before = file.metadata()?;
        if !before.is_file() || !(44..=MAX_WAV_BYTES).contains(&before.len()) {
            return Err(invalid("fixture must be a bounded regular non-symlink WAV"));
        }
        let mut bytes = Vec::with_capacity(before.len() as usize);
        (&mut file)
            .take(MAX_WAV_BYTES + 1)
            .read_to_end(&mut bytes)?;
        if bytes.len() as u64 != before.len() || file.metadata()?.len() != before.len() {
            return Err(invalid("fixture changed size during read"));
        }
        let wav_sha256 = format!("{:x}", Sha256::digest(&bytes));
        if wav_sha256 != expected_sha256 {
            return Err(invalid("fixture SHA256 mismatch"));
        }
        if &bytes[..4] != b"RIFF"
            || &bytes[8..12] != b"WAVE"
            || u32::from_le_bytes(bytes[4..8].try_into().unwrap()) as usize + 8 != bytes.len()
        {
            return Err(invalid("fixture requires a complete RIFF/WAVE container"));
        }
        let mut reader = hound::WavReader::new(Cursor::new(&bytes)).map_err(io::Error::other)?;
        let spec = reader.spec();
        let frames = reader.len() as usize;
        if spec.sample_rate != 24_000
            || spec.channels != 1
            || spec.bits_per_sample != 16
            || spec.sample_format != hound::SampleFormat::Int
            || !(240..=MAX_REPLY_FRAMES).contains(&frames)
        {
            return Err(invalid(
                "fixture requires 10 ms through 30 s of mono 24 kHz PCM16",
            ));
        }
        let samples: Vec<i16> = reader
            .samples()
            .collect::<Result<_, _>>()
            .map_err(io::Error::other)?;
        if samples.len() != frames || samples.iter().all(|&sample| sample == 0) {
            return Err(invalid("fixture PCM is incomplete or entirely silent"));
        }
        let mut pcm_hash = Sha256::new();
        for sample in &samples {
            pcm_hash.update(sample.to_le_bytes());
        }
        let info = FixtureInfo {
            wav_sha256,
            pcm_sha256: format!("{:x}", pcm_hash.finalize()),
            rate: 24_000,
            channels: 1,
            frames,
            rail_samples: samples
                .iter()
                .filter(|&&s| matches!(s, i16::MIN | i16::MAX))
                .count(),
        };
        Ok(Self { info, samples })
    }
}

#[derive(Debug)]
pub struct FixtureOptions {
    pub directed: DirectedOptions,
    pub cue_socket: Option<PathBuf>,
}
impl FixtureOptions {
    pub fn parse(arguments: &[String]) -> io::Result<Self> {
        let mut remaining = Vec::new();
        let mut cue_socket = None;
        let mut arguments = arguments.iter();
        while let Some(value) = arguments.next() {
            if value == "--cue-socket" {
                if cue_socket.is_some() {
                    return Err(invalid("duplicate cue socket"));
                }
                cue_socket = Some(PathBuf::from(
                    arguments
                        .next()
                        .ok_or_else(|| invalid("missing cue socket"))?,
                ));
            } else {
                remaining.push(value.clone());
            }
        }
        let directed = DirectedOptions::parse(&remaining)?;
        if !directed.diagnostics {
            return Err(invalid(
                "fixture experiments require explicit --diagnostics",
            ));
        }
        Ok(Self {
            directed,
            cue_socket,
        })
    }
}

struct Input {
    owner: TurnOwner,
    open: bool,
    sequence: u64,
    read_at_us: Option<u64>,
    frames: usize,
}
struct Reply {
    owner: TurnOwner,
    offset: usize,
    stage: u8,
    audio: bool,
}

struct RetainedInput {
    command: ProviderInput,
    received_at_us: u64,
}
#[derive(Clone, Copy, Eq, PartialEq)]
enum ControlDrain {
    Empty,
    Deferred,
    Stopped,
}

/// Bounded protocol state. The source is reserved for the first admitted
/// owner, including when cancellation overtakes its queued Start/End messages.
/// Later inputs are observed but never trigger another fixture playback.
pub struct FixtureProvider {
    fixture: Fixture,
    guard: BoundaryGuard,
    first_owner: Option<TurnOwner>,
    input: Option<Input>,
    reply: Option<Reply>,
    events: VecDeque<ProviderOutput>,
    pending: Option<ProviderOutput>,
    retained_input: Option<RetainedInput>,
    highest_seen: u64,
    retired_through: u64,
    output_sequence: u64,
    output_flow: OutputSender,
    last_control_us: u64,
    last_now_us: u64,
    saw_allowed: bool,
    stopped: bool,
}
impl FixtureProvider {
    pub fn new(fixture: Fixture, boot: BootId, now_us: u64) -> Self {
        Self {
            fixture,
            guard: BoundaryGuard::new(boot, MonoTime::from_micros(now_us)),
            first_owner: None,
            input: None,
            reply: None,
            events: VecDeque::with_capacity(MAX_EVENTS),
            pending: Some(ProviderOutput::Ready { setup_us: 0 }),
            retained_input: None,
            highest_seen: 0,
            retired_through: 0,
            output_sequence: 0,
            output_flow: OutputSender::default(),
            last_control_us: now_us,
            last_now_us: now_us,
            saw_allowed: false,
            stopped: false,
        }
    }
    pub fn stop(&mut self) {
        self.stopped = true;
        self.guard.invalidate();
        self.input = None;
        self.reply = None;
        self.pending = None;
        self.retained_input = None;
        self.events.clear();
    }
    fn checked<T>(&mut self, result: io::Result<T>) -> io::Result<T> {
        if result.is_err() {
            self.stop();
        }
        result
    }
    /// False means privacy ended this finite worker; reopening cannot revive it.
    pub fn install(&mut self, now_us: u64, state: Snapshot) -> io::Result<bool> {
        let result = self.install_inner(now_us, state);
        self.checked(result)
    }
    fn install_inner(&mut self, now_us: u64, state: Snapshot) -> io::Result<bool> {
        // A new packet cannot revive a worker that missed its previous lease.
        self.fresh(now_us)?;
        let changed = self
            .guard
            .state()
            .is_some_and(|old| old.microphone_generation() != state.microphone_generation());
        self.guard
            .install(MonoTime::from_micros(now_us), state)
            .map_err(io::Error::other)?;
        self.last_control_us = now_us;
        if self.saw_allowed && (changed || state.microphone_permission() != Permission::Allowed) {
            self.stop();
            return Ok(false);
        }
        self.saw_allowed |= state.microphone_permission() == Permission::Allowed;
        if let Some(owner) = state.owner() {
            if owner.turn() <= self.retired_through || owner.turn() < self.highest_seen {
                return Err(invalid("fixture authority resurrected a retired owner"));
            }
            self.first_owner.get_or_insert(owner);
            self.highest_seen = owner.turn();
            self.retired_through = self.retired_through.max(owner.turn() - 1);
        } else {
            self.retired_through = self.retired_through.max(self.highest_seen);
        }
        if self
            .input
            .as_ref()
            .is_some_and(|input| Some(input.owner) != state.owner())
        {
            self.input = None;
        }
        if self
            .reply
            .as_ref()
            .is_some_and(|reply| Some(reply.owner) != state.owner())
        {
            self.reply = None;
        }
        if self
            .pending
            .as_ref()
            .and_then(output_request)
            .is_some_and(|r| r <= self.retired_through)
        {
            self.pending = None;
        }
        self.events
            .retain(|event| output_request(event).is_none_or(|r| r > self.retired_through));
        Ok(true)
    }
    fn clock(&mut self, now_us: u64) -> io::Result<()> {
        if self.stopped || now_us < self.last_now_us {
            return Err(invalid("fixture stopped or monotonic clock regressed"));
        }
        self.last_now_us = now_us;
        Ok(())
    }
    fn fresh(&mut self, now_us: u64) -> io::Result<()> {
        self.clock(now_us)?;
        if now_us.saturating_sub(self.last_control_us) >= CONTROL_LEASE_US {
            return Err(invalid("fixture controller heartbeat expired"));
        }
        if let Some(state) = self.guard.state()
            && (now_us >= state.expires_at().as_micros()
                || (state.owner().is_some()
                    && !state.listening_ready(MonoTime::from_micros(now_us))))
        {
            return Err(invalid("fixture privacy or input lease expired"));
        }
        Ok(())
    }
    fn event(&mut self, event: ProviderOutput) -> io::Result<()> {
        if self.events.len() == MAX_EVENTS {
            return Err(invalid("fixture event queue full"));
        }
        self.events.push_back(event);
        Ok(())
    }
    pub fn submit(&mut self, now_us: u64, command: ProviderInput) -> io::Result<()> {
        let result = self.submit_inner(now_us, command);
        self.checked(result)
    }
    fn submit_inner(&mut self, now_us: u64, command: ProviderInput) -> io::Result<()> {
        self.fresh(now_us)?;
        let state = self
            .guard
            .state()
            .ok_or_else(|| invalid("fixture has no authority"))?;
        let (request, privacy) = match &command {
            ProviderInput::Start {
                request,
                privacy_generation,
            }
            | ProviderInput::Audio {
                request,
                privacy_generation,
                ..
            }
            | ProviderInput::End {
                request,
                privacy_generation,
            } => (*request, *privacy_generation),
        };
        if request == 0
            || !state.listening_ready(MonoTime::from_micros(now_us))
            || privacy != state.microphone_generation()
        {
            return Err(invalid("fixture input lost privacy or retention authority"));
        }
        if request <= self.retired_through {
            return Ok(());
        }
        let owner = state
            .owner()
            .filter(|owner| owner.turn() == request)
            .ok_or_else(|| invalid("fixture input has no matching admitted owner"))?;
        match command {
            ProviderInput::Start { .. } => {
                if self.input.is_some() {
                    return Err(invalid("fixture input already started"));
                }
                self.input = Some(Input {
                    owner,
                    open: true,
                    sequence: 0,
                    read_at_us: None,
                    frames: 0,
                });
                self.event(ProviderOutput::Started {
                    request,
                    waiting_for_barrier: false,
                })?;
            }
            ProviderInput::Audio {
                sequence,
                read_completed_at_us,
                samples,
                ..
            } => {
                let input = self
                    .input
                    .as_mut()
                    .filter(|input| input.owner == owner && input.open)
                    .ok_or_else(|| invalid("fixture audio without open input"))?;
                if samples.len() != lamp_audio::CAPTURE_SAMPLES
                    || read_completed_at_us == 0
                    || input.sequence.checked_add(1) != Some(sequence)
                    || now_us
                        .checked_sub(read_completed_at_us)
                        .is_none_or(|age| age >= 1_000_000)
                    || input
                        .read_at_us
                        .is_some_and(|previous| read_completed_at_us < previous)
                    || input.frames >= 120 * 16_000
                {
                    return Err(invalid(
                        "fixture audio sequence, time, shape or duration invalid",
                    ));
                }
                input.sequence = sequence;
                input.read_at_us = Some(read_completed_at_us);
                input.frames += samples.len();
                // The fixture needs protocol continuity, not retained microphone PCM.
            }
            ProviderInput::End { .. } => {
                let input = self
                    .input
                    .as_mut()
                    .filter(|input| input.owner == owner && input.open && input.frames > 0)
                    .ok_or_else(|| invalid("fixture endpoint without captured open input"))?;
                input.open = false;
                self.reply = Some(Reply {
                    owner,
                    offset: 0,
                    stage: 0,
                    audio: self.first_owner == Some(owner),
                });
            }
        }
        Ok(())
    }
    /// One bounded worker tick. Control is checked again after data receipt:
    /// Authority-before-Start on separate sockets is not receive-ordering.
    /// False means Stop/privacy ended the worker. A full control slice defers
    /// input and output, retaining at most one command with its original age.
    pub fn service_step(
        &mut self,
        mut now: impl FnMut() -> u64,
        mut receive_control: impl FnMut() -> io::Result<Option<Control>>,
        data: &mut Channel,
    ) -> io::Result<bool> {
        let result = self.service_inner(&mut now, &mut receive_control, data);
        self.checked(result)
    }
    fn service_inner(
        &mut self,
        now: &mut impl FnMut() -> u64,
        receive_control: &mut impl FnMut() -> io::Result<Option<Control>>,
        data: &mut Channel,
    ) -> io::Result<bool> {
        let at = now();
        self.fresh(at)?;
        self.check_retained_input(at)?;
        match self.drain_control(now, receive_control)? {
            ControlDrain::Stopped => return Ok(false),
            ControlDrain::Deferred => return Ok(true),
            ControlDrain::Empty => {}
        }
        if self.retained_input.is_none()
            && let Some(command) = data.receive()?
        {
            self.retained_input = Some(RetainedInput {
                command,
                received_at_us: now(),
            });
        }
        // The parent sends authority before its dependent input. It can do
        // both after our first empty read, so the data read must precede this
        // second priority drain. Never consume another input while deferred.
        match self.drain_control(now, receive_control)? {
            ControlDrain::Stopped => return Ok(false),
            ControlDrain::Deferred => return Ok(true),
            ControlDrain::Empty => {}
        }
        let at = now();
        self.check_retained_input(at)?;
        if let Some(retained) = self.retained_input.take() {
            // No speculative wait for missing owner authority: once control
            // is empty, the existing admission/privacy checks remain final.
            self.submit(at, retained.command)?;
        }
        if self.output_flow.can_send() && self.send_one(now(), |event| data.send(event))? {
            self.output_flow.record_sent()?;
        }
        Ok(true)
    }
    fn check_retained_input(&self, now_us: u64) -> io::Result<()> {
        if self.retained_input.as_ref().is_some_and(|input| {
            now_us
                .checked_sub(input.received_at_us)
                .is_none_or(|age| age >= RETAINED_INPUT_US)
        }) {
            return Err(invalid("fixture retained input expired or clock regressed"));
        }
        Ok(())
    }
    fn drain_control(
        &mut self,
        now: &mut impl FnMut() -> u64,
        receive: &mut impl FnMut() -> io::Result<Option<Control>>,
    ) -> io::Result<ControlDrain> {
        for _ in 0..CONTROL_SLICE {
            match receive()? {
                Some(Control::Stop) => {
                    self.stop();
                    return Ok(ControlDrain::Stopped);
                }
                Some(Control::Authority { snapshot }) => {
                    if !self.install(now(), snapshot)? {
                        return Ok(ControlDrain::Stopped);
                    }
                }
                Some(Control::ProviderOutputCapacity { through }) => {
                    self.output_flow.grant(through)?;
                }
                Some(Control::StartCapture | Control::ConnectReference { .. }) => {
                    return Err(invalid("invalid fixture provider control"));
                }
                None => return Ok(ControlDrain::Empty),
            }
        }
        Ok(ControlDrain::Deferred)
    }
    /// At most one bounded datagram per call. WouldBlock retains the original
    /// pending packet; priority authority installation can discard it first.
    pub fn send_one(
        &mut self,
        now_us: u64,
        mut send: impl FnMut(&ProviderOutput) -> io::Result<()>,
    ) -> io::Result<bool> {
        let result = self.send_inner(now_us, &mut send);
        self.checked(result)
    }
    fn send_inner(
        &mut self,
        now_us: u64,
        send: &mut impl FnMut(&ProviderOutput) -> io::Result<()>,
    ) -> io::Result<bool> {
        self.fresh(now_us)?;
        if self.pending.is_none() {
            self.pending = self.events.pop_front();
        }
        if self.pending.is_none()
            && let Some(reply) = self.reply.as_mut()
        {
            let request = reply.owner.turn();
            self.pending = Some(
                if reply.audio && reply.offset < self.fixture.samples.len() {
                    let end = (reply.offset + PACKET_FRAMES).min(self.fixture.samples.len());
                    let samples = self.fixture.samples[reply.offset..end].to_vec();
                    reply.offset = end;
                    self.output_sequence = self
                        .output_sequence
                        .checked_add(1)
                        .ok_or_else(|| invalid("fixture output sequence exhausted"))?;
                    // Each fixed-size event is its own bounded provider packet.
                    ProviderOutput::Audio {
                        request,
                        sequence: self.output_sequence,
                        provider_event_at_us: now_us,
                        source_frames: samples.len(),
                        source_offset: 0,
                        samples,
                    }
                } else if reply.stage == 0 {
                    reply.stage = 1;
                    ProviderOutput::GenerationComplete { request }
                } else {
                    self.reply = None;
                    ProviderOutput::TurnComplete {
                        request,
                        idle: true,
                    }
                },
            );
        }
        let Some(event) = self.pending.as_ref() else {
            return Ok(false);
        };
        if let Some(request) = output_request(event) {
            let state = self
                .guard
                .state()
                .ok_or_else(|| invalid("fixture output has no authority"))?;
            if !state.listening_ready(MonoTime::from_micros(now_us))
                || state.owner().map(|owner| owner.turn()) != Some(request)
            {
                return Err(invalid("fixture output lost original owner"));
            }
        }
        match send(event) {
            Ok(()) => {
                self.pending = None;
                Ok(true)
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => Ok(false),
            Err(error) => Err(error),
        }
    }
}
fn output_request(event: &ProviderOutput) -> Option<u64> {
    match event {
        ProviderOutput::Ready { .. } => None,
        ProviderOutput::Started { request, .. }
        | ProviderOutput::Audio { request, .. }
        | ProviderOutput::GenerationComplete { request }
        | ProviderOutput::Interrupted { request }
        | ProviderOutput::TurnComplete { request, .. } => Some(*request),
        ProviderOutput::Transcript { request, .. } => *request,
    }
}

/// Separate worker, still using the ordinary provider IPC protocol. Control is
/// drained before every input/output step; PCM never bypasses the speaker worker.
pub fn run(mut channels: WorkerChannels, boot: BootId, wav: &Path, sha256: &str) -> io::Result<()> {
    let started_us = monotonic_us();
    let fixture = Fixture::load(wav, sha256)?;
    let loaded_us = monotonic_us();
    let mut provider = FixtureProvider::new(fixture, boot, loaded_us);
    provider.pending = Some(ProviderOutput::Ready {
        setup_us: loaded_us - started_us,
    });
    channels.control.send(WorkerEvent::Ready)?;
    loop {
        if !provider.service_step(
            monotonic_us,
            || channels.control.receive(),
            &mut channels.data,
        )? {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(2));
    }
}

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CueKind {
    ListeningReady,
    SpeakerFirstWrite,
    Cancelled,
    SpeechRetired,
    RunEnd,
}
#[derive(Serialize)]
struct Cue {
    schema: u8,
    sequence: u64,
    kind: CueKind,
    boot: BootId,
    turn: Option<u64>,
    generation: Option<u64>,
    capture_epoch: Option<u64>,
    reference_epoch_context: Option<u64>,
    event_us: u64,
    sent_us: u64,
    expires_us: u64,
}
struct FixedBytes {
    bytes: [u8; CUE_MAX_BYTES],
    len: usize,
}
impl Write for FixedBytes {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > CUE_MAX_BYTES - self.len {
            return Err(invalid("cue exceeds fixed buffer"));
        }
        self.bytes[self.len..self.len + bytes.len()].copy_from_slice(bytes);
        self.len += bytes.len();
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// Optional scheduling-only channel. The connected socket pins the peer endpoint;
/// no filesystem operation, heap output buffer or blocking write occurs in emit.
pub struct CueSink {
    socket: UnixDatagram,
    boot: Option<BootId>,
    sequence: u64,
    capture_epoch: Option<u64>,
    reference_epoch: Option<u64>,
    trace_offset: usize,
    fault: Option<&'static str>,
}
impl CueSink {
    pub fn connect(path: &Path) -> io::Result<Self> {
        if !path.is_absolute() {
            return Err(invalid("cue socket must be absolute"));
        }
        let parent = path
            .parent()
            .ok_or_else(|| invalid("cue socket needs a private parent"))?;
        let before_dir = fs::symlink_metadata(parent)?;
        let before = fs::symlink_metadata(path)?;
        let uid = rustix::process::geteuid().as_raw();
        if !before_dir.is_dir()
            || before_dir.mode() & 0o077 != 0
            || before_dir.uid() != uid
            || !before.file_type().is_socket()
            || before.uid() != uid
        {
            return Err(invalid(
                "cue requires a same-user socket in a private non-symlink directory",
            ));
        }
        let socket = UnixDatagram::unbound()?;
        socket.set_nonblocking(true)?;
        socket.connect(path)?;
        let after = fs::symlink_metadata(path)?;
        let after_dir = fs::symlink_metadata(parent)?;
        if (before.dev(), before.ino()) != (after.dev(), after.ino())
            || (before_dir.dev(), before_dir.ino()) != (after_dir.dev(), after_dir.ino())
        {
            return Err(invalid("cue endpoint changed during connection"));
        }
        Ok(Self {
            socket,
            boot: None,
            sequence: 0,
            capture_epoch: None,
            reference_epoch: None,
            trace_offset: 0,
            fault: None,
        })
    }
    pub fn set_boot(&mut self, boot: BootId) {
        self.boot = Some(boot);
    }
    pub fn fault(&self) -> Option<&'static str> {
        self.fault
    }
    /// Each event is processed once, even after an error. Failure latches and
    /// returns once so the coordinator can add one trace receipt without failing audio.
    pub fn scan(&mut self, trace: &[Value], sent_us: u64) -> Option<&'static str> {
        let previous = self.fault;
        while self.trace_offset < trace.len() {
            let event = &trace[self.trace_offset];
            self.trace_offset += 1;
            match event["kind"].as_str() {
                Some("capture_started") => self.capture_epoch = event["details"]["epoch"].as_u64(),
                Some("reference_clock") => {
                    self.reference_epoch = event["details"]["playback_epoch"].as_u64()
                }
                Some("listening_ready") => self.emit(CueKind::ListeningReady, event, sent_us),
                Some("speaker_first_write") => {
                    self.emit(CueKind::SpeakerFirstWrite, event, sent_us)
                }
                Some("speech_final_sample_retired") => {
                    self.emit(CueKind::SpeechRetired, event, sent_us)
                }
                Some("turn_finished") if event["owner"].is_object() => {
                    self.emit(CueKind::Cancelled, event, sent_us)
                }
                Some("run_end") => self.emit(CueKind::RunEnd, event, sent_us),
                _ => {}
            }
        }
        if previous.is_none() { self.fault } else { None }
    }
    fn emit(&mut self, kind: CueKind, event: &Value, sent_us: u64) {
        if self.fault.is_some() {
            return;
        }
        let result = (|| -> io::Result<()> {
            let boot = self
                .boot
                .ok_or_else(|| invalid("cue has no session identity"))?;
            let event_us = event["at_us"]
                .as_u64()
                .ok_or_else(|| invalid("cue has no event timestamp"))?;
            let expires_us = event_us
                .checked_add(CUE_FRESH_US)
                .ok_or_else(|| invalid("cue deadline overflow"))?;
            if sent_us < event_us || sent_us >= expires_us {
                return Err(invalid("cue event expired before send"));
            }
            let sequence = self
                .sequence
                .checked_add(1)
                .ok_or_else(|| invalid("cue sequence exhausted"))?;
            let cue = Cue {
                schema: 1,
                sequence,
                kind,
                boot,
                turn: event["turn"].as_u64(),
                generation: event["owner"]["generation"].as_u64(),
                capture_epoch: self.capture_epoch,
                reference_epoch_context: self.reference_epoch,
                event_us,
                sent_us,
                expires_us,
            };
            let mut bytes = FixedBytes {
                bytes: [0; CUE_MAX_BYTES],
                len: 0,
            };
            serde_json::to_writer(&mut bytes, &cue).map_err(io::Error::other)?;
            if self.socket.send(&bytes.bytes[..bytes.len])? != bytes.len {
                return Err(invalid("partial cue datagram"));
            }
            self.sequence = sequence;
            Ok(())
        })();
        if let Err(error) = result {
            self.fault = Some(if error.kind() == io::ErrorKind::WouldBlock {
                "cue_backpressure"
            } else {
                "cue_invalid_or_unavailable"
            });
        }
    }
}
