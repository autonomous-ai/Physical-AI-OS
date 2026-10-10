//! Bounded reconnection around one [`Connection`] at a time.
//!
//! The supervisor never replays input or re-requests an answer. When a
//! connection ends it reports exactly what the accepted request had reached,
//! then reconnects only within the caller's policy. Conversation context
//! survives only through a server-issued resumption handle; a reconnect
//! without one is reported as fresh, never presented as continuity.
use crate::{
    Connection, Error, Event, InputSender, Lineage, Recovery, RequestId, Result, ResumptionHandle,
    ResumptionPoint, SessionConfig, SessionId, State,
};
use std::{
    cell::Cell,
    collections::VecDeque,
    fmt,
    pin::Pin,
    time::{Duration, Instant},
};

pub type Attempt = Pin<Box<dyn Future<Output = Result<Connection>> + Send>>;

/// Opens one provider connection. Every call is a new session.
pub trait Connect {
    fn connect(&mut self, session: SessionId, resume: Option<ResumptionHandle>) -> Attempt;
}

/// Production connector: TLS WebSocket to the configured endpoint.
pub struct Dialer {
    config: SessionConfig,
}
impl Dialer {
    pub fn new(config: SessionConfig) -> Self {
        Self { config }
    }
}
impl Connect for Dialer {
    fn connect(&mut self, session: SessionId, resume: Option<ResumptionHandle>) -> Attempt {
        Box::pin(crate::connect(self.config.clone().resume(resume), session))
    }
}

/// Limits on reconnect work. The first connection is never retried.
#[derive(Clone, Copy, Debug)]
pub struct RecoveryPolicy {
    /// Connection attempts per outage. Zero disables recovery.
    pub max_attempts: u32,
    /// Wait before the second attempt; doubles up to `max_backoff`.
    pub first_backoff: Duration,
    pub max_backoff: Duration,
    /// Connection loss to completed setup, across all attempts.
    pub outage_budget: Duration,
    /// Recoveries allowed within `window` before the link is declared unstable.
    pub max_recoveries: u32,
    pub window: Duration,
    /// Refuse to reconnect when no resumption handle is available, instead of
    /// continuing with a session that has forgotten the conversation.
    pub require_context: bool,
}
impl Default for RecoveryPolicy {
    fn default() -> Self {
        Self {
            max_attempts: 4,
            first_backoff: Duration::from_millis(500),
            max_backoff: Duration::from_secs(4),
            outage_budget: Duration::from_secs(15),
            max_recoveries: 3,
            window: Duration::from_secs(60),
            require_context: true,
        }
    }
}
impl RecoveryPolicy {
    /// Any connection loss ends the supervisor.
    pub const DISABLED: Self = Self {
        max_attempts: 0,
        first_backoff: Duration::ZERO,
        max_backoff: Duration::ZERO,
        outage_budget: Duration::ZERO,
        max_recoveries: 0,
        window: Duration::ZERO,
        require_context: true,
    };
    fn validate(self) -> Result<()> {
        if self.max_attempts > 16
            || self.max_recoveries > 64
            || self.first_backoff > self.max_backoff
            || self.max_backoff > Duration::from_secs(60)
            || self.outage_budget > Duration::from_secs(120)
            || self.window > Duration::from_secs(3600)
            || (self.max_attempts > 0 && (self.max_recoveries == 0 || self.outage_budget.is_zero()))
        {
            return Err(Error::InvalidConfiguration);
        }
        Ok(())
    }
    fn backoff(self, completed_attempts: u32) -> Duration {
        let doublings = completed_attempts.saturating_sub(1).min(16);
        self.first_backoff
            .saturating_mul(1 << doublings)
            .min(self.max_backoff)
    }
}

/// How far an accepted request had progressed when its connection ended.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Stage {
    /// Input was still being sent.
    InputOpen,
    /// Input ended; no output had arrived.
    AwaitingResponse,
    /// Some output arrived; generation had not completed.
    Responding,
    /// Generation completed and every earlier event was delivered.
    Generated,
}

/// Conversation state of a newly ready connection.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Context {
    /// First connection of this supervisor.
    Initial,
    /// Resumed from a handle the service had marked current.
    Resumed,
    /// Resumed from the last available handle, issued before the most recent
    /// exchange. That exchange may be missing from the restored conversation.
    ResumedBeforeLatest,
    /// Reconnected without a handle. Earlier conversation is not available.
    Fresh,
}

