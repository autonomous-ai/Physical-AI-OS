use crate::{
    Discard, EVENT_QUEUE_CAPACITY, Error, Event, INPUT_QUEUE_CAPACITY, Lineage, MAX_INPUT_AGE,
    MAX_INPUT_SAMPLES, MAX_WIRE_BYTES, OUTBOX_CAPACITY, OUTPUT_RATE, RequestId, Result, Resumption,
    ResumptionHandle, SessionConfig, SessionId, State, Timeouts, UnansweredInterruption, wire,
};
use futures_util::{SinkExt, StreamExt};
use std::{
    collections::VecDeque,
    sync::{
        Arc, Mutex, PoisonError,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    sync::{Notify, mpsc, watch},
    task::JoinHandle,
    time::{MissedTickBehavior, timeout},
};
use tokio_tungstenite::{
    WebSocketStream, connect_async_with_config,
    tungstenite::{self, Message, protocol::WebSocketConfig},
};

struct Shared {
    retired_through: AtomicU64,
    submitted_through: AtomicU64,
    wake: Notify,
    stop: watch::Sender<Option<Error>>,
    resumption: Mutex<Option<ResumptionPoint>>,
}

/// The latest point from which the service said this session can be resumed.
#[derive(Clone, Debug)]
pub struct ResumptionPoint {
    handle: ResumptionHandle,
    current: bool,
}
impl ResumptionPoint {
    pub fn handle(&self) -> &ResumptionHandle {
        &self.handle
    }
    /// False once the service has since reported a state it cannot resume
    /// from. Resuming then restores the conversation only up to this point.
    pub fn is_current(&self) -> bool {
        self.current
    }
}
impl Shared {
    fn fail(&self, error: Error) {
        self.stop.send_if_modified(|value| {
            if value.is_none() {
                *value = Some(error);
                true
            } else {
                false
            }
        });
        self.wake.notify_one();
    }
}

#[derive(Clone)]
pub struct InputSender {
    commands: mpsc::Sender<Command>,
    shared: Arc<Shared>,
    state: watch::Receiver<State>,
}
impl InputSender {
    /// Queue a new admitted request. This synchronously retires earlier output
    /// from this handle; root must ALSO revoke/flush already delivered playback.
    pub fn try_start(&self, request: RequestId) -> Result<()> {
        self.ensure_ready()?;
        if request.get() <= self.shared.retired_through.load(Ordering::Acquire)
            || self
                .shared
                .submitted_through
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |old| {
                    (request.get() > old).then_some(request.get())
                })
                .is_err()
        {
            return Err(Error::StaleRequest);
        }
        self.shared
            .retired_through
            .fetch_max(request.get() - 1, Ordering::AcqRel);
        self.shared.wake.notify_one();
        self.enqueue(Command::Start(request))
    }
    /// `captured_at` is acquisition time in this process's Instant domain, not
    /// queue receipt time. A process wrapper must preserve IPC acquisition age
    /// when translating its shared OS monotonic timestamps into this domain.
    pub fn try_audio(
        &self,
        request: RequestId,
        sequence: u64,
        captured_at: Instant,
        samples: &[i16],
    ) -> Result<()> {
        self.ensure_ready()?;
        if request.get() <= self.shared.retired_through.load(Ordering::Acquire) {
            return Err(Error::StaleRequest);
        }
        check_age(captured_at)?;
        if samples.is_empty() || samples.len() > MAX_INPUT_SAMPLES {
            return Err(Error::InvalidAudio);
        }
        self.enqueue(Command::Audio(Audio {
            request,
            sequence,
            captured_at,
            samples: samples.to_vec(),
        }))
    }
    pub fn try_end(&self, request: RequestId) -> Result<()> {
        self.enqueue(Command::End(request))
    }
    /// Permanent local retirement is synchronous and cannot be blocked by a full
    /// audio queue. Network interruption follows asynchronously. This does not
    /// revoke permits or flush samples already handed to an external speaker.
    pub fn retire(&self, request: RequestId) {
        self.shared
            .retired_through
            .fetch_max(request.get(), Ordering::AcqRel);
        self.shared.wake.notify_one();
    }
    pub fn shutdown(&self) {
        self.shared.fail(Error::Closed);
    }
    fn ensure_ready(&self) -> Result<()> {
        if self.shared.stop.borrow().is_some() || *self.state.borrow() != State::Ready {
            return Err(Error::Closed);
        }
        Ok(())
    }
    fn enqueue(&self, command: Command) -> Result<()> {
        self.ensure_ready()?;
        self.commands
            .try_send(command)
            .map_err(|error| match error {
                mpsc::error::TrySendError::Full(_) => {
                    self.shared.fail(Error::Backpressure);
                    Error::Backpressure
                }
                mpsc::error::TrySendError::Closed(_) => Error::Closed,
            })
    }
}

