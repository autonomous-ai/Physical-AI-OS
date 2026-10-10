//! Cloud I/O stays in a separate process. Input is private, bounded and tied to
//! its capture privacy generation; response lineage never follows a new turn.
//!
//! A lost provider connection is recovered here only when nothing accepted can
//! be lost or repeated: while idle, or after an answer's generation completed
//! and was delivered. Any other loss ends the worker with the request and the
//! stage it had reached. Input is never replayed and no answer is requested
//! twice. See `docs/gemini-session-reliability.md`.
use crate::{
    config::ProviderConfig,
    provider_flow::{OutputSender, PACKET_SAMPLES},
    transport::{Channel, WorkerChannels},
    wire::{Control, WorkerEvent},
};
use lamp_gemini::{
    Connect, Dialer, Discard, Event, Failure, FailureReason, Notice, RecoveryPolicy, RequestId,
    Resumption, State, Supervisor,
};
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
/// Above this backlog no further provider event is taken. The transport then
/// stops reading its socket, so a fast or bursty service is held back by flow
/// control instead of ending the worker. One event adds at most 50 packets.
const OUTPUT_HIGH_WATER: usize = 384;
/// Initial setup is unchanged. A handle the service volunteers is kept in this
/// process's memory only, so a reconnect can restore the conversation.
const RESUMPTION: Resumption = Resumption::Retain;
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

fn prune_retired_output(output: &mut VecDeque<ProviderOutput>, retired_through: u64) {
    output.retain(|event| {
        let request = match event {
            ProviderOutput::Ready { .. } => None,
            ProviderOutput::Transcript { request, .. } => *request,
            ProviderOutput::Started { request, .. }
            | ProviderOutput::Audio { request, .. }
            | ProviderOutput::GenerationComplete { request }
            | ProviderOutput::Interrupted { request }
            | ProviderOutput::TurnComplete { request, .. } => Some(*request),
        };
        request.is_none_or(|request| request > retired_through)
    });
}

pub async fn run(channels: WorkerChannels, boot: BootId, config_path: &Path) -> Result<()> {
    let config = ProviderConfig::load(config_path)?
        .into_session()?
        .resumption(RESUMPTION);
    let provider = Supervisor::new(Dialer::new(config), RecoveryPolicy::default())?;
    serve(channels, boot, provider).await
}

