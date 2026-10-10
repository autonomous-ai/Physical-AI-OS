//! Explicit, size-bounded local process envelopes. Controller and worker boots
//! are checked by the receiver, never learned from unsolicited messages.
use crate::{
    diagnostic_control::{DiagnosticFinished, DiagnosticStarted},
    options::NoiseSuppression,
};
use lamp_audio::ownership::{PlaybackCursor, QueuedRange};
use lamp_interaction::{BootId, Error as InteractionError, OutputPermit, Snapshot, TurnOwner};
use lamp_ipc::MAX_DATAGRAM_BYTES;
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use std::io;

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Envelope<T> {
    pub boot: BootId,
    pub sequence: u64,
    pub sent_at_us: u64,
    pub payload: T,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Control {
    Authority { snapshot: Snapshot },
    ConnectReference { peer: BootId },
    StartCapture,
    Stop,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum WorkerEvent {
    Ready,
    DiagnosticsStarted {
        started: DiagnosticStarted,
    },
    DiagnosticsFinished {
        receipt: DiagnosticFinished,
    },
    Privacy {
        acquired_at_us: u64,
        muted: bool,
    },
    CapturePrepared {
        epoch: u64,
        privacy_generation: u64,
        prepared_at_us: u64,
    },
    CaptureStarted {
        epoch: u64,
        dsp_epoch: u64,
        noise_suppression: NoiseSuppression,
        privacy_generation: u64,
        rate: u32,
        channels: u32,
        period_frames: i64,
        buffer_frames: i64,
        monotonic_status_timestamps: bool,
        open_started_at_us: u64,
        prepared_at_us: u64,
        capture_started_at_us: u64,
        start_completed_at_us: u64,
    },
    CaptureStopped,
    /// A planned speaker flush resets echo reference, not the user's active turn.
    CaptureDspReset {
        epoch: u64,
        dsp_epoch: u64,
        observed_at_us: u64,
    },
    Playback {
        turn: u64,
        sequence: u64,
        queued_frames: usize,
        accepted_frames: usize,
        observed_at_us: u64,
    },
    SpeechRetired {
        owner: TurnOwner,
        sequence: u64,
        final_sample: PlaybackCursor,
        observed_at_us: u64,
    },
    /// Accepted old-owner PCM retired after ordinary admitted supersession.
    /// This preserves cancellation; it is never a completed-answer receipt.
    CancelledTailRetired {
        owner: TurnOwner,
        superseded_by: TurnOwner,
        first_sample: PlaybackCursor,
        end_sample: PlaybackCursor,
        queued_frames: usize,
        speech_frames: usize,
        started_at_us: u64,
        observed_at_us: u64,
    },
    /// Real zero PCM accepted while a reply still owes speech. These receipts
    /// describe a delivery gap, never inferred room silence or completed speech.
    PlaybackGap {
        phase: PlaybackGapPhase,
        gap: PlaybackGap,
        observed_at_us: u64,
    },
    ReferenceClock {
        timing: ReferenceTiming,
    },
    ReferenceFault {
        fault: ReferenceFault,
    },
    PlaybackRejected {
        turn: u64,
        sequence: u64,
        reason: String,
    },
    PlaybackDiscarded {
        owner: TurnOwner,
        reason: PlaybackDiscardReason,
        queued_frames: usize,
        other_owners: bool,
        observed_at_us: u64,
    },
    Fault {
        code: String,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlaybackGapPhase {
    Started,
    Resumed,
    Cancelled,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaybackGap {
    /// The affected reply; this is not permission to write either speech or zeros.
    pub owner: TurnOwner,
    pub first_sample: PlaybackCursor,
    pub end_sample: PlaybackCursor,
    pub started_at_us: u64,
    pub last_zero_accepted_at_us: u64,
    pub zero_frames: u64,
}

/// Stable typed reasons for every failure reachable through BoundaryGuard::check.
/// Other authority failures still fail closed; their text is never interpreted.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlaybackDiscardReason {
    StaleOwner,
    ExpiredPermit,
    ExpiredState,
    PrivacyClosed,
    InputUnavailable,
    StaleCameraGrant,
    InputStillActive,
    StalePresentation,
    StateTooOld,
    FuturePermit,
    FutureState,
    WrongBoot,
    WrongOutputKind,
    NoAuthority,
    ClockRegression,
    Faulted,
    OtherAuthorityFailure,
}

impl From<InteractionError> for PlaybackDiscardReason {
    fn from(error: InteractionError) -> Self {
        match error {
            InteractionError::StaleOwner => Self::StaleOwner,
            InteractionError::ExpiredPermit => Self::ExpiredPermit,
            InteractionError::ExpiredState => Self::ExpiredState,
            InteractionError::PrivacyClosed => Self::PrivacyClosed,
            InteractionError::InputUnavailable => Self::InputUnavailable,
            InteractionError::StaleCameraGrant => Self::StaleCameraGrant,
            InteractionError::InputStillActive => Self::InputStillActive,
            InteractionError::StalePresentation => Self::StalePresentation,
            InteractionError::StateTooOld => Self::StateTooOld,
            InteractionError::FuturePermit => Self::FuturePermit,
            InteractionError::FutureState => Self::FutureState,
            InteractionError::WrongBoot => Self::WrongBoot,
            InteractionError::WrongOutputKind => Self::WrongOutputKind,
            InteractionError::NoAuthority => Self::NoAuthority,
            InteractionError::ClockRegression => Self::ClockRegression,
            InteractionError::Faulted => Self::Faulted,
            _ => Self::OtherAuthorityFailure,
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MicrophoneFrame {
    pub epoch: u64,
    /// Changes on AEC reset; planned playback cancellation preserves frame sequence.
    pub dsp_epoch: u64,
    pub privacy_generation: u64,
    pub frame_sequence: u64,
    /// Host read completion, not a measured acoustic microphone timestamp.
    pub read_completed_at_us: u64,
    pub timing: CaptureTiming,
    pub probability: f32,
    pub samples: Vec<i16>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SpeakerChunk {
    pub snapshot: Snapshot,
    pub permit: OutputPermit,
    pub chunk_sequence: u64,
    /// The provider has completed; this is its final actual PCM block, including
    /// any trailing silence deliberately written to complete a 10 ms block.
    pub end_of_speech: bool,
    pub samples: Vec<i16>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RenderReference {
    pub playback_epoch: u64,
    pub privacy_generation: u64,
    pub sequence: u64,
    pub accepted_at_us: u64,
    pub queue_observed_at_us: u64,
    pub first_sample: u64,
    pub queued_frames: usize,
    /// Only samples actually accepted by the speaker; may be a partial block.
    pub payload: RenderPayload,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum RenderPayload {
    Silence { frames: u16 },
    Speech { owner: TurnOwner, samples: Vec<i16> },
}

impl RenderPayload {
    pub fn frames(&self) -> usize {
        match self {
            Self::Silence { frames } => usize::from(*frames),
            Self::Speech { samples, .. } => samples.len(),
        }
    }
}

/// Host/driver diagnostics. No field is a measured acoustic acquisition time.
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CaptureTiming {
    pub first_read_started_at_us: u64,
    pub last_read_started_at_us: u64,
    pub successful_reads: u16,
    pub alsa_status_monotonic_us: Option<u64>,
    pub status_observed_at_us: u64,
    pub available_frames: i64,
    pub delayed_frames: i64,
    pub processing_completed_at_us: u64,
    /// ALSA queue-derived estimate, not an acoustic delay measurement.
    pub aec_queue_delay_ms: u16,
    pub reference: ReferenceTiming,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClockPrimed {
    pub playback_epoch: u64,
    pub privacy_generation: u64,
    pub accepted_zero_frames: usize,
    pub accepted_at_us: u64,
    pub queue_observed_at_us: u64,
    pub queued_frames: usize,
    pub discarded: Option<QueuedRange>,
}

/// Private speaker/capture priority channel; no provider or parent PCM relay.
#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum ReferenceControl {
    CapturePrepared {
        privacy_generation: u64,
    },
    ClockPrimed {
        prime: ClockPrimed,
    },
    ReferencePrimed {
        playback_epoch: u64,
        analysed_through: u64,
    },
    ClockStarted {
        playback_epoch: u64,
        start_requested_at_us: u64,
        started_at_us: u64,
    },
    Stopped {
        playback_epoch: u64,
        observed_at_us: u64,
    },
}

#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReferenceTiming {
    pub playback_epoch: u64,
    pub accepted_through: u64,
    pub analysed_through: u64,
    /// Software DSP call accounting only; capture/render use separate USB clocks.
    pub capture_blocks_processed: u64,
    /// Retained host reads strictly before the speaker start-call request;
    /// fixed on ClockStarted. Total processed calls are never rebased.
    pub capture_blocks_before_clock_start: u64,
    pub accepted_at_us: u64,
    pub analysed_at_us: u64,
    pub queue_observed_at_us: u64,
    pub queued_frames: usize,
    /// Host timestamp before the start_clock method call; conservative lower
    /// bound, not the physical DAC start. Reads during the call get no exemption.
    pub clock_start_requested_at_us: Option<u64>,
    /// Host timestamp after pcm.start() succeeds, not physical DAC onset.
    pub clock_started_at_us: Option<u64>,
    pub ignored_old_epoch_packets: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReferenceFaultKind {
    InvalidPrime,
    InvalidStart,
    InvalidReference,
    SequenceGap,
    CursorGap,
    PrivacyMismatch,
    ReferenceLate,
    ReferenceBacklog,
    PartialExpired,
    QueueTiming,
    CaptureTiming,
    ClockStopped,
    BarrierTimeout,
    SpeechStarved,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReferenceFault {
    pub reason: ReferenceFaultKind,
    pub at_us: u64,
    pub expected_sequence: u64,
    pub received_sequence: Option<u64>,
    pub received_first_sample: Option<u64>,
    pub timing: ReferenceTiming,
}

impl std::fmt::Display for ReferenceFault {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "reference {:?}: epoch={} accepted={} analysed={} capture_blocks={} before_clock_start={} at_us={}",
            self.reason,
            self.timing.playback_epoch,
            self.timing.accepted_through,
            self.timing.analysed_through,
            self.timing.capture_blocks_processed,
            self.timing.capture_blocks_before_clock_start,
            self.at_us
        )
    }
}
impl std::error::Error for ReferenceFault {}

pub fn encode<T: Serialize>(value: &T) -> io::Result<Vec<u8>> {
    let data = serde_json::to_vec(value).map_err(io::Error::other)?;
    if data.len() > MAX_DATAGRAM_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "runtime message exceeds IPC bound",
        ));
    }
    Ok(data)
}

pub fn decode<T: DeserializeOwned>(data: &[u8]) -> io::Result<T> {
    if data.len() > MAX_DATAGRAM_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "runtime message exceeds IPC bound",
        ));
    }
    serde_json::from_slice(data)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "malformed runtime message"))
}

#[derive(Debug)]
pub struct ReceiveOrder {
    boot: BootId,
    sequence: u64,
    sent_at_us: u64,
}

impl ReceiveOrder {
    pub fn new(boot: BootId) -> Self {
        Self {
            boot,
            sequence: 0,
            sent_at_us: 0,
        }
    }

    pub fn accept<T>(
        &mut self,
        packet: &Envelope<T>,
        now_us: u64,
        max_age_us: u64,
    ) -> io::Result<()> {
        if packet.boot != self.boot
            || packet.sequence <= self.sequence
            || packet.sent_at_us < self.sent_at_us
            || now_us
                .checked_sub(packet.sent_at_us)
                .is_none_or(|age| age >= max_age_us)
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "stale, reordered or wrong-boot runtime message",
            ));
        }
        self.sequence = packet.sequence;
        self.sent_at_us = packet.sent_at_us;
        Ok(())
    }
}