#[derive(Debug)]
pub enum Notice {
    /// Setup completed. `after` is the error that ended the previous
    /// connection; it is absent for the first one. `elapsed` runs from that
    /// loss (or from the first attempt) to completed setup.
    Ready {
        session: SessionId,
        context: Context,
        attempts: u32,
        elapsed: Duration,
        after: Option<Error>,
    },
    Event(Event),
    /// The connection ended after this request's generation completed and all
    /// of its output was delivered. Only the idle barrier was outstanding, and
    /// a closed connection can deliver nothing further for it.
    Settled {
        lineage: Lineage,
        error: Error,
    },
    /// The connection ended while this accepted request was unfinished. Its
    /// input is not replayed and no answer is requested again.
    TurnLost {
        lineage: Lineage,
        stage: Stage,
        error: Error,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FailureReason {
    /// Local shutdown was requested.
    Shutdown,
    /// The first connection failed.
    InitialConnect,
    /// The error class does not allow an immediate reconnect.
    NotRecoverable,
    /// Policy requires conversation context and no handle is available.
    NoResumableContext,
    AttemptsExhausted,
    BudgetExhausted,
    /// Too many recoveries inside the policy window.
    Unstable,
    /// The connection task ended without reporting a terminal state.
    Lifecycle,
}

/// Terminal result. `error` is the fixed category that ended the last
/// connection or attempt; no raw server text is carried.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Failure {
    pub error: Error,
    pub reason: FailureReason,
    pub attempts: u32,
}
impl fmt::Display for Failure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{} ({:?} after {} recovery attempt(s))",
            self.error, self.reason, self.attempts
        )
    }
}
impl std::error::Error for Failure {}

#[derive(Clone, Copy)]
struct Tracked {
    lineage: Lineage,
    stage: Stage,
}
#[derive(Clone, Copy)]
struct Outage {
    since: Instant,
    error: Error,
    /// Whether the handle offered for this outage was current when lost.
    context: Context,
}
enum Link {
    Connecting {
        attempt: Attempt,
        session: SessionId,
        number: u32,
        started: Instant,
        outage: Option<Outage>,
    },
    Waiting {
        until: Instant,
        number: u32,
        outage: Outage,
    },
    Connected {
        connection: Connection,
        input: InputSender,
        session: SessionId,
    },
    Failed(Failure),
}

pub struct Supervisor<C> {
    connector: C,
    policy: RecoveryPolicy,
    sessions: u64,
    link: Link,
    /// Updated from the input methods (shared access) and from events.
    tracked: Cell<Option<Tracked>>,
    retired_through: Cell<u64>,
    resume: Option<ResumptionPoint>,
    recoveries: VecDeque<Instant>,
    /// At most the two notices produced by one connection loss.
    pending: VecDeque<Notice>,
}

impl<C: Connect> Supervisor<C> {
    /// Starts the first connection attempt. It completes inside [`Self::next`].
    pub fn new(mut connector: C, policy: RecoveryPolicy) -> Result<Self> {
        policy.validate()?;
        let session = SessionId::new(1)?;
        let attempt = connector.connect(session, None);
        Ok(Self {
            connector,
            policy,
            sessions: 1,
            link: Link::Connecting {
                attempt,
                session,
                number: 1,
                started: Instant::now(),
                outage: None,
            },
            tracked: Cell::new(None),
            retired_through: Cell::new(0),
            resume: None,
            recoveries: VecDeque::new(),
            pending: VecDeque::with_capacity(2),
        })
    }

    pub fn is_connected(&self) -> bool {
        matches!(self.link, Link::Connected { .. })
    }

    fn input(&self) -> Result<(&InputSender, SessionId)> {
        match &self.link {
            Link::Connected { input, session, .. } => Ok((input, *session)),
            _ => Err(Error::NotConnected),
        }
    }