async fn serve<C: Connect>(
    mut channels: WorkerChannels,
    boot: BootId,
    mut provider: Supervisor<C>,
) -> Result<()> {
    channels.control.send(WorkerEvent::Ready)?;
    let mut intake = ProviderIntake::new(boot, monotonic_us());
    let mut ticker = tokio::time::interval(Duration::from_millis(2));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut output = VecDeque::with_capacity(MAX_QUEUED_OUTPUT);
    let mut output_sequence = 0u64;
    let mut discards = DiscardLog::default();
    loop {
        // Control is polled first and never waits for the provider: stop,
        // privacy and retirement are serviced every tick whether a connection
        // is ready, reconnecting or delivering a burst.
        tokio::select! {
            biased;
            _=ticker.tick()=>{
                let mut control_budget = CONTROL_SLICE;
                let control = intake.drain_control(&mut monotonic_us, &mut || channels.control.receive(), &mut control_budget)?;
                if control == ControlDrain::Stopped { provider.shutdown(); return Ok(()); }
                // Local retirement cannot wait behind a full control slice.
                intake.handoff.synchronize(&provider)?;
                prune_retired_output(&mut output, intake.handoff.retired_through);
                if control == ControlDrain::Deferred { continue; }
                let received = intake.receive_inputs(&mut monotonic_us, &mut || channels.control.receive(), &mut channels.data, &provider, &mut control_budget);
                match received {
                    Ok(ControlDrain::Stopped) => { provider.shutdown(); return Ok(()); }
                    Ok(ControlDrain::Deferred) => continue,
                    Ok(ControlDrain::Empty) => {}
                    // Admitted input cannot be held until a connection exists:
                    // it would be answered late or not at all. Say so and end.
                    Err(error) if !provider.is_connected() => return Err(io::Error::other(format!("provider unavailable while input was admitted: {error}")).into()),
                    Err(error) => return Err(error),
                }
                // receive_inputs can observe a newer authority too.
                prune_retired_output(&mut output, intake.handoff.retired_through);
                for _ in 0..8 {
                    let Some(front)=output.front() else {break;};
                    if intake.output_flow.try_send(|| channels.data.send(front))? {
                        output.pop_front();
                    } else { break; }
                }
            },
            notice=provider.next(), if output.len() < OUTPUT_HIGH_WATER=>{
                match notice {
                    Ok(Notice::Ready {session,context,attempts,elapsed,after})=>{
                        if let Some(error)=after {
                            // The coordinator contract carries a recovery only as
                            // a repeated Ready. Fixed categories, no server text.
                            eprintln!("lamp-live provider: recovered after {error}; session={} context={context:?} attempts={attempts} outage_ms={}", session.get(), elapsed.as_millis());
                        }
                        output.push_back(ProviderOutput::Ready { setup_us: elapsed.as_micros().try_into().unwrap_or(u64::MAX) });
                    },
                    Ok(Notice::Event(event))=>forward(event, &mut output, &mut output_sequence, &mut discards)?,
                    Ok(Notice::Settled {lineage,error})=>{
                        // Generation completed and every block was forwarded.
                        // The closed connection is this request's idle barrier.
                        eprintln!("lamp-live provider: request {} settled by {error} after generation completed", lineage.request.get());
                        output.push_back(ProviderOutput::TurnComplete {request:lineage.request.get(),idle:true});
                    },
                    Ok(Notice::TurnLost {lineage,stage,error})=>return Err(io::Error::other(format!("{}; request {} was lost at {stage:?} and is not retried", connection_ended(State::Disconnected(error)), lineage.request.get())).into()),
                    Err(failure)=>return Err(ended(failure).into()),
                }
                if output.len()>MAX_QUEUED_OUTPUT { return Err(io::Error::other("provider IPC output backlog exceeded bound").into()); }
            }
        }
    }
}

fn forward(
    event: Event,
    output: &mut VecDeque<ProviderOutput>,
    output_sequence: &mut u64,
    discards: &mut DiscardLog,
) -> Result<()> {
    match event {
        Event::InputStarted {
            lineage,
            waiting_for_barrier,
        } => output.push_back(ProviderOutput::Started {
            request: lineage.request.get(),
            waiting_for_barrier,
        }),
        Event::Audio { lineage, pcm, .. } => {
            let provider_event_at_us = monotonic_us();
            let source_frames = pcm.len();
            for (index, part) in pcm.chunks(PACKET_SAMPLES).enumerate() {
                *output_sequence = output_sequence
                    .checked_add(1)
                    .ok_or_else(|| io::Error::other("provider output counter exhausted"))?;
                output.push_back(ProviderOutput::Audio {
                    request: lineage.request.get(),
                    sequence: *output_sequence,
                    provider_event_at_us,
                    source_frames,
                    source_offset: index * PACKET_SAMPLES,
                    samples: part.to_vec(),
                });
            }
        }
        Event::OutputTranscript {
            lineage,
            text,
            finished,
        } => push_text(output, Some(lineage.request.get()), text, finished),
        Event::UncorrelatedInputTranscript { text, finished, .. } => {
            push_text(output, None, text, finished);
        }
        Event::ModelText { .. } | Event::VoiceActivity { .. } => {}
        Event::GenerationComplete { lineage } => {
            output.push_back(ProviderOutput::GenerationComplete {
                request: lineage.request.get(),
            });
        }
        Event::Interrupted { lineage } => output.push_back(ProviderOutput::Interrupted {
            request: lineage.request.get(),
        }),
        Event::TurnComplete { lineage, idle } => output.push_back(ProviderOutput::TurnComplete {
            request: lineage.request.get(),
            idle,
        }),
        Event::GoAway { time_left, .. } => eprintln!(
            "lamp-live provider: service announced a close; time_left_ms={:?}",
            time_left.map(|left| left.as_millis())
        ),
        Event::Discarded { reason, .. } => discards.note(reason),
    }
    Ok(())
}