pub struct Connection {
    input: InputSender,
    events: mpsc::Receiver<Event>,
    state: watch::Receiver<State>,
    task: JoinHandle<()>,
}
impl Connection {
    pub fn input(&self) -> InputSender {
        self.input.clone()
    }
    pub fn state(&self) -> State {
        match *self.input.shared.stop.borrow() {
            Some(Error::Closed) => State::Closed,
            Some(error) => State::Disconnected(error),
            None => *self.state.borrow(),
        }
    }
    pub fn subscribe_state(&self) -> watch::Receiver<State> {
        self.state.clone()
    }
    pub fn shutdown(&self) {
        self.input.shutdown();
    }
    /// Latest retained resumption point, if the configuration allows retention
    /// and the service has issued one. Remains readable after the connection ends.
    pub fn resumption(&self) -> Option<ResumptionPoint> {
        self.input
            .shared
            .resumption
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }
    /// Cancellation-safe receiver. Already queued retired audio/text is dropped,
    /// while lifecycle events retain their original lineage for bookkeeping.
    /// Output received before a remote or transport failure is still delivered,
    /// in order, ahead of the end of the stream: a failure must not truncate an
    /// answer that had already arrived. A local shutdown drops queued output.
    pub async fn next_event(&mut self) -> Option<Event> {
        while let Some(event) = self.events.recv().await {
            let output_request = match &event {
                Event::Audio { lineage, .. }
                | Event::OutputTranscript { lineage, .. }
                | Event::ModelText { lineage, .. } => Some(lineage.request.get()),
                _ => None,
            };
            if output_request.is_some_and(|id| {
                *self.input.shared.stop.borrow() == Some(Error::Closed)
                    || id <= self.input.shared.retired_through.load(Ordering::Acquire)
            }) {
                continue;
            }
            return Some(event);
        }
        None
    }
}
impl Drop for Connection {
    fn drop(&mut self) {
        self.input.shutdown();
        self.task.abort();
    }
}

/// Establish TLS, send setup, and wait for setupComplete before returning Ready.
/// No implicit reconnect, resumption, tools, or hidden agent task.
pub async fn connect(config: SessionConfig, session: SessionId) -> Result<Connection> {
    config.timeouts.validate()?;
    let request = config.request()?;
    let (socket, _) = timeout(
        config.timeouts.connect,
        connect_async_with_config(request, Some(socket_config()), true),
    )
    .await
    .map_err(|_| Error::ConnectTimeout)?
    .map_err(transport_error)?;
    setup_and_spawn(socket, config, session).await
}

pub(crate) fn socket_config() -> WebSocketConfig {
    WebSocketConfig::default()
        .read_buffer_size(16_384)
        .write_buffer_size(0)
        .max_write_buffer_size(MAX_WIRE_BYTES + 4096)
        .max_message_size(Some(MAX_WIRE_BYTES))
        .max_frame_size(Some(MAX_WIRE_BYTES))
}