    /// See [`InputSender::try_start`]. Fails with [`Error::NotConnected`]
    /// while no connection is ready; accepted input is never buffered here.
    pub fn try_start(&self, request: RequestId) -> Result<()> {
        let (input, session) = self.input()?;
        input.try_start(request)?;
        self.tracked.set(Some(Tracked {
            lineage: Lineage { session, request },
            stage: Stage::InputOpen,
        }));
        Ok(())
    }
    pub fn try_audio(
        &self,
        request: RequestId,
        sequence: u64,
        captured_at: Instant,
        samples: &[i16],
    ) -> Result<()> {
        self.input()?
            .0
            .try_audio(request, sequence, captured_at, samples)
    }
    pub fn try_end(&self, request: RequestId) -> Result<()> {
        self.input()?.0.try_end(request)?;
        if let Some(mut tracked) = self.tracked.get()
            && tracked.lineage.request == request
            && tracked.stage == Stage::InputOpen
        {
            tracked.stage = Stage::AwaitingResponse;
            self.tracked.set(Some(tracked));
        }
        Ok(())
    }
    /// Synchronous local retirement. It is remembered across a reconnect and
    /// never waits for the network or for a connection to exist.
    pub fn retire(&self, request: RequestId) {
        self.retired_through
            .set(self.retired_through.get().max(request.get()));
        if self
            .tracked
            .get()
            .is_some_and(|tracked| tracked.lineage.request <= request)
        {
            // Cancelled locally: nothing is owed to it any more.
            self.tracked.set(None);
        }
        if let Ok((input, _)) = self.input() {
            input.retire(request);
        }
    }
    pub fn shutdown(&mut self) {
        if let Link::Connected { input, .. } = &self.link {
            input.shutdown();
        }
        self.link = Link::Failed(Failure {
            error: Error::Closed,
            reason: FailureReason::Shutdown,
            attempts: 0,
        });
    }

    fn observe(&self, event: &Event) {
        let Some(mut tracked) = self.tracked.get() else {
            return;
        };
        let stage = match event {
            Event::Audio { lineage, .. } | Event::ModelText { lineage, .. }
                if *lineage == tracked.lineage =>
            {
                Some(Stage::Responding)
            }
            Event::OutputTranscript { lineage, .. }
                if *lineage == tracked.lineage && tracked.stage == Stage::AwaitingResponse =>
            {
                Some(Stage::Responding)
            }
            Event::GenerationComplete { lineage } if *lineage == tracked.lineage => {
                Some(Stage::Generated)
            }
            Event::TurnComplete {
                lineage,
                idle: true,
            } if *lineage == tracked.lineage => {
                self.tracked.set(None);
                return;
            }
            _ => None,
        };
        if let Some(stage) = stage {
            tracked.stage = stage;
            self.tracked.set(Some(tracked));
        }
    }

    /// Cancellation-safe. Returns the next notice, or the terminal failure
    /// once (and then repeatedly) when recovery is not allowed or has ended.
    pub async fn next(&mut self) -> std::result::Result<Notice, Failure> {
        loop {
            if let Some(notice) = self.pending.pop_front() {
                return Ok(notice);
            }
            match &mut self.link {
                Link::Failed(failure) => return Err(*failure),
                Link::Connected { connection, .. } => match connection.next_event().await {
                    Some(event) => {
                        self.observe(&event);
                        return Ok(Notice::Event(event));
                    }
                    None => self.lost(),
                },
                Link::Waiting {
                    until,
                    number,
                    outage,
                } => {
                    tokio::time::sleep_until((*until).into()).await;
                    let (number, outage) = (*number, *outage);
                    self.dial(number, Some(outage));
                }
                Link::Connecting {
                    attempt,
                    session,
                    number,
                    started,
                    outage,
                } => {
                    let (session, number, started, outage) = (*session, *number, *started, *outage);
                    let result = match outage {
                        // The first connection is bounded by its own timeouts.
                        None => attempt.as_mut().await,
                        Some(outage) => {
                            let deadline = outage.since + self.policy.outage_budget;
                            tokio::select! {
                                result = attempt.as_mut() => result,
                                _ = tokio::time::sleep_until(deadline.into()) => {
                                    self.fail(outage.error, FailureReason::BudgetExhausted, number);
                                    continue;
                                }
                            }
                        }
                    };
                    match result {
                        Ok(connection) => {
                            return Ok(self.connected(connection, session, number, started, outage));
                        }
                        Err(error) => self.attempt_failed(error, number, outage),
                    }
                }
            }
        }
    }

    fn fail(&mut self, error: Error, reason: FailureReason, attempts: u32) {
        self.link = Link::Failed(Failure {
            error,
            reason,
            attempts,
        });
    }

    fn dial(&mut self, number: u32, outage: Option<Outage>) {
        self.sessions += 1;
        let Ok(session) = SessionId::new(self.sessions) else {
            return self.fail(
                Error::InvalidConfiguration,
                FailureReason::Lifecycle,
                number,
            );
        };
        let handle = self.resume.as_ref().map(|point| point.handle().clone());
        self.link = Link::Connecting {
            attempt: self.connector.connect(session, handle),
            session,
            number,
            started: Instant::now(),
            outage,
        };
    }

