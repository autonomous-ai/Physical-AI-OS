//! Bounded Gemini Live transport for a separately supervised provider process.
//!
//! This crate does not admit speech, mint actuator permits, or decide addressees.
//! The caller maps each immutable [`Lineage`] to the original interaction owner.
//! Input transcription and provider VAD are session-scoped observations, never
//! authorization. Local playback cancellation must not wait for this transport.
#![forbid(unsafe_code)]

mod config;
mod session;
pub mod wire;

pub use config::{Credential, GOOGLE_ENDPOINT, SessionConfig, ThinkingLevel, Timeouts};
use serde::{Deserialize, Serialize};
pub use session::{Connection, InputSender, connect};
use std::{fmt, num::NonZeroU64, time::Duration};

pub const INPUT_RATE: u32 = 16_000;
pub const OUTPUT_RATE: u32 = 24_000;
pub const MAX_INPUT_SAMPLES: usize = 320;
pub const MAX_INPUT_AGE: Duration = Duration::from_secs(1);
pub const INPUT_QUEUE_CAPACITY: usize = 32;
pub const EVENT_QUEUE_CAPACITY: usize = 16;
pub const MAX_PENDING_SAMPLES: usize = 12_800;
pub const MAX_PENDING_CHUNKS: usize = 128;
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
    ResponseTimeout,
    Authentication,
    RateLimited,
    ServerUnavailable,
    ServerRejected,
    ServerGoAway,
    Transport,
    PeerClosed { code: Option<u16> },
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
    Closed,
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // All variants contain only fixed labels and an optional numeric code.
        write!(f, "Gemini Live {self:?}")
    }
}
impl std::error::Error for Error {}
pub type Result<T> = std::result::Result<T, Error>;