pub(crate) async fn setup_and_spawn<S>(
    mut socket: WebSocketStream<S>,
    config: SessionConfig,
    session: SessionId,
) -> Result<Connection>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    send(
        &mut socket,
        Message::text(wire::encode_setup(&config)?),
        config.timeouts.write,
    )
    .await?;
    let setup = async {
        loop {
            let message = socket
                .next()
                .await
                .ok_or(Error::PeerClosed { code: None })?
                .map_err(transport_error)?;
            match message {
                Message::Text(text) => {
                    let frame = wire::decode_server(text.as_bytes())?;
                    if frame.setup_complete {
                        return Ok(());
                    }
                    if frame.content.is_some()
                        || frame.go_away.is_some()
                        || frame.voice_activity.is_some()
                    {
                        return Err(Error::UnexpectedResponse);
                    }
                }
                Message::Binary(bytes) => {
                    let frame = wire::decode_server(&bytes)?;
                    if frame.setup_complete {
                        return Ok(());
                    }
                    if frame.content.is_some()
                        || frame.go_away.is_some()
                        || frame.voice_activity.is_some()
                    {
                        return Err(Error::UnexpectedResponse);
                    }
                }
                Message::Ping(bytes) => {
                    send(&mut socket, Message::Pong(bytes), config.timeouts.write).await?
                }
                Message::Pong(_) => {}
                Message::Close(frame) => return Err(closed(frame)),
                Message::Frame(_) => return Err(Error::MalformedMessage),
            }
        }
    };
    timeout(config.timeouts.setup, setup)
        .await
        .map_err(|_| Error::SetupTimeout)??;
    let (commands_tx, commands) = mpsc::channel(INPUT_QUEUE_CAPACITY);
    let (events_tx, events) = mpsc::channel(EVENT_QUEUE_CAPACITY);
    let (state_tx, state) = watch::channel(State::Ready);
    let (stop, stop_rx) = watch::channel(None);
    let shared = Arc::new(Shared {
        retired_through: AtomicU64::new(0),
        submitted_through: AtomicU64::new(0),
        wake: Notify::new(),
        stop,
        resumption: Mutex::new(None),
    });
    let input = InputSender {
        commands: commands_tx,
        shared: shared.clone(),
        state: state.clone(),
    };
    let actor = Actor {
        socket,
        session,
        timeouts: config.timeouts,
        retain: config.resumption != Resumption::Off,
        unanswered: config.unanswered,
        commands,
        events: events_tx,
        outbox: VecDeque::with_capacity(OUTBOX_CAPACITY),
        outbox_since: None,
        shared,
        stop: stop_rx,
        current: None,
        successor: None,
        last_responder: None,
        stale_terminal: None,
        go_away: false,
        activity_open: false,
        last_request: 0,
        read_at: Instant::now(),
        ping_at: Instant::now(),
    };
    // Actor::run owns and drops its event sender before returning. Keep one
    // sender until the final state is published so an EOF-first receiver cannot
    // observe Ready and lose the sanitized failure on a multithreaded runtime.
    let event_close_guard = actor.events.clone();
    let task = tokio::spawn(async move {
        let result = actor.run().await;
        state_tx.send_replace(match result {
            Ok(()) | Err(Error::Closed) => State::Closed,
            Err(error) => State::Disconnected(error),
        });
        drop(event_close_guard);
    });
    Ok(Connection {
        input,
        events,
        state,
        task,
    })
}