    fn connected(
        &mut self,
        connection: Connection,
        session: SessionId,
        number: u32,
        started: Instant,
        outage: Option<Outage>,
    ) -> Notice {
        let input = connection.input();
        if let Ok(retired) = RequestId::new(self.retired_through.get()) {
            input.retire(retired);
        }
        if outage.is_some() {
            self.recoveries.push_back(Instant::now());
        }
        self.link = Link::Connected {
            connection,
            input,
            session,
        };
        Notice::Ready {
            session,
            context: outage.map_or(Context::Initial, |outage| outage.context),
            attempts: number,
            elapsed: outage.map_or(started, |outage| outage.since).elapsed(),
            after: outage.map(|outage| outage.error),
        }
    }

    fn attempt_failed(&mut self, error: Error, number: u32, outage: Option<Outage>) {
        let Some(mut outage) = outage else {
            return self.fail(error, FailureReason::InitialConnect, 0);
        };
        // A refusal while presenting a handle may be the handle being refused.
        // Only a policy that tolerates a session without context may drop it.
        let drop_handle = self.resume.is_some()
            && !self.policy.require_context
            && matches!(
                error,
                Error::ServerRejected | Error::UnexpectedResponse | Error::PeerClosed { .. }
            );
        match error.recovery() {
            Recovery::Reconnect => {}
            Recovery::Fatal if drop_handle => {}
            Recovery::Fatal | Recovery::Unavailable => {
                return self.fail(error, FailureReason::NotRecoverable, number);
            }
        }
        if drop_handle {
            self.resume = None;
            outage.context = Context::Fresh;
        }
        if number >= self.policy.max_attempts {
            return self.fail(error, FailureReason::AttemptsExhausted, number);
        }
        let until = Instant::now() + self.policy.backoff(number);
        if until >= outage.since + self.policy.outage_budget {
            return self.fail(error, FailureReason::BudgetExhausted, number);
        }
        self.link = Link::Waiting {
            until,
            number: number + 1,
            outage,
        };
    }

    /// The event stream ended: every event the connection produced has been
    /// returned. Report what the accepted request had reached, then decide.
    fn lost(&mut self) {
        let placeholder = Link::Failed(Failure {
            error: Error::Closed,
            reason: FailureReason::Lifecycle,
            attempts: 0,
        });
        let Link::Connected { connection, .. } = std::mem::replace(&mut self.link, placeholder)
        else {
            return;
        };
        if let Some(point) = connection.resumption() {
            self.resume = Some(point);
        }
        let error = match connection.state() {
            State::Disconnected(error) => error,
            State::Closed => return self.fail(Error::Closed, FailureReason::Shutdown, 0),
            // Already failed as a lifecycle fault by the placeholder above.
            State::Ready => return,
        };
        drop(connection);
        if let Some(tracked) = self.tracked.take() {
            // Its identifier ends with this connection: a later session must
            // not accept the same request again as if nothing had happened.
            self.retired_through.set(
                self.retired_through
                    .get()
                    .max(tracked.lineage.request.get()),
            );
            self.pending.push_back(match tracked.stage {
                Stage::Generated => Notice::Settled {
                    lineage: tracked.lineage,
                    error,
                },
                stage => Notice::TurnLost {
                    lineage: tracked.lineage,
                    stage,
                    error,
                },
            });
        }
        if self.policy.max_attempts == 0 || error.recovery() != Recovery::Reconnect {
            return self.fail(error, FailureReason::NotRecoverable, 0);
        }
        let context = match &self.resume {
            Some(point) if point.is_current() => Context::Resumed,
            Some(_) => Context::ResumedBeforeLatest,
            None if self.policy.require_context => {
                return self.fail(error, FailureReason::NoResumableContext, 0);
            }
            None => Context::Fresh,
        };
        let now = Instant::now();
        while self
            .recoveries
            .front()
            .is_some_and(|at| now.duration_since(*at) >= self.policy.window)
        {
            self.recoveries.pop_front();
        }
        if self.recoveries.len() >= self.policy.max_recoveries as usize {
            return self.fail(error, FailureReason::Unstable, 0);
        }
        self.dial(
            1,
            Some(Outage {
                since: now,
                error,
                context,
            }),
        );
    }
}

#[cfg(test)]
mod tests;
