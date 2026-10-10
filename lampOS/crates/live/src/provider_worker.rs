//! Cloud I/O stays in a separate process. Input is private, bounded and tied to
//! its capture privacy generation; response lineage never follows a new turn.
use crate::{
    config::ProviderConfig,
    transport::{Channel, WorkerChannels},
    wire::{Control, WorkerEvent},
};
use lamp_gemini::{Event, InputSender, RequestId, SessionId, State};
use lamp_interaction::{BootId, BoundaryGuard, MonoTime, Permission, Snapshot};
use lamp_ipc::monotonic_us;
use serde::{Deserialize, Serialize};
use std::{
    collections::VecDeque,
    io,
    path::Path,
    time::{Duration, Instant},
};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
const MAX_QUEUED_OUTPUT: usize = 512;
const CONTROL_SLICE: usize = 16;
const INPUT_SLICE: usize = 8;
const RETAINED_INPUT_US: u64 = 100_000;

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum ProviderInput {
    Start {
        request: u64,
        privacy_generation: u64,
    },
    Audio {
        request: u64,
        privacy_generation: u64,
        sequence: u64,
        read_completed_at_us: u64,
        samples: Vec<i16>,
    },
    End {
        request: u64,
        privacy_generation: u64,
    },
}
impl ProviderInput {
    fn request(&self) -> u64 {
        match self {
            Self::Start { request, .. }
            | Self::Audio { request, .. }
            | Self::End { request, .. } => *request,
        }
    }
    fn privacy_generation(&self) -> u64 {
        match self {
            Self::Start {
                privacy_generation, ..
            }
            | Self::Audio {
                privacy_generation, ..
            }
            | Self::End {
                privacy_generation, ..
            } => *privacy_generation,
        }
    }
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum ProviderOutput {
    Ready {
        setup_us: u64,
    },
    Started {
        request: u64,
        waiting_for_barrier: bool,
    },
    Audio {
        request: u64,
        sequence: u64,
        /// Wrapper event observation, not a socket-read or acoustic timestamp.
        provider_event_at_us: u64,
        source_frames: usize,
        source_offset: usize,
        samples: Vec<i16>,
    },
    Transcript {
        request: Option<u64>,
        text: String,
        finished: bool,
    },
    GenerationComplete {
        request: u64,
    },
    Interrupted {
        request: u64,
    },
    TurnComplete {
        request: u64,
        idle: bool,
    },
}

pub async fn run(mut channels: WorkerChannels, boot: BootId, config_path: &Path) -> Result<()> {
    let config = ProviderConfig::load(config_path)?.into_session()?;
    channels.control.send(WorkerEvent::Ready)?;
    let mut intake = ProviderIntake::new(boot, monotonic_us());
    let mut ticker = tokio::time::interval(Duration::from_millis(2));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let started = monotonic_us();
    let connect = lamp_gemini::connect(config, SessionId::new(1)?);
    tokio::pin!(connect);
    let mut connection = loop {
        tokio::select! {
            result=&mut connect=>break result?,
            _=ticker.tick()=>{
                let mut control_budget = CONTROL_SLICE;
                if intake.drain_control(&mut monotonic_us, &mut || channels.control.receive(), &mut control_budget)? == ControlDrain::Stopped { return Ok(()); }
            }
        }
    };
    let input = connection.input();
    let mut output = VecDeque::with_capacity(MAX_QUEUED_OUTPUT);
    output.push_back(ProviderOutput::Ready {
        setup_us: monotonic_us() - started,
    });
    let mut output_sequence = 0u64;
    loop {
        tokio::select! {
            biased;
            _=ticker.tick()=>{
                let mut control_budget = CONTROL_SLICE;
                let control = intake.drain_control(&mut monotonic_us, &mut || channels.control.receive(), &mut control_budget)?;
                if control == ControlDrain::Stopped { input.shutdown(); return Ok(()); }
                let state = connection.state();
                if state != State::Ready { return Err(connection_ended(state).into()); }
                // Local retirement cannot wait behind a full control slice.
                intake.handoff.synchronize(&input)?;
                if control == ControlDrain::Deferred { continue; }
                match intake.receive_inputs(&mut monotonic_us, &mut || channels.control.receive(), &mut channels.data, &input, &mut control_budget)? {
                    ControlDrain::Stopped => { input.shutdown(); return Ok(()); }
                    ControlDrain::Deferred => continue,
                    ControlDrain::Empty => {}
                }
                for _ in 0..8 {
                    let Some(front)=output.front() else {break;};
                    match channels.data.send(front) {
                        Ok(())=>{output.pop_front();},
                        Err(error) if error.kind()==io::ErrorKind::WouldBlock=>break,
                        Err(error)=>return Err(error.into()),
                    }
                }
            },
            event=connection.next_event()=>{
                let event=event.ok_or_else(||connection_ended(connection.state()))?;
                match event {
                    Event::InputStarted {lineage,waiting_for_barrier}=>output.push_back(ProviderOutput::Started {request:lineage.request.get(),waiting_for_barrier}),
                    Event::Audio {lineage,pcm,..}=>{
                        let provider_event_at_us = monotonic_us();
                        let source_frames = pcm.len();
                        for (index, part) in pcm.chunks(960).enumerate() {
                            output_sequence=output_sequence.checked_add(1).ok_or_else(||io::Error::other("provider output counter exhausted"))?;
                            output.push_back(ProviderOutput::Audio {request:lineage.request.get(),sequence:output_sequence,provider_event_at_us,source_frames,source_offset:index*960,samples:part.to_vec()});
                        }
                    },
                    Event::OutputTranscript {lineage,text,finished}=>push_text(&mut output,Some(lineage.request.get()),text,finished),
                    Event::UncorrelatedInputTranscript {text,finished,..}=>push_text(&mut output,None,text,finished),
                    Event::ModelText {..}|Event::VoiceActivity {..}=>{},
                    Event::GenerationComplete {lineage}=>output.push_back(ProviderOutput::GenerationComplete {request:lineage.request.get()}),
                    Event::Interrupted {lineage}=>output.push_back(ProviderOutput::Interrupted {request:lineage.request.get()}),
                    Event::TurnComplete {lineage,idle}=>output.push_back(ProviderOutput::TurnComplete {request:lineage.request.get(),idle}),
                }
                if output.len()>MAX_QUEUED_OUTPUT { return Err(io::Error::other("provider IPC output backlog exceeded bound").into()); }
            }
        }
    }
}

/// Gemini state contains only fixed error categories and an optional numeric
/// close code. Never substitute a raw close reason, server body, URL or header.
fn connection_ended(state: State) -> io::Error {
    match state {
        State::Disconnected(error) => {
            io::Error::other(format!("provider connection ended: {error}"))
        }
        State::Closed => io::Error::other("provider connection closed locally"),
        State::Ready => io::Error::other(
            "provider event stream closed without a terminal state: client lifecycle failure",
        ),
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ControlDrain {
    Empty,
    Deferred,
    Stopped,
}

struct RetainedInput {
    command: ProviderInput,
    received_at_us: u64,
}

/// Local process intake; this never grants authority or contacts the provider.
/// Keep at most one original command when the shared control budget is exhausted.
struct ProviderIntake {
    guard: BoundaryGuard,
    handoff: InputHandoff,
    last_control_us: u64,
    saw_allowed: bool,
    retained: Option<RetainedInput>,
    stopped: bool,
}
impl ProviderIntake {
    fn new(boot: BootId, now_us: u64) -> Self {
        Self {
            guard: BoundaryGuard::new(boot, MonoTime::from_micros(now_us)),
            handoff: InputHandoff::default(),
            last_control_us: now_us,
            saw_allowed: false,
            retained: None,
            stopped: false,
        }
    }

    fn stop(&mut self) {
        self.stopped = true;
        self.retained = None;
        self.guard.invalidate();
    }

    fn check_retained(&self, now_us: u64) -> Result<()> {
        if self.stopped {
            return Err(io::Error::other("provider intake stopped").into());
        }
        if self.retained.as_ref().is_some_and(|retained| {
            now_us
                .checked_sub(retained.received_at_us)
                .is_none_or(|age| age >= RETAINED_INPUT_US)
        }) {
            return Err(
                io::Error::other("provider retained input expired or clock regressed").into(),
            );
        }
        Ok(())
    }

    fn drain_control(
        &mut self,
        now: &mut impl FnMut() -> u64,
        receive: &mut impl FnMut() -> io::Result<Option<Control>>,
        remaining: &mut usize,
    ) -> Result<ControlDrain> {
        let result = self.drain_control_inner(now, receive, remaining);
        if result.is_err() {
            self.stop();
        }
        result
    }

    fn drain_control_inner(
        &mut self,
        now: &mut impl FnMut() -> u64,
        receive: &mut impl FnMut() -> io::Result<Option<Control>>,
        remaining: &mut usize,
    ) -> Result<ControlDrain> {
        self.check_retained(now())?;
        let mut empty = false;
        while *remaining > 0 {
            let Some(command) = receive()? else {
                empty = true;
                break;
            };
            *remaining -= 1;
            match command {
                Control::Stop => {
                    self.stop();
                    return Ok(ControlDrain::Stopped);
                }
                Control::Authority { snapshot } => {
                    let at = now();
                    let privacy_changed = self.guard.state().is_some_and(|old| {
                        old.microphone_generation() != snapshot.microphone_generation()
                    });
                    self.guard.install(MonoTime::from_micros(at), snapshot)?;
                    self.handoff.observe_authority(snapshot)?;
                    self.last_control_us = at;
                    if self.saw_allowed
                        && (privacy_changed
                            || snapshot.microphone_permission() != Permission::Allowed)
                    {
                        self.stop();
                        return Ok(ControlDrain::Stopped);
                    }
                    self.saw_allowed |= snapshot.microphone_permission() == Permission::Allowed;
                }
                Control::StartCapture | Control::ConnectReference { .. } => {
                    return Err(io::Error::other("invalid provider command").into());
                }
            }
        }
        if now().saturating_sub(self.last_control_us) > 250_000 {
            return Err(io::Error::other("provider controller heartbeat expired").into());
        }
        Ok(if empty {
            ControlDrain::Empty
        } else {
            ControlDrain::Deferred
        })
    }

    fn receive_inputs(
        &mut self,
        now: &mut impl FnMut() -> u64,
        receive_control: &mut impl FnMut() -> io::Result<Option<Control>>,
        data: &mut Channel,
        input: &impl InputSink,
        remaining: &mut usize,
    ) -> Result<ControlDrain> {
        let result = self.receive_inputs_inner(now, receive_control, data, input, remaining);
        if result.is_err() {
            self.stop();
        }
        result
    }

    fn receive_inputs_inner(
        &mut self,
        now: &mut impl FnMut() -> u64,
        receive_control: &mut impl FnMut() -> io::Result<Option<Control>>,
        data: &mut Channel,
        input: &impl InputSink,
        remaining: &mut usize,
    ) -> Result<ControlDrain> {
        self.check_retained(now())?;
        self.handoff.synchronize(input)?;
        for _ in 0..INPUT_SLICE {
            if self.retained.is_none()
                && let Some(command) = data.receive::<ProviderInput>()?
            {
                self.retained = Some(RetainedInput {
                    command,
                    received_at_us: now(),
                });
            }
            // The parent sends Authority before its dependent input. Both can
            // arrive after the first empty control read, so reread control after
            // data receipt. Never submit while more control may still be queued.
            let control = self.drain_control(now, receive_control, remaining)?;
            if control == ControlDrain::Stopped {
                return Ok(control);
            }
            // Apply observed cancellation even when the bounded slice filled.
            // This preserves Gemini's existing old-response barrier and FIFO End.
            self.handoff.synchronize(input)?;
            if control == ControlDrain::Deferred {
                return Ok(control);
            }
            self.check_retained(now())?;
            let Some(retained) = self.retained.take() else {
                break;
            };
            // Empty control plus unknown owner is a protocol error, not a reason
            // to invent an authority grace period or relabel this command.
            self.handoff.submit(retained.command, now(), input)?;
        }
        Ok(ControlDrain::Empty)
    }
}

/// Only successful submissions change the input's open/closed state. Authority
/// and audio use separate sockets, so root ownership is not proof that a Start
/// or End has reached Gemini yet. This tracker retains no PCM or request history.
#[derive(Default)]
struct InputHandoff {
    authority: Option<Snapshot>,
    highest_seen: u64,
    retired_through: u64,
    retired_sent_through: u64,
    submitted: Option<SubmittedInput>,
}

#[derive(Clone, Copy)]
struct SubmittedInput {
    request: RequestId,
    open: bool,
}

impl InputHandoff {
    /// Called for every validated control packet, not just the final snapshot in
    /// a tick. An owner followed by cancellation must retire its later queued
    /// Start even if that Start has never been submitted to the provider.
    fn observe_authority(&mut self, state: Snapshot) -> Result<()> {
        if let Some(owner) = state.owner() {
            let request = owner.turn();
            if request <= self.retired_through || request < self.highest_seen {
                return Err(
                    io::Error::other("provider authority resurrected a retired request").into(),
                );
            }
            self.highest_seen = request;
            self.retired_through = self.retired_through.max(request - 1);
        } else {
            self.retired_through = self.retired_through.max(self.highest_seen);
        }
        self.authority = Some(state);
        Ok(())
    }

    fn synchronize(&mut self, input: &impl InputSink) -> Result<()> {
        if self.retired_through > self.retired_sent_through {
            // This is synchronous local revocation, independent of queue space
            // or the network. It must happen even if closing the input fails.
            input.retire(RequestId::new(self.retired_through)?);
            self.retired_sent_through = self.retired_through;
        }
        if let Some(submitted) = self.submitted
            && submitted.request.get() <= self.retired_through
        {
            if submitted.open {
                // Enqueued before any successor Start on the same FIFO. Merely
                // dropping a root End that is still on the IPC socket would
                // leave the Gemini actor's old input open and reject the Start.
                input.end(submitted.request)?;
            }
            self.submitted = None;
        }
        Ok(())
    }

    fn submit(
        &mut self,
        command: ProviderInput,
        now_us: u64,
        input: &impl InputSink,
    ) -> Result<()> {
        self.synchronize(input)?;
        let state = self
            .authority
            .ok_or_else(|| io::Error::other("provider has no authority"))?;
        let now = MonoTime::from_micros(now_us);
        if !state.listening_ready(now)
            || command.privacy_generation() != state.microphone_generation()
        {
            return Err(io::Error::other("provider input lost privacy authority").into());
        }
        let request = RequestId::new(command.request())?;
        if request.get() <= self.retired_through {
            // Old PCM retains its old identity and is discarded without reaching
            // the provider. Its sequence/timestamp cannot poison the new input.
            return Ok(());
        }
        if state.owner().map(|owner| owner.turn()) != Some(request.get()) {
            return Err(io::Error::other("provider input has no matching current owner").into());
        }
        match command {
            ProviderInput::Start { .. } => {
                if self.submitted.is_some() {
                    return Err(io::Error::other("provider input already started").into());
                }
                input.start(request)?;
                self.submitted = Some(SubmittedInput {
                    request,
                    open: true,
                });
            }
            ProviderInput::Audio {
                sequence,
                read_completed_at_us,
                samples,
                ..
            } => {
                self.require_open(request)?;
                let age = now_us
                    .checked_sub(read_completed_at_us)
                    .map(Duration::from_micros)
                    .filter(|age| *age < lamp_gemini::MAX_INPUT_AGE)
                    .ok_or_else(|| io::Error::other("invalid or stale capture time"))?;
                let captured = Instant::now()
                    .checked_sub(age)
                    .ok_or_else(|| io::Error::other("invalid capture age"))?;
                input.audio(request, sequence, captured, &samples)?;
            }
            ProviderInput::End { .. } => {
                self.require_open(request)?;
                input.end(request)?;
                if let Some(submitted) = self.submitted.as_mut() {
                    submitted.open = false;
                }
            }
        }
        Ok(())
    }

    fn require_open(&self, request: RequestId) -> Result<()> {
        if !self
            .submitted
            .is_some_and(|submitted| submitted.request == request && submitted.open)
        {
            return Err(io::Error::other("provider input is not open for this request").into());
        }
        Ok(())
    }
}

/// The production sink is the bounded Gemini command queue. The small seam lets
/// regressions exercise cross-channel ordering without credentials or a socket.
trait InputSink {
    fn start(&self, request: RequestId) -> lamp_gemini::Result<()>;
    fn audio(
        &self,
        request: RequestId,
        sequence: u64,
        captured: Instant,
        samples: &[i16],
    ) -> lamp_gemini::Result<()>;
    fn end(&self, request: RequestId) -> lamp_gemini::Result<()>;
    fn retire(&self, request: RequestId);
}

impl InputSink for InputSender {
    fn start(&self, request: RequestId) -> lamp_gemini::Result<()> {
        self.try_start(request)
    }
    fn audio(
        &self,
        request: RequestId,
        sequence: u64,
        captured: Instant,
        samples: &[i16],
    ) -> lamp_gemini::Result<()> {
        self.try_audio(request, sequence, captured, samples)
    }
    fn end(&self, request: RequestId) -> lamp_gemini::Result<()> {
        self.try_end(request)
    }
    fn retire(&self, request: RequestId) {
        InputSender::retire(self, request);
    }
}

fn push_text(
    queue: &mut VecDeque<ProviderOutput>,
    request: Option<u64>,
    mut text: String,
    finished: bool,
) {
    while text.len() > 3500 {
        let mut split = 3500;
        while !text.is_char_boundary(split) {
            split -= 1;
        }
        let tail = text.split_off(split);
        queue.push_back(ProviderOutput::Transcript {
            request,
            text,
            finished: false,
        });
        text = tail;
    }
    queue.push_back(ProviderOutput::Transcript {
        request,
        text,
        finished,
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use lamp_interaction::{AdmissionState, AdmittedInput, CaptureState, Controller};
    use std::cell::{Cell, RefCell};

    const NOW: u64 = 10_000_000;

    #[derive(Debug, Clone, PartialEq, Eq)]
    enum Call {
        Start(u64),
        Audio(u64, u64, Vec<i16>),
        End(u64),
        EndBlocked(u64),
        Retire(u64),
    }

    /// Models FIFO submission and synchronous retirement, not cloud timing.
    /// Gemini's own transport tests cover the old-response barrier separately.
    #[derive(Default)]
    struct RecordingSink {
        calls: RefCell<Vec<Call>>,
        open: Cell<Option<u64>>,
        retired: Cell<u64>,
        reject_end: Cell<bool>,
        reject_start: Cell<bool>,
    }
    impl InputSink for RecordingSink {
        fn start(&self, request: RequestId) -> lamp_gemini::Result<()> {
            if self.reject_start.get() {
                return Err(lamp_gemini::Error::Backpressure);
            }
            if self.open.get().is_some() || request.get() <= self.retired.get() {
                return Err(lamp_gemini::Error::OverlappingInput);
            }
            self.open.set(Some(request.get()));
            self.calls.borrow_mut().push(Call::Start(request.get()));
            Ok(())
        }
        fn audio(
            &self,
            request: RequestId,
            sequence: u64,
            _captured: Instant,
            samples: &[i16],
        ) -> lamp_gemini::Result<()> {
            if self.open.get() != Some(request.get()) || request.get() <= self.retired.get() {
                return Err(lamp_gemini::Error::StaleRequest);
            }
            self.calls
                .borrow_mut()
                .push(Call::Audio(request.get(), sequence, samples.to_vec()));
            Ok(())
        }
        fn end(&self, request: RequestId) -> lamp_gemini::Result<()> {
            if self.reject_end.get() {
                self.calls
                    .borrow_mut()
                    .push(Call::EndBlocked(request.get()));
                return Err(lamp_gemini::Error::Backpressure);
            }
            if self.open.get() != Some(request.get()) {
                return Err(lamp_gemini::Error::StaleRequest);
            }
            self.calls.borrow_mut().push(Call::End(request.get()));
            self.open.set(None);
            Ok(())
        }
        fn retire(&self, request: RequestId) {
            self.retired.set(self.retired.get().max(request.get()));
            self.calls.borrow_mut().push(Call::Retire(request.get()));
        }
    }

    fn controller() -> Controller {
        let mut owner = Controller::new(BootId::new([9; 16]).unwrap(), time());
        owner
            .set_microphone_permission(time(), Permission::Allowed)
            .unwrap();
        owner
            .set_capture(
                time(),
                CaptureState::RetainingUntil(MonoTime::from_micros(NOW + 1_000_000)),
            )
            .unwrap();
        owner
            .set_admission(
                time(),
                AdmissionState::OpenUntil(MonoTime::from_micros(NOW + 1_000_000)),
            )
            .unwrap();
        owner
    }
    fn time() -> MonoTime {
        MonoTime::from_micros(NOW)
    }
    fn authorize(owner: &mut Controller, handoff: &mut InputHandoff) -> u64 {
        let turn = owner.admit(time(), AdmittedInput::NewTurn).unwrap();
        handoff
            .observe_authority(owner.snapshot(time()).unwrap())
            .unwrap();
        turn.turn()
    }
    fn generation(handoff: &InputHandoff) -> u64 {
        handoff.authority.unwrap().microphone_generation()
    }
    fn start(request: u64, handoff: &InputHandoff) -> ProviderInput {
        ProviderInput::Start {
            request,
            privacy_generation: generation(handoff),
        }
    }
    fn audio(request: u64, sequence: u64, sample: i16, handoff: &InputHandoff) -> ProviderInput {
        ProviderInput::Audio {
            request,
            privacy_generation: generation(handoff),
            sequence,
            read_completed_at_us: NOW - 200_000,
            samples: vec![sample; 160],
        }
    }
    fn end(request: u64, handoff: &InputHandoff) -> ProviderInput {
        ProviderInput::End {
            request,
            privacy_generation: generation(handoff),
        }
    }

    fn intake_channels() -> (
        crate::process::SessionDirectory,
        WorkerChannels,
        WorkerChannels,
    ) {
        let directory = crate::process::SessionDirectory::create().unwrap();
        let parent_boot = BootId::new([9; 16]).unwrap();
        let worker_boot = BootId::new([8; 16]).unwrap();
        let mut parent =
            WorkerChannels::bind(&directory.path, "provider", parent_boot, worker_boot, false)
                .unwrap();
        let mut worker =
            WorkerChannels::bind(&directory.path, "provider", parent_boot, worker_boot, true)
                .unwrap();
        parent.connect(&directory.path, "provider", false).unwrap();
        worker.connect(&directory.path, "provider", true).unwrap();
        (directory, parent, worker)
    }

    #[test]
    fn separate_socket_authority_in_empty_drain_gap_precedes_new_start() {
        let (_directory, mut parent, mut worker) = intake_channels();
        let mut owner = controller();
        let mut intake = ProviderIntake::new(BootId::new([9; 16]).unwrap(), NOW);
        let sink = RecordingSink::default();
        let initial = owner.snapshot(time()).unwrap();
        parent
            .control
            .send(Control::Authority { snapshot: initial })
            .unwrap();
        let mut initial_budget = CONTROL_SLICE;
        intake
            .drain_control(
                &mut || NOW,
                &mut || worker.control.receive(),
                &mut initial_budget,
            )
            .unwrap();
        let turn = owner.admit(time(), AdmittedInput::NewTurn).unwrap();
        let state = owner.snapshot(time()).unwrap();
        let mut injected = false;
        let mut control_budget = CONTROL_SLICE;
        assert_eq!(
            intake
                .drain_control(
                    &mut || NOW,
                    &mut || {
                        let control = worker.control.receive()?;
                        if !injected {
                            assert!(control.is_none());
                            injected = true;
                            // Force the legal interleaving: parent sends control before data,
                            // but both sends occur after the worker observed an empty control socket.
                            parent
                                .control
                                .send(Control::Authority { snapshot: state })?;
                            parent.data.send(ProviderInput::Start {
                                request: turn.turn(),
                                privacy_generation: state.microphone_generation(),
                            })?;
                        }
                        Ok(control)
                    },
                    &mut control_budget
                )
                .unwrap(),
            ControlDrain::Empty
        );
        assert_eq!(
            intake
                .receive_inputs(
                    &mut || NOW,
                    &mut || worker.control.receive(),
                    &mut worker.data,
                    &sink,
                    &mut control_budget
                )
                .unwrap(),
            ControlDrain::Empty
        );
        assert!(injected);
        assert_eq!(*sink.calls.borrow(), vec![Call::Start(turn.turn())]);
    }

    struct SocketRig {
        _directory: crate::process::SessionDirectory,
        parent: WorkerChannels,
        worker: WorkerChannels,
        owner: Controller,
        intake: ProviderIntake,
        sink: RecordingSink,
    }
    impl SocketRig {
        fn new() -> Self {
            let (directory, parent, worker) = intake_channels();
            let mut rig = Self {
                _directory: directory,
                parent,
                worker,
                owner: controller(),
                intake: ProviderIntake::new(BootId::new([9; 16]).unwrap(), NOW),
                sink: RecordingSink::default(),
            };
            rig.owner.admit(time(), AdmittedInput::NewTurn).unwrap();
            let state = rig.owner.snapshot(time()).unwrap();
            rig.parent
                .control
                .send(Control::Authority { snapshot: state })
                .unwrap();
            rig.parent
                .data
                .send(ProviderInput::Start {
                    request: 1,
                    privacy_generation: state.microphone_generation(),
                })
                .unwrap();
            assert_eq!(rig.tick(NOW).unwrap(), ControlDrain::Empty);
            assert_eq!(*rig.sink.calls.borrow(), vec![Call::Start(1)]);
            rig
        }
        fn tick(&mut self, at: u64) -> Result<ControlDrain> {
            let mut budget = CONTROL_SLICE;
            let control = self.intake.drain_control(
                &mut || at,
                &mut || self.worker.control.receive(),
                &mut budget,
            )?;
            if control == ControlDrain::Stopped {
                return Ok(control);
            }
            self.intake.handoff.synchronize(&self.sink)?;
            if control == ControlDrain::Deferred {
                return Ok(control);
            }
            self.intake.receive_inputs(
                &mut || at,
                &mut || self.worker.control.receive(),
                &mut self.worker.data,
                &self.sink,
                &mut budget,
            )
        }
    }

    #[test]
    fn separate_socket_interruption_retires_old_data_and_keeps_new_prefix() {
        let mut rig = SocketRig::new();
        let new = rig
            .owner
            .admit(time(), AdmittedInput::Interruption)
            .unwrap();
        let state = rig.owner.snapshot(time()).unwrap();
        // Already queued old audio is intentionally malformed; once retired it
        // must not poison the next question's sequence, samples or capture age.
        rig.parent
            .data
            .send(ProviderInput::Audio {
                request: 1,
                privacy_generation: state.microphone_generation(),
                sequence: u64::MAX,
                read_completed_at_us: u64::MAX,
                samples: vec![],
            })
            .unwrap();
        let mut injected = false;
        let mut budget = CONTROL_SLICE;
        assert_eq!(
            rig.intake
                .drain_control(
                    &mut || NOW,
                    &mut || {
                        let control = rig.worker.control.receive()?;
                        if !injected {
                            assert!(control.is_none());
                            injected = true;
                            rig.parent
                                .control
                                .send(Control::Authority { snapshot: state })?;
                            for command in [
                                ProviderInput::Start {
                                    request: new.turn(),
                                    privacy_generation: state.microphone_generation(),
                                },
                                ProviderInput::Audio {
                                    request: new.turn(),
                                    privacy_generation: state.microphone_generation(),
                                    sequence: 1,
                                    read_completed_at_us: NOW - 200_000,
                                    samples: vec![21; 160],
                                },
                                ProviderInput::End {
                                    request: new.turn(),
                                    privacy_generation: state.microphone_generation(),
                                },
                            ] {
                                rig.parent.data.send(command)?;
                            }
                        }
                        Ok(control)
                    },
                    &mut budget
                )
                .unwrap(),
            ControlDrain::Empty
        );
        assert_eq!(
            rig.intake
                .receive_inputs(
                    &mut || NOW,
                    &mut || rig.worker.control.receive(),
                    &mut rig.worker.data,
                    &rig.sink,
                    &mut budget
                )
                .unwrap(),
            ControlDrain::Empty
        );
        assert_eq!(
            *rig.sink.calls.borrow(),
            vec![
                Call::Start(1),
                Call::Retire(1),
                Call::End(1),
                Call::Start(2),
                Call::Audio(2, 1, vec![21; 160]),
                Call::End(2)
            ]
        );
    }

    #[test]
    fn separate_socket_stop_privacy_and_coalesced_reopen_precede_input() {
        for mode in 0..3 {
            let mut rig = SocketRig::new();
            let audio = audio(1, 1, 41, &rig.intake.handoff);
            let priority = if mode == 0 {
                Control::Stop
            } else {
                rig.owner
                    .set_microphone_permission(time(), Permission::Denied)
                    .unwrap();
                if mode == 2 {
                    rig.owner
                        .set_microphone_permission(time(), Permission::Allowed)
                        .unwrap();
                }
                Control::Authority {
                    snapshot: rig.owner.snapshot(time()).unwrap(),
                }
            };
            let mut pending = Some((priority, audio));
            let mut budget = CONTROL_SLICE;
            assert_eq!(
                rig.intake
                    .drain_control(
                        &mut || NOW,
                        &mut || {
                            let control = rig.worker.control.receive()?;
                            if let Some((priority, command)) = pending.take() {
                                assert!(control.is_none());
                                rig.parent.control.send(priority)?;
                                rig.parent.data.send(command)?;
                            }
                            Ok(control)
                        },
                        &mut budget
                    )
                    .unwrap(),
                ControlDrain::Empty
            );
            assert_eq!(
                rig.intake
                    .receive_inputs(
                        &mut || NOW,
                        &mut || rig.worker.control.receive(),
                        &mut rig.worker.data,
                        &rig.sink,
                        &mut budget
                    )
                    .unwrap(),
                ControlDrain::Stopped
            );
            assert_eq!(*rig.sink.calls.borrow(), vec![Call::Start(1)]);
            assert!(rig.intake.retained.is_none());
            assert!(rig.tick(NOW).is_err());
        }
    }

    #[test]
    fn full_control_budget_defers_one_original_input_and_expires_at_original_receipt() {
        for expire in [false, true] {
            let mut rig = SocketRig::new();
            rig.owner
                .admit(time(), AdmittedInput::Interruption)
                .unwrap();
            let mut states: VecDeque<_> = (0..17)
                .map(|_| rig.owner.snapshot(time()).unwrap())
                .collect();
            let privacy = states[0].microphone_generation();
            let mut budget = CONTROL_SLICE;
            assert_eq!(
                rig.intake
                    .drain_control(
                        &mut || NOW,
                        &mut || rig.worker.control.receive(),
                        &mut budget
                    )
                    .unwrap(),
                ControlDrain::Empty
            );
            // Queue new input and authority after the initial drain. Further
            // authority packets are fed one per receive, independent of the OS
            // Unix datagram queue limit.
            rig.parent
                .data
                .send(ProviderInput::Start {
                    request: 2,
                    privacy_generation: privacy,
                })
                .unwrap();
            rig.parent
                .data
                .send(ProviderInput::Audio {
                    request: 2,
                    privacy_generation: privacy,
                    sequence: 1,
                    read_completed_at_us: NOW - 200_000,
                    samples: vec![51; 160],
                })
                .unwrap();
            rig.parent
                .control
                .send(Control::Authority {
                    snapshot: states.pop_front().unwrap(),
                })
                .unwrap();
            assert_eq!(
                rig.intake
                    .receive_inputs(
                        &mut || NOW,
                        &mut || {
                            let control = rig.worker.control.receive()?;
                            if control.is_some()
                                && let Some(snapshot) = states.pop_front()
                            {
                                rig.parent.control.send(Control::Authority { snapshot })?;
                            }
                            Ok(control)
                        },
                        &mut rig.worker.data,
                        &rig.sink,
                        &mut budget
                    )
                    .unwrap(),
                ControlDrain::Deferred
            );
            assert_eq!(budget, 0);
            assert!(states.is_empty()); // Last authority remains on the control socket.
            let retained = rig.intake.retained.as_ref().unwrap();
            assert_eq!(retained.received_at_us, NOW);
            assert_eq!(retained.command.request(), 2);
            assert_eq!(
                *rig.sink.calls.borrow(),
                vec![Call::Start(1), Call::Retire(1), Call::End(1)]
            );
            if expire {
                let error = rig.tick(NOW + RETAINED_INPUT_US).unwrap_err();
                assert!(error.to_string().contains("retained input expired"));
                assert!(rig.intake.retained.is_none());
                assert_eq!(
                    *rig.sink.calls.borrow(),
                    vec![Call::Start(1), Call::Retire(1), Call::End(1)]
                );
            } else {
                assert_eq!(rig.tick(NOW + 1).unwrap(), ControlDrain::Empty);
                assert!(rig.intake.retained.is_none());
                assert_eq!(
                    *rig.sink.calls.borrow(),
                    vec![
                        Call::Start(1),
                        Call::Retire(1),
                        Call::End(1),
                        Call::Start(2),
                        Call::Audio(2, 1, vec![51; 160])
                    ]
                );
            }
        }
    }

    #[test]
    fn intake_keeps_eight_command_slice_and_does_not_wait_for_unknown_authority() {
        let mut rig = SocketRig::new();
        for sequence in 1..=9 {
            rig.parent
                .data
                .send(audio(1, sequence, sequence as i16, &rig.intake.handoff))
                .unwrap();
        }
        rig.tick(NOW).unwrap();
        assert_eq!(
            rig.sink
                .calls
                .borrow()
                .iter()
                .filter(|call| matches!(call, Call::Audio(..)))
                .count(),
            INPUT_SLICE
        );
        rig.tick(NOW).unwrap();
        assert_eq!(rig.sink.calls.borrow().len(), 10);
        rig.parent
            .data
            .send(ProviderInput::Start {
                request: 2,
                privacy_generation: generation(&rig.intake.handoff),
            })
            .unwrap();
        let error = rig.tick(NOW).unwrap_err();
        assert!(error.to_string().contains("no matching current owner"));
        assert!(rig.intake.retained.is_none());
        assert!(rig.intake.stopped);
    }

    #[test]
    fn intake_does_not_extend_snapshot_capture_or_controller_leases() {
        for capture_expiry in [false, true] {
            let mut rig = SocketRig::new();
            if capture_expiry {
                rig.owner
                    .set_capture(
                        time(),
                        CaptureState::RetainingUntil(MonoTime::from_micros(NOW + 10)),
                    )
                    .unwrap();
                rig.parent
                    .control
                    .send(Control::Authority {
                        snapshot: rig.owner.snapshot(time()).unwrap(),
                    })
                    .unwrap();
                rig.tick(NOW).unwrap();
            }
            let expires = rig
                .intake
                .handoff
                .authority
                .unwrap()
                .expires_at()
                .as_micros();
            rig.parent
                .data
                .send(audio(1, 1, 71, &rig.intake.handoff))
                .unwrap();
            assert!(rig.tick(expires).is_err());
            assert_eq!(*rig.sink.calls.borrow(), vec![Call::Start(1)]);
        }
        let mut rig = SocketRig::new();
        let error = rig.tick(NOW + 250_001).unwrap_err();
        assert!(error.to_string().contains("controller heartbeat expired"));
        assert!(rig.intake.stopped);
    }

    #[test]
    fn priority_transfer_closes_old_input_before_successor_and_discards_queued_old_data() {
        let mut owner = controller();
        let mut handoff = InputHandoff::default();
        let sink = RecordingSink::default();
        let old = authorize(&mut owner, &mut handoff);
        handoff.submit(start(old, &handoff), NOW, &sink).unwrap();
        handoff
            .submit(audio(old, 1, 11, &handoff), NOW, &sink)
            .unwrap();

        let new = authorize(&mut owner, &mut handoff);
        handoff.synchronize(&sink).unwrap();
        // These messages were already in the other IPC socket before transfer.
        for command in [
            audio(old, 2, 99, &handoff),
            end(old, &handoff),
            start(old, &handoff),
            end(old, &handoff),
        ] {
            handoff.submit(command, NOW, &sink).unwrap();
        }
        handoff.submit(start(new, &handoff), NOW, &sink).unwrap();
        handoff
            .submit(audio(new, 1, 21, &handoff), NOW, &sink)
            .unwrap();
        handoff
            .submit(audio(new, 2, 22, &handoff), NOW, &sink)
            .unwrap();
        handoff.submit(end(new, &handoff), NOW, &sink).unwrap();
        assert_eq!(
            *sink.calls.borrow(),
            vec![
                Call::Start(old),
                Call::Audio(old, 1, vec![11; 160]),
                Call::Retire(old),
                Call::End(old),
                Call::Start(new),
                Call::Audio(new, 1, vec![21; 160]),
                Call::Audio(new, 2, vec![22; 160]),
                Call::End(new),
            ]
        );
    }

    #[test]
    fn an_already_submitted_end_is_not_repeated_on_retirement() {
        let mut owner = controller();
        let mut handoff = InputHandoff::default();
        let sink = RecordingSink::default();
        let old = authorize(&mut owner, &mut handoff);
        handoff.submit(start(old, &handoff), NOW, &sink).unwrap();
        handoff.submit(end(old, &handoff), NOW, &sink).unwrap();
        let new = authorize(&mut owner, &mut handoff);
        handoff.synchronize(&sink).unwrap();
        handoff.synchronize(&sink).unwrap();
        handoff.submit(end(old, &handoff), NOW, &sink).unwrap();
        handoff.submit(start(new, &handoff), NOW, &sink).unwrap();
        assert_eq!(
            *sink.calls.borrow(),
            vec![
                Call::Start(old),
                Call::End(old),
                Call::Retire(old),
                Call::Start(new)
            ]
        );
    }

    #[test]
    fn overtaken_start_never_creates_a_phantom_old_input_or_end() {
        let mut owner = controller();
        let mut handoff = InputHandoff::default();
        let sink = RecordingSink::default();
        let old = authorize(&mut owner, &mut handoff);
        let new = authorize(&mut owner, &mut handoff);
        for command in [
            start(old, &handoff),
            audio(old, 1, 11, &handoff),
            end(old, &handoff),
        ] {
            handoff.submit(command, NOW, &sink).unwrap();
        }
        handoff.submit(start(new, &handoff), NOW, &sink).unwrap();
        assert_eq!(
            *sink.calls.borrow(),
            vec![Call::Retire(old), Call::Start(new)]
        );
    }

    #[test]
    fn coalesced_owner_then_cancellation_retires_a_start_not_yet_received() {
        let mut owner = controller();
        let mut handoff = InputHandoff::default();
        let sink = RecordingSink::default();
        let old = authorize(&mut owner, &mut handoff);
        let snapshot = owner.snapshot(time()).unwrap();
        owner
            .cancel_turn(time(), snapshot.owner().unwrap())
            .unwrap();
        handoff
            .observe_authority(owner.snapshot(time()).unwrap())
            .unwrap();
        handoff.submit(start(old, &handoff), NOW, &sink).unwrap();
        handoff.submit(end(old, &handoff), NOW, &sink).unwrap();
        assert_eq!(*sink.calls.borrow(), vec![Call::Retire(old)]);
        assert!(handoff.observe_authority(snapshot).is_err());
    }

    #[test]
    fn a_future_or_unknown_request_cannot_borrow_current_authority() {
        let mut owner = controller();
        let mut handoff = InputHandoff::default();
        let sink = RecordingSink::default();
        let current = authorize(&mut owner, &mut handoff);
        for command in [
            start(0, &handoff),
            start(current + 1, &handoff),
            audio(current + 1, 1, 90, &handoff),
            end(current + 1, &handoff),
        ] {
            assert!(handoff.submit(command, NOW, &sink).is_err());
        }
        assert!(sink.calls.borrow().is_empty());
        handoff
            .submit(start(current, &handoff), NOW, &sink)
            .unwrap();
    }

    #[test]
    fn input_requires_a_successful_start_and_cannot_reopen_after_end() {
        let mut owner = controller();
        let mut handoff = InputHandoff::default();
        let sink = RecordingSink::default();
        let current = authorize(&mut owner, &mut handoff);
        assert!(
            handoff
                .submit(audio(current, 1, 10, &handoff), NOW, &sink)
                .is_err()
        );
        assert!(handoff.submit(end(current, &handoff), NOW, &sink).is_err());
        handoff
            .submit(start(current, &handoff), NOW, &sink)
            .unwrap();
        assert!(
            handoff
                .submit(start(current, &handoff), NOW, &sink)
                .is_err()
        );
        handoff.submit(end(current, &handoff), NOW, &sink).unwrap();
        assert!(handoff.submit(end(current, &handoff), NOW, &sink).is_err());
        assert!(
            handoff
                .submit(audio(current, 2, 20, &handoff), NOW, &sink)
                .is_err()
        );
        assert_eq!(
            *sink.calls.borrow(),
            vec![Call::Start(current), Call::End(current)]
        );
    }

    #[test]
    fn close_backpressure_revokes_output_but_never_starts_a_successor() {
        let mut owner = controller();
        let mut handoff = InputHandoff::default();
        let sink = RecordingSink::default();
        let old = authorize(&mut owner, &mut handoff);
        handoff.submit(start(old, &handoff), NOW, &sink).unwrap();
        let new = authorize(&mut owner, &mut handoff);
        sink.reject_end.set(true);
        assert!(handoff.submit(start(new, &handoff), NOW, &sink).is_err());
        assert_eq!(
            *sink.calls.borrow(),
            vec![Call::Start(old), Call::Retire(old), Call::EndBlocked(old)]
        );
        assert_eq!(handoff.submitted.unwrap().request.get(), old);
        assert!(handoff.submitted.unwrap().open);
    }

    #[test]
    fn rejected_start_is_not_later_closed_as_if_it_had_been_submitted() {
        let mut owner = controller();
        let mut handoff = InputHandoff::default();
        let sink = RecordingSink::default();
        let old = authorize(&mut owner, &mut handoff);
        sink.reject_start.set(true);
        assert!(handoff.submit(start(old, &handoff), NOW, &sink).is_err());
        sink.reject_start.set(false);
        let new = authorize(&mut owner, &mut handoff);
        handoff.submit(start(new, &handoff), NOW, &sink).unwrap();
        assert_eq!(
            *sink.calls.borrow(),
            vec![Call::Retire(old), Call::Start(new)]
        );
    }

    #[test]
    fn privacy_generation_and_expired_authority_still_prevent_audio() {
        let mut owner = controller();
        let mut handoff = InputHandoff::default();
        let sink = RecordingSink::default();
        let current = authorize(&mut owner, &mut handoff);
        handoff
            .submit(start(current, &handoff), NOW, &sink)
            .unwrap();
        let mut wrong_generation = audio(current, 1, 10, &handoff);
        if let ProviderInput::Audio {
            privacy_generation, ..
        } = &mut wrong_generation
        {
            *privacy_generation += 1;
        }
        assert!(handoff.submit(wrong_generation, NOW, &sink).is_err());
        let expires = handoff.authority.unwrap().expires_at().as_micros();
        assert!(
            handoff
                .submit(audio(current, 1, 10, &handoff), expires, &sink)
                .is_err()
        );
        owner
            .set_microphone_permission(time(), Permission::Denied)
            .unwrap();
        handoff
            .observe_authority(owner.snapshot(time()).unwrap())
            .unwrap();
        assert!(
            handoff
                .submit(audio(current, 1, 10, &handoff), NOW, &sink)
                .is_err()
        );
        assert!(
            sink.calls
                .borrow()
                .iter()
                .all(|call| !matches!(call, Call::Audio(..)))
        );
    }

    #[test]
    fn retained_prefix_keeps_capture_age_and_rejects_stale_or_future_samples() {
        let mut owner = controller();
        let mut handoff = InputHandoff::default();
        let sink = RecordingSink::default();
        let current = authorize(&mut owner, &mut handoff);
        handoff
            .submit(start(current, &handoff), NOW, &sink)
            .unwrap();
        handoff
            .submit(audio(current, 1, 10, &handoff), NOW, &sink)
            .unwrap();
        for captured_at in [NOW - 1_000_000, NOW + 1] {
            let mut invalid = audio(current, 2, 90, &handoff);
            if let ProviderInput::Audio {
                read_completed_at_us,
                ..
            } = &mut invalid
            {
                *read_completed_at_us = captured_at;
            }
            assert!(handoff.submit(invalid, NOW, &sink).is_err());
        }
        assert_eq!(
            *sink.calls.borrow(),
            vec![Call::Start(current), Call::Audio(current, 1, vec![10; 160])]
        );
    }

    #[test]
    fn retired_capture_metadata_does_not_poison_successor_sequence_or_prefix() {
        let mut owner = controller();
        let mut handoff = InputHandoff::default();
        let sink = RecordingSink::default();
        let old = authorize(&mut owner, &mut handoff);
        let new = authorize(&mut owner, &mut handoff);
        let mut stale = audio(old, u64::MAX, 99, &handoff);
        if let ProviderInput::Audio {
            read_completed_at_us,
            samples,
            ..
        } = &mut stale
        {
            *read_completed_at_us = u64::MAX;
            samples.clear();
        }
        handoff.submit(stale, NOW, &sink).unwrap();
        handoff.submit(start(new, &handoff), NOW, &sink).unwrap();
        handoff
            .submit(audio(new, 1, 20, &handoff), NOW, &sink)
            .unwrap();
        assert_eq!(
            *sink.calls.borrow(),
            vec![
                Call::Retire(old),
                Call::Start(new),
                Call::Audio(new, 1, vec![20; 160])
            ]
        );
    }

    #[test]
    fn event_eof_preserves_sanitized_peer_close_code() {
        let error = connection_ended(State::Disconnected(lamp_gemini::Error::PeerClosed {
            code: Some(4029),
        }));
        assert_eq!(
            error.to_string(),
            "provider connection ended: Gemini Live PeerClosed { code: Some(4029) }"
        );
        assert_eq!(
            connection_ended(State::Disconnected(lamp_gemini::Error::ServerRejected)).to_string(),
            "provider connection ended: Gemini Live ServerRejected"
        );
    }

    #[test]
    fn event_eof_distinguishes_client_lifecycle_failure_from_local_shutdown() {
        assert!(
            connection_ended(State::Ready)
                .to_string()
                .contains("client lifecycle failure")
        );
        assert_eq!(
            connection_ended(State::Closed).to_string(),
            "provider connection closed locally"
        );
    }
}