/// Late provider events never reach the coordinator. Each kind is reported
/// once per worker so a recurring pattern is visible without unbounded output.
#[derive(Default)]
struct DiscardLog {
    seen: [bool; 5],
}
impl DiscardLog {
    fn note(&mut self, reason: Discard) {
        let index = match reason {
            Discard::LateInterruption => 0,
            Discard::LateTerminal => 1,
            Discard::LateOutput => 2,
            Discard::UnownedOutput => 3,
            Discard::UnansweredBarrier => 4,
        };
        if !std::mem::replace(&mut self.seen[index], true) {
            eprintln!(
                "lamp-live provider: dropped a late provider event ({reason:?}); it changed no request"
            );
        }
    }
}

/// Terminal provider failure as one line of fixed categories.
fn ended(failure: Failure) -> io::Error {
    match failure.reason {
        FailureReason::Shutdown => connection_ended(State::Closed),
        FailureReason::Lifecycle => connection_ended(State::Ready),
        reason => io::Error::other(format!(
            "{}; {reason:?} after {} recovery attempt(s)",
            connection_ended(State::Disconnected(failure.error)),
            failure.attempts
        )),
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
    output_flow: OutputSender,
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
            output_flow: OutputSender::default(),
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
                Control::ProviderOutputCapacity { through } => {
                    self.output_flow.grant(through)?;
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

/// The production sink is the supervised connection's bounded command queue.
/// The small seam lets regressions exercise cross-channel ordering without
/// credentials or a socket.
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

impl<C: Connect> InputSink for Supervisor<C> {
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
        Supervisor::retire(self, request);
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

    #[test]
    fn output_capacity_cannot_renew_authority_or_controller_heartbeat() {
        let mut intake = ProviderIntake::new(BootId::new([9; 16]).unwrap(), NOW);
        let mut budget = CONTROL_SLICE;
        let mut command = Some(Control::ProviderOutputCapacity { through: 2 });
        let error = intake
            .drain_control(
                &mut || NOW + 250_001,
                &mut || Ok(command.take()),
                &mut budget,
            )
            .unwrap_err();
        assert!(error.to_string().contains("heartbeat expired"));
        assert_eq!(intake.last_control_us, NOW);
        assert!(intake.guard.state().is_none());
        assert!(intake.stopped);
    }

    #[test]
    fn retired_output_is_removed_before_new_output_without_changing_survivor_order() {
        let mut queue = VecDeque::new();
        for sequence in 0..MAX_QUEUED_OUTPUT - 3 {
            queue.push_back(ProviderOutput::Audio {
                request: 1,
                sequence: sequence as u64 + 1,
                provider_event_at_us: NOW,
                source_frames: PACKET_SAMPLES,
                source_offset: 0,
                samples: vec![1; PACKET_SAMPLES],
            });
        }
        queue.push_back(ProviderOutput::TurnComplete {
            request: 1,
            idle: true,
        });
        queue.push_back(ProviderOutput::Started {
            request: 2,
            waiting_for_barrier: true,
        });
        queue.push_back(ProviderOutput::Transcript {
            request: None,
            text: "unattributed".into(),
            finished: true,
        });
        prune_retired_output(&mut queue, 1);
        assert_eq!(queue.len(), 2);
        assert!(matches!(
            queue.pop_front(),
            Some(ProviderOutput::Started { request: 2, .. })
        ));
        assert!(matches!(
            queue.pop_front(),
            Some(ProviderOutput::Transcript { request: None, .. })
        ));
    }

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

    // ---- Supervised provider connection over real local IPC -------------
    //
    // The service is scripted in memory; no credential, TLS or cloud request.
    // Authority uses the real monotonic clock because `serve` does.
    use lamp_gemini::{
        Credential, GOOGLE_ENDPOINT, SessionConfig,
        testing::{Dial, audio as provider_audio, connector},
    };
    use serde_json::json;
    use tokio::{sync::mpsc::UnboundedReceiver, task::JoinHandle};

    struct LiveRig {
        _directory: crate::process::SessionDirectory,
        parent: WorkerChannels,
        output_flow: crate::provider_flow::OutputReceiver,
        owner: Controller,
        snapshot: Snapshot,
        dials: UnboundedReceiver<Dial>,
        worker: JoinHandle<Result<()>>,
    }
    fn real_time() -> MonoTime {
        MonoTime::from_micros(monotonic_us())
    }
    impl LiveRig {
        fn start(policy: RecoveryPolicy) -> Self {
            let (directory, parent, worker) = intake_channels();
            let boot = BootId::new([9; 16]).unwrap();
            let mut owner = Controller::new(boot, real_time());
            owner
                .set_microphone_permission(real_time(), Permission::Allowed)
                .unwrap();
            let config =
                SessionConfig::new(GOOGLE_ENDPOINT, Credential::api_key("test-only").unwrap())
                    .unwrap()
                    .resumption(RESUMPTION);
            let (dialer, dials) = connector(config);
            let provider = Supervisor::new(dialer, policy).unwrap();
            let snapshot = owner.snapshot(real_time()).unwrap();
            let mut rig = Self {
                _directory: directory,
                parent,
                output_flow: crate::provider_flow::OutputReceiver::default(),
                owner,
                snapshot,
                dials,
                worker: tokio::spawn(serve(worker, boot, provider)),
            };
            rig.publish();
            rig
        }
        /// One coordinator heartbeat: fresh input leases and authority.
        fn publish(&mut self) {
            self.heartbeat().unwrap();
        }
        fn heartbeat(&mut self) -> io::Result<()> {
            let at = monotonic_us();
            let now = MonoTime::from_micros(at);
            let lease = MonoTime::from_micros(at + 100_000);
            self.owner
                .set_capture(now, CaptureState::RetainingUntil(lease))
                .unwrap();
            self.owner
                .set_admission(now, AdmissionState::OpenUntil(lease))
                .unwrap();
            self.snapshot = self.owner.snapshot(now).unwrap();
            self.parent.control.send(Control::Authority {
                snapshot: self.snapshot,
            })
        }
        async fn pump(&mut self, duration: Duration) {
            let until = Instant::now() + duration;
            while Instant::now() < until {
                self.publish();
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        }
        async fn output(&mut self) -> ProviderOutput {
            let deadline = Instant::now() + Duration::from_secs(3);
            loop {
                self.publish();
                if let Some(output) = self.parent.data.receive::<ProviderOutput>().unwrap() {
                    self.output_flow.received().unwrap();
                    self.output_flow
                        .replenish(0, &mut self.parent.control)
                        .unwrap();
                    return output;
                }
                assert!(!self.worker.is_finished(), "worker ended before output");
                assert!(Instant::now() < deadline, "no provider output in time");
                tokio::time::sleep(Duration::from_millis(2)).await;
            }
        }
        /// Heartbeat until the worker attempts a connection.
        async fn dial(&mut self) -> Dial {
            let deadline = Instant::now() + Duration::from_secs(3);
            loop {
                self.publish();
                if let Ok(dial) = self.dials.try_recv() {
                    return dial;
                }
                assert!(Instant::now() < deadline, "no connection attempt in time");
                tokio::time::sleep(Duration::from_millis(2)).await;
            }
        }
        /// Heartbeat until the worker ends; its result and the wait.
        async fn exit(&mut self) -> (Result<()>, Duration) {
            let started = Instant::now();
            while !self.worker.is_finished() {
                assert!(started.elapsed() < Duration::from_secs(3), "worker hung");
                // The worker may close its socket at any point from here on.
                let _ = self.heartbeat();
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
            ((&mut self.worker).await.unwrap(), started.elapsed())
        }
        /// Heartbeat until the scripted service has the worker's next message.
        async fn service_message(&mut self, dial: &mut Dial) -> serde_json::Value {
            let deadline = Instant::now() + Duration::from_secs(3);
            loop {
                self.publish();
                if let Some(message) = dial.service.try_receive(Duration::from_millis(5)).await {
                    return message;
                }
                assert!(Instant::now() < deadline, "service saw no client message");
            }
        }
        async fn ready(&mut self) -> Dial {
            let mut dial = self.dial().await;
            let setup = self.service_message(&mut dial).await;
            assert_eq!(
                setup["setup"].get("sessionResumption").is_some(),
                dial.resumed
            );
            dial.service.send(json!({"setupComplete": {}})).await;
            assert!(matches!(self.output().await, ProviderOutput::Ready { .. }));
            dial
        }
        /// Admit a request and submit its start, one block and optional end.
        async fn speak(&mut self, dial: &mut Dial, interruption: bool, end: bool) -> u64 {
            let input = if interruption {
                AdmittedInput::Interruption
            } else {
                AdmittedInput::NewTurn
            };
            let owner = self.owner.admit(real_time(), input).unwrap();
            let request = owner.turn();
            self.publish();
            let privacy_generation = self.snapshot.microphone_generation();
            self.parent
                .data
                .send(ProviderInput::Start {
                    request,
                    privacy_generation,
                })
                .unwrap();
            self.parent
                .data
                .send(ProviderInput::Audio {
                    request,
                    privacy_generation,
                    sequence: 1,
                    read_completed_at_us: monotonic_us(),
                    samples: vec![3; 160],
                })
                .unwrap();
            assert!(matches!(
                self.output().await,
                ProviderOutput::Started { request: started, .. } if started == request
            ));
            if end {
                self.owner.input_ended(real_time(), owner).unwrap();
                self.publish();
                self.parent
                    .data
                    .send(ProviderInput::End {
                        request,
                        privacy_generation,
                    })
                    .unwrap();
            }
            // The service sees exactly this request's boundaries and audio.
            let mut expected = vec!["activityStart", "audio"];
            if end {
                expected.push("activityEnd");
            }
            for field in expected {
                let message = self.service_message(dial).await;
                assert!(
                    message["realtimeInput"].get(field).is_some(),
                    "expected {field}, service received {message}"
                );
            }
            request
        }
    }
    fn quick_policy() -> RecoveryPolicy {
        RecoveryPolicy {
            first_backoff: Duration::from_millis(20),
            max_backoff: Duration::from_millis(40),
            ..RecoveryPolicy::default()
        }
    }
    fn resumable() -> serde_json::Value {
        json!({"sessionResumptionUpdate":{"newHandle":"HANDLE-ONE","resumable":true}})
    }
    fn idle_complete() -> serde_json::Value {
        json!({"serverContent":{"turnComplete":true,"interactionStatus":"IDLE"}})
    }

    #[tokio::test]
    async fn exhausted_output_credit_preserves_input_and_priority_stop() {
        let mut rig = LiveRig::start(quick_policy());
        let mut dial = rig.ready().await;
        let request = rig.speak(&mut dial, false, true).await;
        dial.service
            .send(provider_audio(5, lamp_gemini::MAX_OUTPUT_SAMPLES))
            .await;
        // Consume the two already-reserved packets without returning capacity.
        // Nothing else may be timestamped/sent while playback has no space.
        for _ in 0..2 {
            let deadline = Instant::now() + Duration::from_secs(1);
            loop {
                rig.publish();
                if let Some(output) = rig.parent.data.receive::<ProviderOutput>().unwrap() {
                    rig.output_flow.received().unwrap();
                    assert!(
                        matches!(output, ProviderOutput::Audio { request: current, .. } if current == request)
                    );
                    break;
                }
                assert!(Instant::now() < deadline);
                tokio::time::sleep(Duration::from_millis(2)).await;
            }
        }
        rig.pump(Duration::from_millis(150)).await;
        assert!(
            rig.parent
                .data
                .receive::<ProviderOutput>()
                .unwrap()
                .is_none()
        );
        assert!(!rig.worker.is_finished());

        let next = rig
            .owner
            .admit(real_time(), AdmittedInput::Interruption)
            .unwrap();
        rig.publish();
        let privacy_generation = rig.snapshot.microphone_generation();
        rig.parent
            .data
            .send(ProviderInput::Start {
                request: next.turn(),
                privacy_generation,
            })
            .unwrap();
        rig.parent
            .data
            .send(ProviderInput::Audio {
                request: next.turn(),
                privacy_generation,
                sequence: 1,
                read_completed_at_us: monotonic_us(),
                samples: vec![9; 160],
            })
            .unwrap();
        // The new microphone activity reaches the scripted service even while
        // all provider-output credits are exhausted. No old audio is relabelled.
        let start = rig.service_message(&mut dial).await;
        assert!(start["realtimeInput"].get("activityStart").is_some());
        let audio = rig.service_message(&mut dial).await;
        assert!(audio["realtimeInput"].get("audio").is_some());
        assert!(
            rig.parent
                .data
                .receive::<ProviderOutput>()
                .unwrap()
                .is_none()
        );
        rig.parent.control.send(Control::Stop).unwrap();
        let (result, elapsed) = rig.exit().await;
        assert!(result.is_ok());
        println!(
            "MEASURED stop_during_zero_output_credit_us={} (host control send to worker task end)",
            elapsed.as_micros()
        );
        assert!(elapsed < Duration::from_millis(100));
    }

    #[tokio::test]
    async fn complete_answer_then_idle_provider_loss_recovers_with_context_and_a_second_ready() {
        let mut rig = LiveRig::start(quick_policy());
        let mut dial = rig.ready().await;
        dial.service.send(resumable()).await;
        let request = rig.speak(&mut dial, false, true).await;
        // 2,000 frames arrive as one provider message; the service then closes
        // the idle session, as measured on V1 after 86-198 s without a turn.
        dial.service.send(provider_audio(5, 2_000)).await;
        dial.service
            .send(json!({"serverContent":{"generationComplete":true}}))
            .await;
        dial.service.send(idle_complete()).await;
        dial.service.close(1008, "The operation was aborted").await;
        let mut frames = 0;
        for offset in [0, 960, 1_920] {
            let ProviderOutput::Audio {
                request: owner,
                source_frames,
                source_offset,
                samples,
                ..
            } = rig.output().await
            else {
                panic!("expected provider audio");
            };
            assert_eq!(
                (owner, source_frames, source_offset),
                (request, 2_000, offset)
            );
            assert!(samples.iter().all(|sample| *sample == 5));
            frames += samples.len();
        }
        assert_eq!(frames, 2_000);
        assert!(matches!(
            rig.output().await,
            ProviderOutput::GenerationComplete { request: done } if done == request
        ));
        assert!(matches!(
            rig.output().await,
            ProviderOutput::TurnComplete { request: done, idle: true } if done == request
        ));
        // The coordinator finishes the turn; the worker reconnects on its own.
        let owner = rig.snapshot.owner().unwrap();
        rig.owner.complete_turn(real_time(), owner).unwrap();
        let mut redial = rig.ready().await;
        assert!(redial.resumed);
        assert_eq!(redial.session.get(), 2);
        // The next question is served by the resumed session.
        let next = rig.speak(&mut redial, false, true).await;
        redial.service.send(provider_audio(6, 240)).await;
        assert!(matches!(
            rig.output().await,
            ProviderOutput::Audio { request: owner, .. } if owner == next
        ));
        rig.parent.control.send(Control::Stop).unwrap();
        assert!(rig.exit().await.0.is_ok());
    }

    #[tokio::test]
    async fn answer_generated_before_the_service_closed_is_settled_not_failed() {
        let mut rig = LiveRig::start(quick_policy());
        let mut dial = rig.ready().await;
        dial.service.send(resumable()).await;
        let request = rig.speak(&mut dial, false, true).await;
        dial.service.send(provider_audio(5, 480)).await;
        dial.service
            .send(json!({"serverContent":{"generationComplete":true}}))
            .await;
        dial.service.close(1008, "idle").await;
        assert!(matches!(rig.output().await, ProviderOutput::Audio { .. }));
        assert!(matches!(
            rig.output().await,
            ProviderOutput::GenerationComplete { .. }
        ));
        // No idle completion ever came from the service. The closed connection
        // is the barrier, so the fully delivered answer can finish normally.
        assert!(matches!(
            rig.output().await,
            ProviderOutput::TurnComplete { request: done, idle: true } if done == request
        ));
        rig.ready().await;
    }

    #[tokio::test]
    async fn provider_loss_before_an_answer_completes_ends_the_worker_naming_request_and_stage() {
        for (stage, expected) in [(0, "InputOpen"), (1, "AwaitingResponse"), (2, "Responding")] {
            let mut rig = LiveRig::start(quick_policy());
            let mut dial = rig.ready().await;
            dial.service.send(resumable()).await;
            let request = rig.speak(&mut dial, false, stage > 0).await;
            if stage == 2 {
                dial.service.send(provider_audio(5, 480)).await;
                assert!(matches!(rig.output().await, ProviderOutput::Audio { .. }));
            }
            dial.service.close(1011, "PRIVATE BACKEND DETAIL").await;
            let (result, _) = rig.exit().await;
            let message = result.unwrap_err().to_string();
            assert!(
                message.contains("PeerClosed { code: Some(1011) }"),
                "{message}"
            );
            assert!(
                message.contains(&format!("request {request} was lost at {expected}")),
                "{message}"
            );
            assert!(!message.contains("PRIVATE"));
            // Nothing was replayed anywhere: no further connection was dialed.
            assert!(rig.dials.try_recv().is_err());
        }
    }

    #[tokio::test]
    async fn stop_and_heartbeat_are_serviced_while_a_reconnect_hangs() {
        let mut rig = LiveRig::start(RecoveryPolicy {
            outage_budget: Duration::from_secs(5),
            ..quick_policy()
        });
        let mut dial = rig.ready().await;
        dial.service.send(resumable()).await;
        dial.service.close(1008, "idle").await;
        // The service never answers the reconnect. For longer than the 250 ms
        // controller heartbeat bound the worker must keep draining control.
        let _hung = rig.dial().await;
        rig.pump(Duration::from_millis(400)).await;
        assert!(!rig.worker.is_finished());
        assert!(
            rig.parent
                .data
                .receive::<ProviderOutput>()
                .unwrap()
                .is_none()
        );
        rig.parent.control.send(Control::Stop).unwrap();
        let (result, waited) = rig.exit().await;
        assert!(result.is_ok());
        println!(
            "MEASURED stop_during_hung_reconnect_us={} (local socket send to worker task end, host)",
            waited.as_micros()
        );
        assert!(waited < Duration::from_millis(100));
    }

    #[tokio::test]
    async fn input_admitted_during_an_outage_ends_the_worker_instead_of_being_held() {
        let mut rig = LiveRig::start(RecoveryPolicy {
            outage_budget: Duration::from_secs(5),
            ..quick_policy()
        });
        let mut dial = rig.ready().await;
        dial.service.send(resumable()).await;
        dial.service.close(1008, "idle").await;
        let _hung = rig.dial().await;
        let owner = rig
            .owner
            .admit(real_time(), AdmittedInput::NewTurn)
            .unwrap();
        rig.publish();
        rig.parent
            .data
            .send(ProviderInput::Start {
                request: owner.turn(),
                privacy_generation: rig.snapshot.microphone_generation(),
            })
            .unwrap();
        let message = rig.exit().await.0.unwrap_err().to_string();
        assert!(
            message.contains("provider unavailable while input was admitted"),
            "{message}"
        );
    }

    #[tokio::test]
    async fn exhausted_recovery_and_quota_end_with_fixed_categories() {
        let mut rig = LiveRig::start(RecoveryPolicy {
            max_attempts: 2,
            ..quick_policy()
        });
        let mut dial = rig.ready().await;
        dial.service.send(resumable()).await;
        dial.service.close(1008, "idle").await;
        drop(rig.dial().await);
        drop(rig.dial().await);
        let message = rig.exit().await.0.unwrap_err().to_string();
        assert!(
            message.contains("AttemptsExhausted after 2 recovery attempt(s)"),
            "{message}"
        );

        let mut rig = LiveRig::start(quick_policy());
        let mut dial = rig.ready().await;
        dial.service.send(resumable()).await;
        dial.service
            .close(1011, "You exceeded your current quota PRIVATE")
            .await;
        let message = rig.exit().await.0.unwrap_err().to_string();
        assert_eq!(
            message,
            "provider connection ended: Gemini Live QuotaExceeded; NotRecoverable after 0 recovery attempt(s)"
        );

        // No handle was ever issued: the conversation could not be restored,
        // so the loss is reported rather than hidden behind a fresh session.
        let mut rig = LiveRig::start(quick_policy());
        let mut dial = rig.ready().await;
        dial.service.close(1008, "idle").await;
        let message = rig.exit().await.0.unwrap_err().to_string();
        assert!(message.contains("NoResumableContext"), "{message}");
    }

    #[tokio::test]
    async fn fast_provider_burst_is_held_by_flow_control_and_arrives_complete_in_order() {
        let mut rig = LiveRig::start(quick_policy());
        let mut dial = rig.ready().await;
        let request = rig.speak(&mut dial, false, true).await;
        // 80 s of speech offered at once: 40 provider messages of 2 s each,
        // 2,000 IPC packets. The coordinator now grants at most two packets
        // in flight, so its backlog would pass the 512-packet bound that used to
        // end it. The IPC freshness rule means the coordinator side must keep
        // reading; it does, as fast as packets arrive.
        const MESSAGES: usize = 40;
        const FRAMES: usize = lamp_gemini::MAX_OUTPUT_SAMPLES;
        let mut service = dial.service;
        let script = tokio::spawn(async move {
            service
                .burst((0..MESSAGES).map(|index| provider_audio(index as i16, FRAMES)))
                .await;
            service.send(idle_complete()).await;
            service
        });
        let started = Instant::now();
        let (mut packets, mut last_sequence, mut frames) = (0, 0, 0usize);
        loop {
            match rig.output().await {
                ProviderOutput::Audio {
                    request: owner,
                    sequence,
                    source_offset,
                    samples,
                    ..
                } => {
                    if packets == 0 {
                        // Backpressure reached the service: with most of the
                        // burst unread it is still blocked on its socket.
                        assert!(!script.is_finished());
                    }
                    assert_eq!(owner, request);
                    assert!(sequence > last_sequence);
                    last_sequence = sequence;
                    // Every block carries its message index: none is lost,
                    // duplicated or reordered.
                    let expected = (frames / FRAMES) as i16;
                    assert_eq!(source_offset, frames % FRAMES);
                    assert!(samples.iter().all(|sample| *sample == expected));
                    frames += samples.len();
                    packets += 1;
                }
                ProviderOutput::TurnComplete { idle: true, .. } => break,
                other => panic!("unexpected provider output: {other:?}"),
            }
        }
        assert_eq!((packets, frames), (MESSAGES * 50, MESSAGES * FRAMES));
        println!(
            "MEASURED burst_80s_audio_2000_packets_delivered_ms={} (scripted service to local IPC reader, host)",
            started.elapsed().as_millis()
        );
        let _service = script.await.unwrap();
        rig.parent.control.send(Control::Stop).unwrap();
        assert!(rig.exit().await.0.is_ok());
    }

    #[tokio::test]
    async fn late_provider_events_never_reach_the_coordinator_for_a_newer_request() {
        let mut rig = LiveRig::start(quick_policy());
        let mut dial = rig.ready().await;
        let first = rig.speak(&mut dial, false, true).await;
        dial.service.send(provider_audio(5, 240)).await;
        dial.service.send(idle_complete()).await;
        assert!(matches!(rig.output().await, ProviderOutput::Audio { .. }));
        assert!(matches!(
            rig.output().await,
            ProviderOutput::TurnComplete { request, idle: true } if request == first
        ));
        let owner = rig.snapshot.owner().unwrap();
        rig.owner.complete_turn(real_time(), owner).unwrap();
        // The person starts a follow-up. The interruption that its explicit
        // activityStart requested, and a duplicate completion, arrive late.
        let second = rig.speak(&mut dial, false, false).await;
        dial.service
            .send(json!({"serverContent":{"interrupted":true}}))
            .await;
        dial.service.send(idle_complete()).await;
        dial.service.send(provider_audio(66, 240)).await;
        rig.pump(Duration::from_millis(120)).await;
        assert!(
            rig.parent
                .data
                .receive::<ProviderOutput>()
                .unwrap()
                .is_none()
        );
        // The follow-up ends and is answered under its own identity only.
        let owner = rig.snapshot.owner().unwrap();
        rig.owner.input_ended(real_time(), owner).unwrap();
        rig.publish();
        rig.parent
            .data
            .send(ProviderInput::End {
                request: second,
                privacy_generation: rig.snapshot.microphone_generation(),
            })
            .unwrap();
        rig.pump(Duration::from_millis(40)).await;
        dial.service.send(provider_audio(7, 240)).await;
        let ProviderOutput::Audio {
            request, samples, ..
        } = rig.output().await
        else {
            panic!("expected the follow-up answer");
        };
        assert_eq!(request, second);
        assert!(samples.iter().all(|sample| *sample == 7));
    }
}
