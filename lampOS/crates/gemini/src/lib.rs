//! Bounded Gemini Live transport for a separately supervised provider process.
//!
//! This crate does not admit speech, mint actuator permits, or decide addressees.
//! The caller maps each immutable [`Lineage`] to the original interaction owner.
//! Input transcription and provider VAD are session-scoped observations, never
//! authorization. Local playback cancellation must not wait for this transport.
#![forbid(unsafe_code)]

mod config;
mod recovery;
mod session;
#[doc(hidden)]
pub mod testing;
pub mod wire;

pub use config::{
    Credential, GOOGLE_ENDPOINT, Resumption, ResumptionHandle, SessionConfig, ThinkingLevel,
    Timeouts, UnansweredInterruption,
};
pub use recovery::{
    Attempt, Connect, Context, Dialer, Failure, FailureReason, Notice, RecoveryPolicy, Stage,
    Supervisor,
};
use serde::{Deserialize, Serialize};
pub use session::{Connection, InputSender, ResumptionPoint, connect};
use std::{fmt, num::NonZeroU64, time::Duration};

pub const INPUT_RATE: u32 = 16_000;
pub const OUTPUT_RATE: u32 = 24_000;
pub const MAX_INPUT_SAMPLES: usize = 320;
pub const MAX_INPUT_AGE: Duration = Duration::from_secs(1);
pub const INPUT_QUEUE_CAPACITY: usize = 32;
pub const EVENT_QUEUE_CAPACITY: usize = 16;
/// Decoded events of the message being delivered while the event queue is full.
/// The socket is not read while any remain, so this never grows with traffic.
pub const OUTBOX_CAPACITY: usize = 32;
pub const MAX_HANDLE_BYTES: usize = 8_192;
pub const MAX_OUTPUT_SAMPLES: usize = 48_000;
pub const MAX_WIRE_BYTES: usize = 262_144;
pub const MAX_TEXT_BYTES: usize = 8_192;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(transparent)]
pub struct SessionId(NonZeroU64);
impl SessionId {
    /// Must be fresh after every reconnect, supplied by the trusted supervisor.
    pub fn new(value: u64) -> Result<Self> {
        NonZeroU64::new(value)
            .map(Self)
            .ok_or(Error::InvalidConfiguration)
    }
    pub fn get(self) -> u64 {
        self.0.get()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(transparent)]
pub struct RequestId(NonZeroU64);
impl RequestId {
    /// Strictly increasing within a session; old identifiers are never reused.
    pub fn new(value: u64) -> Result<Self> {
        NonZeroU64::new(value)
            .map(Self)
            .ok_or(Error::InvalidConfiguration)
    }
    pub fn get(self) -> u64 {
        self.0.get()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Lineage {
    pub session: SessionId,
    pub request: RequestId,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum State {
    Ready,
    Disconnected(Error),
    Closed,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum VoiceActivity {
    Start,
    End,
    Unspecified,
}

#[derive(Debug)]
pub enum Event {
    InputStarted {
        lineage: Lineage,
        waiting_for_barrier: bool,
    },
    Audio {
        lineage: Lineage,
        sequence: u64,
        pcm: Vec<i16>,
    },
    OutputTranscript {
        lineage: Lineage,
        text: String,
        finished: bool,
    },
    ModelText {
        lineage: Lineage,
        text: String,
    },
    GenerationComplete {
        lineage: Lineage,
    },
    Interrupted {
        lineage: Lineage,
    },
    /// `idle=false` is not an ownership-retirement barrier.
    TurnComplete {
        lineage: Lineage,
        idle: bool,
    },
    /// Google gives input transcripts no reliable per-turn ordering.
    UncorrelatedInputTranscript {
        session: SessionId,
        text: String,
        finished: bool,
    },
    VoiceActivity {
        session: SessionId,
        kind: VoiceActivity,
        audio_offset: Option<Duration>,
    },
    /// Advance notice that the service will close this connection. Not a
    /// failure: an unfinished request continues until its idle barrier.
    GoAway {
        session: SessionId,
        time_left: Option<Duration>,
    },
    /// A server message that no current request can own was dropped. It never
    /// cancels, completes or supplies output for a request.
    Discarded {
        session: SessionId,
        reason: Discard,
    },
}

/// Why a server message was dropped instead of being attached to a request.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Discard {
    /// `interrupted` for a request this client never asked to interrupt. With
    /// provider activity detection disabled only an explicit activityStart can
    /// interrupt, so the event belongs to an earlier request.
    LateInterruption,
    /// Completion that arrived before the current request could have one.
    LateTerminal,
    /// Audio or text that arrived while the current request was still input.
    LateOutput,
    /// Output or lifecycle with no request in progress.
    UnownedOutput,
    /// Opt-in [`UnansweredInterruption::AssumeCancelled`] released a barrier
    /// for a request that never produced output or a terminal event.
    UnansweredBarrier,
}

/// What a caller may do about a connection that ended with this error.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Recovery {
    /// A fresh connection may succeed now.
    Reconnect,
    /// The service refused for a reason that will not clear on its own soon
    /// (quota, rate or usage limit). Retrying immediately only adds load.
    Unavailable,
    /// Configuration, authentication, protocol or local fault.
    Fatal,
}

/// Fixed diagnostic categories. Never retain raw HTTP/WebSocket/server errors,
/// close reasons, URLs, headers, response bodies, or authentication values.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    InvalidConfiguration,
    ConnectTimeout,
    SetupTimeout,
    ReadTimeout,
    WriteTimeout,
    BarrierTimeout,
    /// No output arrived for an ended input within `Timeouts::response`.
    ResponseTimeout,
    /// Output began, then stopped before generation completed.
    StalledResponse,
    /// Generation completed but the idle barrier never followed.
    CompletionTimeout,
    Authentication,
    RateLimited,
    QuotaExceeded,
    ServerUnavailable,
    ServerRejected,
    ServerGoAway,
    Transport,
    PeerClosed {
        code: Option<u16>,
    },
    MalformedMessage,
    UnsupportedMessage,
    MessageTooLarge,
    InvalidAudio,
    Backpressure,
    StaleInput,
    InputSequence,
    StaleRequest,
    OverlappingInput,
    UnexpectedResponse,
    InputCancelled,
    /// Input was offered while no provider connection was established.
    NotConnected,
    Closed,
}
impl Error {
    pub fn recovery(self) -> Recovery {
        match self {
            Self::ConnectTimeout
            | Self::SetupTimeout
            | Self::ReadTimeout
            | Self::WriteTimeout
            | Self::BarrierTimeout
            | Self::ResponseTimeout
            | Self::StalledResponse
            | Self::CompletionTimeout
            | Self::ServerUnavailable
            | Self::ServerGoAway
            | Self::Transport => Recovery::Reconnect,
            // Application close codes observed on the deployment relay for a
            // missing key, an unconfigured route and an exhausted usage limit.
            Self::PeerClosed {
                code: Some(4001 | 4002 | 4029),
            } => Recovery::Unavailable,
            Self::PeerClosed { .. } => Recovery::Reconnect,
            Self::RateLimited | Self::QuotaExceeded => Recovery::Unavailable,
            Self::InvalidConfiguration
            | Self::Authentication
            | Self::ServerRejected
            | Self::MalformedMessage
            | Self::UnsupportedMessage
            | Self::MessageTooLarge
            | Self::InvalidAudio
            | Self::Backpressure
            | Self::StaleInput
            | Self::InputSequence
            | Self::StaleRequest
            | Self::OverlappingInput
            | Self::UnexpectedResponse
            | Self::InputCancelled
            | Self::NotConnected
            | Self::Closed => Recovery::Fatal,
        }
    }
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // All variants contain only fixed labels and an optional numeric code.
        write!(f, "Gemini Live {self:?}")
    }
}
impl std::error::Error for Error {}
pub type Result<T> = std::result::Result<T, Error>;