struct Audio {
    request: RequestId,
    sequence: u64,
    captured_at: Instant,
    samples: Vec<i16>,
}
enum Command {
    Start(RequestId),
    Audio(Audio),
    End(RequestId),
}
struct InputState {
    open: bool,
    last_sequence: Option<u64>,
    last_capture: Option<Instant>,
}
impl InputState {
    fn new() -> Self {
        Self {
            open: true,
            last_sequence: None,
            last_capture: None,
        }
    }
    fn accept(&mut self, audio: &Audio) -> Result<()> {
        check_age(audio.captured_at)?;
        if !self.open
            || self
                .last_sequence
                .is_some_and(|last| last.checked_add(1) != Some(audio.sequence))
            || self
                .last_capture
                .is_some_and(|last| audio.captured_at < last)
        {
            return Err(Error::InputSequence);
        }
        self.last_sequence = Some(audio.sequence);
        self.last_capture = Some(audio.captured_at);
        Ok(())
    }
}
struct Turn {
    lineage: Lineage,
    input: InputState,
    /// Superseded or cancelled locally. Its output is never delivered.
    retired: bool,
    /// When this client asked the service to interrupt the retired response.
    retire_at: Option<Instant>,
    saw_interrupt: bool,
    /// activityEnd sent: the service now owes a response or a terminal event.
    ended_at: Option<Instant>,
    first_output_at: Option<Instant>,
    progress_at: Option<Instant>,
    generation_at: Option<Instant>,
    output_samples: u64,
    audio_sequence: u64,
}
impl Turn {
    fn new(lineage: Lineage, input: InputState) -> Self {
        Self {
            lineage,
            input,
            retired: false,
            retire_at: None,
            saw_interrupt: false,
            ended_at: None,
            first_output_at: None,
            progress_at: None,
            generation_at: None,
            output_samples: 0,
            audio_sequence: 0,
        }
    }
}
/// The request admitted while an earlier response still owns the output
/// stream. Its audio goes to the service at once: the service cannot answer an
/// activity before its activityEnd, so only that End waits for the barrier.
struct Successor {
    request: RequestId,
    input: InputState,
}
struct Actor<S> {
    socket: WebSocketStream<S>,
    session: SessionId,
    timeouts: Timeouts,
    retain: bool,
    unanswered: UnansweredInterruption,
    commands: mpsc::Receiver<Command>,
    events: mpsc::Sender<Event>,
    /// Events decoded but not yet accepted by the bounded event queue.
    outbox: VecDeque<Event>,
    outbox_since: Option<Instant>,
    shared: Arc<Shared>,
    stop: watch::Receiver<Option<Error>>,
    current: Option<Turn>,
    successor: Option<Successor>,
    /// Most recent request that produced output: the only possible owner of an
    /// output transcript that trails its audio.
    last_responder: Option<Lineage>,
    /// When a stray `interrupted` was dropped whose companion completion has
    /// not been seen yet. Only honored within `Timeouts::barrier` of it.
    stale_terminal: Option<Instant>,
    go_away: bool,
    activity_open: bool,
    last_request: u64,
    read_at: Instant,
    ping_at: Instant,
}
impl<S: AsyncRead + AsyncWrite + Unpin> Actor<S> {
    async fn run(mut self) -> Result<()> {
        let result = self.serve().await;
        // Hand over as much already decoded output as fits, in order, before
        // the stream ends. Nothing is skipped: the first refusal stops it.
        self.flush();
        result
    }
    async fn serve(&mut self) -> Result<()> {
        let mut tick = tokio::time::interval(Duration::from_millis(10));
        tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
        loop {
            if let Some(error) = *self.stop.borrow() {
                return Err(error);
            }
            self.retire_active().await?;
            self.enforce_barrier().await?;
            self.flush();
            self.check_deadlines()?;
            if self.go_away
                && self.current.is_none()
                && self.successor.is_none()
                && self.outbox.is_empty()
            {
                // Nothing is owed on this connection: end it at a clean
                // boundary instead of waiting for the announced close.
                return Err(Error::ServerGoAway);
            }
            if self.ping_at.elapsed() >= self.timeouts.keepalive {
                send(
                    &mut self.socket,
                    Message::Ping(Vec::new().into()),
                    self.timeouts.write,
                )
                .await?;
                self.ping_at = Instant::now();
            }
            // While the consumer is behind, stop reading the socket and let
            // transport flow control hold the service back. Commands, local
            // retirement and shutdown stay live; only cloud output waits.
            let gated = !self.outbox.is_empty();
            tokio::select! {
                _ = self.shared.wake.notified() => {},
                _ = tick.tick() => {},
                // This actor is the only sender, so the released slot is still
                // free when the next iteration flushes the outbox into it.
                ready = capacity(&self.events), if gated => ready?,
                command = self.commands.recv() => match command {
                    Some(command) => self.command(command).await?,
                    None => return Ok(()),
                },
                incoming = self.socket.next(), if !gated => {
                    let incoming = incoming
                        .ok_or(Error::PeerClosed { code: None })?
                        .map_err(transport_error)?;
                    self.read_at = Instant::now();
                    self.message(incoming).await?;
                    // One server message per scheduling turn: a burst already in
                    // the socket buffer cannot starve the caller's control tick.
                    tokio::task::yield_now().await;
                }
            }
        }
    }
    fn check_deadlines(&self) -> Result<()> {
        if let Some(since) = self.outbox_since {
            if since.elapsed() >= self.timeouts.deliver {
                return Err(Error::Backpressure);
            }
            // The socket is deliberately unread, so neither liveness nor
            // response progress can be judged until delivery resumes.
            return Ok(());
        }
        if self.read_at.elapsed() >= self.timeouts.read {
            return Err(Error::ReadTimeout);
        }
        let Some(turn) = self.current.as_ref().filter(|turn| !turn.retired) else {
            return Ok(());
        };
        let Some(ended) = turn.ended_at else {
            return Ok(());
        };
        if ended.elapsed() >= self.timeouts.turn {
            return Err(Error::ResponseTimeout);
        }
        match (turn.first_output_at, turn.generation_at) {
            (None, _) => {
                if ended.elapsed() >= self.timeouts.response {
                    return Err(Error::ResponseTimeout);
                }
            }
            (Some(_), None) => {
                if turn
                    .progress_at
                    .is_some_and(|at| at.elapsed() >= self.timeouts.stall)
                {
                    return Err(Error::StalledResponse);
                }
            }
            (Some(first), Some(generated)) => {
                // The service withholds the idle completion while it assumes the
                // generated audio is still playing in real time.
                let playback = Duration::from_micros(
                    turn.output_samples.saturating_mul(1_000_000) / u64::from(OUTPUT_RATE),
                );
                let due = (first + playback).max(generated) + self.timeouts.completion;
                if Instant::now() >= due {
                    return Err(Error::CompletionTimeout);
                }
            }
        }
        Ok(())
    }
    async fn start_activity(&mut self) -> Result<()> {
        if !self.activity_open {
            send(
                &mut self.socket,
                Message::text(wire::ACTIVITY_START),
                self.timeouts.write,
            )
            .await?;
            self.activity_open = true;
        }
        Ok(())
    }
    async fn end_activity(&mut self) -> Result<()> {
        send(
            &mut self.socket,
            Message::text(wire::ACTIVITY_END),
            self.timeouts.write,
        )
        .await?;
        self.activity_open = false;
        Ok(())
    }
    fn retired_through(&self) -> u64 {
        self.shared.retired_through.load(Ordering::Acquire)
    }
    async fn retire_active(&mut self) -> Result<()> {
        let retired_through = self.retired_through();
        let Some(turn) = self.current.as_mut() else {
            return Ok(());
        };
        if turn.retired || turn.lineage.request.get() > retired_through {
            return Ok(());
        }
        turn.retired = true;
        if turn.input.open {
            // No activityEnd was sent, so the service owes this request
            // nothing. Its End drops it without asking for a response.
            return Ok(());
        }
        turn.retire_at = Some(Instant::now());
        // Explicit activityStart is the only interruption request available
        // with provider activity detection disabled.
        self.start_activity().await
    }
    async fn enforce_barrier(&mut self) -> Result<()> {
        let Some(turn) = self.current.as_ref() else {
            return Ok(());
        };
        if turn
            .retire_at
            .is_none_or(|at| at.elapsed() < self.timeouts.barrier)
        {
            return Ok(());
        }
        if self.unanswered == UnansweredInterruption::AssumeCancelled
            && turn.first_output_at.is_none()
            && !turn.saw_interrupt
        {
            self.current = None;
            self.emit(Event::Discarded {
                session: self.session,
                reason: Discard::UnansweredBarrier,
            })?;
            return self.promote().await;
        }
        Err(Error::BarrierTimeout)
    }
    async fn command(&mut self, command: Command) -> Result<()> {
        match command {
            Command::Start(request) => {
                if request.get() <= self.last_request
                    || self.current.as_ref().is_some_and(|turn| turn.input.open)
                    || self
                        .successor
                        .as_ref()
                        .is_some_and(|successor| successor.input.open)
                {
                    return Err(Error::OverlappingInput);
                }
                self.last_request = request.get();
                let lineage = Lineage {
                    session: self.session,
                    request,
                };
                let waiting_for_barrier = self.current.is_some();
                if waiting_for_barrier {
                    self.retire_active().await?;
                    // An earlier successor whose End is still held was never
                    // committed. Its audio stays in the activity this request
                    // continues; it cannot receive a response of its own.
                    self.successor = Some(Successor {
                        request,
                        input: InputState::new(),
                    });
                } else {
                    self.current = Some(Turn::new(lineage, InputState::new()));
                }
                self.start_activity().await?;
                self.emit(Event::InputStarted {
                    lineage,
                    waiting_for_barrier,
                })?;
            }
            Command::Audio(audio) => {
                if audio.request.get() <= self.retired_through() {
                    return Ok(());
                }
                let input = match (self.successor.as_mut(), self.current.as_mut()) {
                    (Some(successor), _) if successor.request == audio.request => {
                        &mut successor.input
                    }
                    (None, Some(turn)) if turn.lineage.request == audio.request => &mut turn.input,
                    _ => return Err(Error::StaleRequest),
                };
                input.accept(&audio)?;
                send_audio(&mut self.socket, &audio, self.timeouts.write).await?;
            }
            Command::End(request) => {
                if let Some(successor) = self.successor.as_mut() {
                    if successor.request != request || !successor.input.open {
                        return Err(Error::StaleRequest);
                    }
                    // Held until the superseded response reaches its idle
                    // barrier: output after this End belongs to this request.
                    successor.input.open = false;
                    return Ok(());
                }
                let retired_through = self.retired_through();
                let turn = self.current.as_mut().ok_or(Error::StaleRequest)?;
                if turn.lineage.request != request || !turn.input.open {
                    return Err(Error::StaleRequest);
                }
                turn.input.open = false;
                if turn.retired || request.get() <= retired_through {
                    // Cancelled before commit. Sending activityEnd would ask
                    // for a response nobody owns; leave the activity open for
                    // the next admitted request to continue.
                    self.current = None;
                    return Ok(());
                }
                turn.ended_at = Some(Instant::now());
                self.end_activity().await?;
            }
        }
        Ok(())
    }
    fn decode(&self, bytes: &[u8]) -> Result<wire::ServerFrame> {
        if self.retain {
            wire::decode_server_retaining(bytes)
        } else {
            wire::decode_server(bytes)
        }
    }
    async fn message(&mut self, message: Message) -> Result<()> {
        let frame = match message {
            Message::Text(text) => self.decode(text.as_bytes())?,
            Message::Binary(bytes) => self.decode(&bytes)?,
            Message::Ping(bytes) => {
                send(&mut self.socket, Message::Pong(bytes), self.timeouts.write).await?;
                return Ok(());
            }
            Message::Pong(_) => return Ok(()),
            Message::Close(frame) => return Err(closed(frame)),
            Message::Frame(_) => return Err(Error::MalformedMessage),
        };
        if frame.setup_complete {
            return Err(Error::UnexpectedResponse);
        }
        if let Some(update) = frame.resumption {
            let mut point = self
                .shared
                .resumption
                .lock()
                .unwrap_or_else(PoisonError::into_inner);
            match update.handle {
                Some(handle) => {
                    *point = Some(ResumptionPoint {
                        handle,
                        current: true,
                    });
                }
                // The previous handle still resumes, but only up to its own point.
                None => {
                    if let Some(point) = point.as_mut() {
                        point.current = false;
                    }
                }
            }
        }
        if let Some(notice) = frame.go_away
            && !self.go_away
        {
            self.go_away = true;
            self.emit(Event::GoAway {
                session: self.session,
                time_left: notice.time_left,
            })?;
        }
        if let Some(activity) = frame.voice_activity {
            self.emit(Event::VoiceActivity {
                session: self.session,
                kind: activity.kind,
                audio_offset: activity.audio_offset,
            })?;
        }
        if let Some(content) = frame.content {
            self.content(content).await?;
        }
        Ok(())
    }
    fn discard(&mut self, reason: Discard) -> Result<()> {
        self.emit(Event::Discarded {
            session: self.session,
            reason,
        })
    }
    /// The service gives output transcripts no ordering relative to audio or
    /// completion. One that arrives before the current request has produced
    /// output cannot describe it, so it stays with the previous responder.
    fn output_transcript(&mut self, transcript: wire::Transcript) -> Result<()> {
        let owner = match &self.current {
            Some(turn)
                if !turn.input.open
                    && (turn.first_output_at.is_some() || self.last_responder.is_none()) =>
            {
                Some(turn.lineage)
            }
            _ => self.last_responder,
        };
        match owner {
            Some(lineage) => self.emit(Event::OutputTranscript {
                lineage,
                text: transcript.text,
                finished: transcript.finished,
            }),
            None => self.discard(Discard::UnownedOutput),
        }
    }
    async fn content(&mut self, mut content: wire::ServerContent) -> Result<()> {
        if let Some(transcript) = content.input_transcript.take() {
            self.emit(Event::UncorrelatedInputTranscript {
                session: self.session,
                text: transcript.text,
                finished: transcript.finished,
            })?;
        }
        let transcript = content.output_transcript.take();
        let payload = !content.audio.is_empty() || !content.text.is_empty();
        let terminal = content.interrupted || content.turn_complete;
        if payload || terminal || content.generation_complete {
            // Apply any local retirement first so ownership below is current.
            self.retire_active().await?;
            self.owned(content, payload).await?;
        }
        if let Some(transcript) = transcript {
            self.output_transcript(transcript)?;
        }
        Ok(())
    }
    async fn owned(&mut self, content: wire::ServerContent, payload: bool) -> Result<()> {
        let stale = if content.interrupted {
            Discard::LateInterruption
        } else if payload {
            Discard::LateOutput
        } else {
            Discard::LateTerminal
        };
        let Some(turn) = self.current.as_mut() else {
            return self.discard(if content.interrupted {
                Discard::LateInterruption
            } else {
                Discard::UnownedOutput
            });
        };
        let lineage = turn.lineage;
        let now = Instant::now();
        if turn.input.open {
            // No activityEnd has been sent for this request, so the service
            // cannot be answering it. Everything here belongs to the past.
            if content.interrupted && !content.turn_complete {
                self.stale_terminal = Some(now);
            }
            return self.discard(stale);
        }
        if turn.retired {
            if payload {
                turn.first_output_at.get_or_insert(now);
            }
            if content.interrupted {
                turn.saw_interrupt = true;
                self.emit(Event::Interrupted { lineage })?;
            }
            if content.turn_complete {
                self.emit(Event::TurnComplete {
                    lineage,
                    idle: content.idle,
                })?;
                if content.idle {
                    self.current = None;
                    self.promote().await?;
                }
            }
            return Ok(());
        }
        if content.interrupted {
            // This client has not asked to interrupt this request, and nothing
            // else can with provider activity detection disabled. The event is
            // the delayed result of an earlier activityStart; it must neither
            // cancel this request nor confirm that anyone spoke.
            if !content.turn_complete {
                self.stale_terminal = Some(now);
            }
            return self.discard(Discard::LateInterruption);
        }
        if payload || content.generation_complete {
            self.stale_terminal = None;
            turn.first_output_at.get_or_insert(now);
            turn.progress_at = Some(now);
            self.last_responder = Some(lineage);
        }
        let sequence = turn.audio_sequence;
        if !content.audio.is_empty() {
            turn.audio_sequence = sequence.checked_add(1).ok_or(Error::UnexpectedResponse)?;
            turn.output_samples = turn
                .output_samples
                .saturating_add(content.audio.len() as u64);
            // Audio after a completed generation reopens it.
            turn.generation_at = None;
        }
        if content.generation_complete {
            turn.generation_at = Some(now);
        }
        let answered = turn.first_output_at.is_some();
        if !content.audio.is_empty() {
            self.emit(Event::Audio {
                lineage,
                sequence,
                pcm: content.audio,
            })?;
        }
        for text in content.text {
            self.emit(Event::ModelText { lineage, text })?;
        }
        if content.generation_complete {
            self.emit(Event::GenerationComplete { lineage })?;
        }
        if content.turn_complete {
            let companion = self
                .stale_terminal
                .take()
                .is_some_and(|at| at.elapsed() < self.timeouts.barrier);
            if companion && !answered {
                // Companion of the stray interruption dropped just before.
                return self.discard(Discard::LateTerminal);
            }
            self.emit(Event::TurnComplete {
                lineage,
                idle: content.idle,
            })?;
            if content.idle {
                self.current = None;
            } else if let Some(turn) = self.current.as_mut() {
                turn.progress_at = Some(now);
            }
        }
        Ok(())
    }
    async fn promote(&mut self) -> Result<()> {
        let Some(successor) = self.successor.take() else {
            return Ok(());
        };
        let mut turn = Turn::new(
            Lineage {
                session: self.session,
                request: successor.request,
            },
            successor.input,
        );
        if successor.request.get() <= self.retired_through() {
            // Cancelled while it waited. Never commit it; an input that is
            // still open stays only until its End arrives.
            if turn.input.open {
                turn.retired = true;
                self.current = Some(turn);
            }
            return Ok(());
        }
        if !turn.input.open {
            self.end_activity().await?;
            turn.ended_at = Some(Instant::now());
        }
        self.current = Some(turn);
        Ok(())
    }
    fn emit(&mut self, event: Event) -> Result<()> {
        if self.outbox.len() >= OUTBOX_CAPACITY {
            return Err(Error::Backpressure);
        }
        self.outbox.push_back(event);
        self.flush();
        Ok(())
    }
    fn flush(&mut self) {
        let mut accepted = false;
        while let Some(event) = self.outbox.pop_front() {
            match self.events.try_send(event) {
                Ok(()) => accepted = true,
                Err(mpsc::error::TrySendError::Full(event)) => {
                    self.outbox.push_front(event);
                    break;
                }
                Err(mpsc::error::TrySendError::Closed(_)) => {
                    self.outbox.clear();
                    break;
                }
            }
        }
        if !self.outbox.is_empty() {
            // The delivery bound measures a consumer taking nothing at all,
            // not one that is slow but still making progress.
            if accepted || self.outbox_since.is_none() {
                self.outbox_since = Some(Instant::now());
            }
        } else if self.outbox_since.take().is_some() {
            // Time spent not reading is this client's doing, not a stall.
            let now = Instant::now();
            self.read_at = now;
            if let Some(progress) = self
                .current
                .as_mut()
                .and_then(|turn| turn.progress_at.as_mut())
            {
                *progress = now;
            }
        }
    }
}

