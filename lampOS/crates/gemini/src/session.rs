use crate::{
    EVENT_QUEUE_CAPACITY, Error, Event, INPUT_QUEUE_CAPACITY, Lineage, MAX_INPUT_AGE,
    MAX_INPUT_SAMPLES, MAX_PENDING_CHUNKS, MAX_PENDING_SAMPLES, MAX_WIRE_BYTES, RequestId, Result,
    SessionConfig, SessionId, State, Timeouts, wire,
};
use futures_util::{SinkExt, StreamExt};
use std::{
    collections::VecDeque,
    sync::{
        Arc,
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
    /// Cancellation-safe receiver. Already queued retired audio/text is dropped,
    /// while lifecycle events retain their original lineage for bookkeeping.
    pub async fn next_event(&mut self) -> Option<Event> {
        while let Some(event) = self.events.recv().await {
            let output_request = match &event {
                Event::Audio { lineage, .. }
                | Event::OutputTranscript { lineage, .. }
                | Event::ModelText { lineage, .. } => Some(lineage.request.get()),
                _ => None,
            };
            if output_request.is_some_and(|id| {
                self.state() != State::Ready
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

fn socket_config() -> WebSocketConfig {
    WebSocketConfig::default()
        .read_buffer_size(16_384)
        .write_buffer_size(0)
        .max_write_buffer_size(MAX_WIRE_BYTES + 4096)
        .max_message_size(Some(MAX_WIRE_BYTES))
        .max_frame_size(Some(MAX_WIRE_BYTES))
}

async fn setup_and_spawn<S>(
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
                    if frame.content.is_some() || frame.go_away || frame.voice_activity.is_some() {
                        return Err(Error::UnexpectedResponse);
                    }
                }
                Message::Binary(bytes) => {
                    let frame = wire::decode_server(&bytes)?;
                    if frame.setup_complete {
                        return Ok(());
                    }
                    if frame.content.is_some() || frame.go_away || frame.voice_activity.is_some() {
                        return Err(Error::UnexpectedResponse);
                    }
                }
                Message::Ping(bytes) => {
                    send(&mut socket, Message::Pong(bytes), config.timeouts.write).await?
                }
                Message::Pong(_) => {}
                Message::Close(frame) => {
                    return Err(Error::PeerClosed {
                        code: frame.map(|frame| frame.code.into()),
                    });
                }
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
        commands,
        events: events_tx,
        shared,
        stop: stop_rx,
        current: None,
        pending: None,
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
    retired: bool,
    retire_at: Option<Instant>,
    response_at: Option<Instant>,
    audio_sequence: u64,
}
impl Turn {
    fn new(lineage: Lineage) -> Self {
        Self {
            lineage,
            input: InputState::new(),
            retired: false,
            retire_at: None,
            response_at: None,
            audio_sequence: 0,
        }
    }
}
struct Pending {
    request: RequestId,
    input: InputState,
    audio: VecDeque<Audio>,
    samples: usize,
}
struct Actor<S> {
    socket: WebSocketStream<S>,
    session: SessionId,
    timeouts: Timeouts,
    commands: mpsc::Receiver<Command>,
    events: mpsc::Sender<Event>,
    shared: Arc<Shared>,
    stop: watch::Receiver<Option<Error>>,
    current: Option<Turn>,
    pending: Option<Pending>,
    activity_open: bool,
    last_request: u64,
    read_at: Instant,
    ping_at: Instant,
}
impl<S: AsyncRead + AsyncWrite + Unpin> Actor<S> {
    async fn run(mut self) -> Result<()> {
        let mut tick = tokio::time::interval(Duration::from_millis(10));
        tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
        loop {
            if let Some(error) = *self.stop.borrow() {
                return Err(error);
            }
            self.retire_active().await?;
            self.check_deadlines()?;
            if self.ping_at.elapsed() >= self.timeouts.keepalive {
                send(
                    &mut self.socket,
                    Message::Ping(Vec::new().into()),
                    self.timeouts.write,
                )
                .await?;
                self.ping_at = Instant::now();
            }
            tokio::select! {
                _ = self.shared.wake.notified() => {},
                _ = tick.tick() => {},
                command = self.commands.recv() => match command { Some(command) => self.command(command).await?, None => return Ok(()) },
                incoming = self.socket.next() => {
                    let incoming = incoming.ok_or(Error::PeerClosed { code: None })?.map_err(transport_error)?;
                    self.read_at = Instant::now();
                    self.message(incoming).await?;
                }
            }
        }
    }
    fn check_deadlines(&self) -> Result<()> {
        if self.read_at.elapsed() >= self.timeouts.read {
            return Err(Error::ReadTimeout);
        }
        if let Some(turn) = &self.current {
            if turn
                .retire_at
                .is_some_and(|started| started.elapsed() >= self.timeouts.barrier)
            {
                return Err(Error::BarrierTimeout);
            }
            if turn
                .response_at
                .is_some_and(|started| started.elapsed() >= self.timeouts.response)
            {
                return Err(Error::ResponseTimeout);
            }
        }
        if let Some(pending) = &self.pending
            && let Some(audio) = pending.audio.front()
        {
            check_age(audio.captured_at)?;
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
    async fn retire_active(&mut self) -> Result<()> {
        let Some(turn) = self.current.as_mut() else {
            return Ok(());
        };
        if turn.lineage.request.get() <= self.shared.retired_through.load(Ordering::Acquire) {
            if !turn.retired {
                turn.retired = true;
                turn.retire_at = Some(Instant::now());
            }
            if !turn.input.open {
                self.start_activity().await?;
            }
        }
        Ok(())
    }
    async fn command(&mut self, command: Command) -> Result<()> {
        match command {
            Command::Start(request) => {
                if request.get() <= self.last_request || self.pending.is_some() {
                    return Err(Error::OverlappingInput);
                }
                if self.current.as_ref().is_some_and(|turn| turn.input.open) {
                    return Err(Error::OverlappingInput);
                }
                self.last_request = request.get();
                let lineage = Lineage {
                    session: self.session,
                    request,
                };
                if self.current.is_some() {
                    self.retire_active().await?;
                    self.pending = Some(Pending {
                        request,
                        input: InputState::new(),
                        audio: VecDeque::with_capacity(MAX_PENDING_CHUNKS),
                        samples: 0,
                    });
                    self.start_activity().await?;
                    self.emit(Event::InputStarted {
                        lineage,
                        waiting_for_barrier: true,
                    })?;
                } else {
                    self.current = Some(Turn::new(lineage));
                    self.start_activity().await?;
                    self.emit(Event::InputStarted {
                        lineage,
                        waiting_for_barrier: false,
                    })?;
                }
            }
            Command::Audio(audio) => {
                if audio.request.get() <= self.shared.retired_through.load(Ordering::Acquire) {
                    return Ok(());
                }
                if let Some(pending) = self.pending.as_mut() {
                    if pending.request != audio.request {
                        return Err(Error::StaleRequest);
                    }
                    pending.input.accept(&audio)?;
                    if pending.audio.len() >= MAX_PENDING_CHUNKS
                        || pending.samples + audio.samples.len() > MAX_PENDING_SAMPLES
                    {
                        return Err(Error::Backpressure);
                    }
                    pending.samples += audio.samples.len();
                    pending.audio.push_back(audio);
                } else {
                    let turn = self.current.as_mut().ok_or(Error::StaleRequest)?;
                    if turn.lineage.request != audio.request {
                        return Err(Error::StaleRequest);
                    }
                    turn.input.accept(&audio)?;
                    send_audio(&mut self.socket, &audio, self.timeouts.write).await?;
                }
            }
            Command::End(request) => {
                if let Some(pending) = self.pending.as_mut() {
                    if pending.request != request || !pending.input.open {
                        return Err(Error::StaleRequest);
                    }
                    pending.input.open = false;
                } else {
                    let turn = self.current.as_mut().ok_or(Error::StaleRequest)?;
                    if turn.lineage.request != request || !turn.input.open {
                        return Err(Error::StaleRequest);
                    }
                    turn.input.open = false;
                    turn.response_at = Some(Instant::now());
                    send(
                        &mut self.socket,
                        Message::text(wire::ACTIVITY_END),
                        self.timeouts.write,
                    )
                    .await?;
                    self.activity_open = false;
                }
            }
        }
        Ok(())
    }
    async fn message(&mut self, message: Message) -> Result<()> {
        let frame = match message {
            Message::Text(text) => wire::decode_server(text.as_bytes())?,
            Message::Binary(bytes) => wire::decode_server(&bytes)?,
            Message::Ping(bytes) => {
                send(&mut self.socket, Message::Pong(bytes), self.timeouts.write).await?;
                return Ok(());
            }
            Message::Pong(_) => return Ok(()),
            Message::Close(frame) => {
                return Err(Error::PeerClosed {
                    code: frame.map(|frame| frame.code.into()),
                });
            }
            Message::Frame(_) => return Err(Error::MalformedMessage),
        };
        if frame.setup_complete {
            return Err(Error::UnexpectedResponse);
        }
        if frame.go_away {
            return Err(Error::ServerGoAway);
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
    async fn content(&mut self, content: wire::ServerContent) -> Result<()> {
        if let Some(transcript) = content.input_transcript {
            self.emit(Event::UncorrelatedInputTranscript {
                session: self.session,
                text: transcript.text,
                finished: transcript.finished,
            })?;
        }
        let has_output = !content.audio.is_empty()
            || !content.text.is_empty()
            || content.output_transcript.is_some()
            || content.generation_complete
            || content.interrupted
            || content.turn_complete;
        if !has_output {
            return Ok(());
        }
        let turn = self.current.as_mut().ok_or(Error::UnexpectedResponse)?;
        let lineage = turn.lineage;
        if content.interrupted {
            turn.retired = true;
            turn.retire_at.get_or_insert_with(Instant::now);
            self.shared
                .retired_through
                .fetch_max(lineage.request.get(), Ordering::AcqRel);
        }
        let retired = turn.retired
            || lineage.request.get() <= self.shared.retired_through.load(Ordering::Acquire);
        if !retired && turn.input.open {
            return Err(Error::UnexpectedResponse);
        }
        let audio_sequence = turn.audio_sequence;
        if !content.audio.is_empty() {
            turn.audio_sequence = turn
                .audio_sequence
                .checked_add(1)
                .ok_or(Error::UnexpectedResponse)?;
        }
        if content.interrupted {
            self.emit(Event::Interrupted { lineage })?;
        }
        if !retired {
            if !content.audio.is_empty() {
                self.emit(Event::Audio {
                    lineage,
                    sequence: audio_sequence,
                    pcm: content.audio,
                })?;
            }
            for text in content.text {
                self.emit(Event::ModelText { lineage, text })?;
            }
            if let Some(transcript) = content.output_transcript {
                self.emit(Event::OutputTranscript {
                    lineage,
                    text: transcript.text,
                    finished: transcript.finished,
                })?;
            }
            if content.generation_complete {
                self.emit(Event::GenerationComplete { lineage })?;
            }
        }
        if content.turn_complete {
            self.emit(Event::TurnComplete {
                lineage,
                idle: content.idle,
            })?;
            if content.idle {
                self.current = None;
                self.promote_pending().await?;
            }
        }
        Ok(())
    }
    async fn promote_pending(&mut self) -> Result<()> {
        let Some(mut pending) = self.pending.take() else {
            return Ok(());
        };
        if pending.request.get() <= self.shared.retired_through.load(Ordering::Acquire) {
            return Err(Error::InputCancelled);
        }
        let mut turn = Turn::new(Lineage {
            session: self.session,
            request: pending.request,
        });
        turn.input = pending.input;
        while let Some(audio) = pending.audio.pop_front() {
            if pending.request.get() <= self.shared.retired_through.load(Ordering::Acquire) {
                return Err(Error::InputCancelled);
            }
            if let Some(error) = *self.stop.borrow() {
                return Err(error);
            }
            send_audio(&mut self.socket, &audio, self.timeouts.write).await?;
        }
        if !turn.input.open {
            send(
                &mut self.socket,
                Message::text(wire::ACTIVITY_END),
                self.timeouts.write,
            )
            .await?;
            self.activity_open = false;
            turn.response_at = Some(Instant::now());
        }
        self.current = Some(turn);
        Ok(())
    }
    fn emit(&self, event: Event) -> Result<()> {
        self.events.try_send(event).map_err(|error| match error {
            mpsc::error::TrySendError::Full(_) => Error::Backpressure,
            mpsc::error::TrySendError::Closed(_) => Error::Closed,
        })
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