async fn capacity(events: &mpsc::Sender<Event>) -> Result<()> {
    events.reserve().await.map(drop).map_err(|_| Error::Closed)
}

/// Quota and usage refusals are recognised from the close reason so they are
/// not retried as ordinary disconnects. The reason selects a fixed category
/// and is dropped here; it is never retained, logged or returned.
fn closed(frame: Option<tungstenite::protocol::CloseFrame>) -> Error {
    let Some(frame) = frame else {
        return Error::PeerClosed { code: None };
    };
    let reason = frame.reason.to_ascii_lowercase();
    if [
        "quota",
        "resource_exhausted",
        "resource exhausted",
        "usage limit",
        "rate limit",
    ]
    .iter()
    .any(|marker| reason.contains(marker))
    {
        return Error::QuotaExceeded;
    }
    Error::PeerClosed {
        code: Some(frame.code.into()),
    }
}

fn check_age(captured_at: Instant) -> Result<Duration> {
    Instant::now()
        .checked_duration_since(captured_at)
        .filter(|age| *age < MAX_INPUT_AGE)
        .ok_or(Error::StaleInput)
}
async fn send_audio<S: AsyncRead + AsyncWrite + Unpin>(
    socket: &mut WebSocketStream<S>,
    audio: &Audio,
    deadline: Duration,
) -> Result<()> {
    let age = check_age(audio.captured_at)?;
    let deadline = deadline.min(MAX_INPUT_AGE - age);
    send(
        socket,
        Message::text(wire::encode_audio(&audio.samples)?),
        deadline,
    )
    .await
}
async fn send<S: AsyncRead + AsyncWrite + Unpin>(
    socket: &mut WebSocketStream<S>,
    message: Message,
    deadline: Duration,
) -> Result<()> {
    timeout(deadline, socket.send(message))
        .await
        .map_err(|_| Error::WriteTimeout)?
        .map_err(transport_error)
}
fn transport_error(error: tungstenite::Error) -> Error {
    match error {
        tungstenite::Error::Http(response) => match response.status().as_u16() {
            401 | 403 => Error::Authentication,
            429 => Error::RateLimited,
            500..=599 => Error::ServerUnavailable,
            _ => Error::Transport,
        },
        tungstenite::Error::Capacity(_) => Error::MessageTooLarge,
        tungstenite::Error::WriteBufferFull(_) => Error::Backpressure,
        tungstenite::Error::ConnectionClosed | tungstenite::Error::AlreadyClosed => {
            Error::PeerClosed { code: None }
        }
        _ => Error::Transport,
    }
}

#[cfg(test)]
mod tests;
