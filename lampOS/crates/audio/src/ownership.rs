//! Bounded authorization metadata for PCM already accepted by a playback device.
//!
//! A newer write cannot hide an older frame's camera dependency or deadline.
//! The speaker writer retires metadata using a fresh nonnegative ALSA delay,
//! checks all remaining permits, and flushes hardware if any hard fence fails.
//! Only an explicitly recorded continuous-clock acceptance may retire after
//! ordinary supersession; strict callers retain the original owner checks.
//! This ledger is not an audio buffer, an acoustic clock, or a timing measurement.
use lamp_interaction::{
    BoundaryGuard, Error as InteractionError, MonoTime, OutputKind, OutputPermit, TurnOwner,
    ZeroAuthority,
};
use serde::{Deserialize, Serialize};
use std::{collections::VecDeque, fmt};

/// Matches the speaker's maximum negotiated PCM buffer: 40 ms at 24 kHz.
/// A one-frame write is the worst case for metadata entry count.
pub const MAX_QUEUED_FRAMES: usize = 960;
/// Wall watchdog, not acoustic latency: 40 ms PCM plus 60 ms scheduling margin.
pub const MAX_CANCELLED_TAIL_US: u64 = 100_000;

#[derive(Clone, Copy, Debug)]
struct Pending {
    authority: FrameAuthority,
    frames: usize,
}

#[derive(Clone, Copy, Debug)]
enum FrameAuthority {
    Speech {
        permit: OutputPermit,
        accepted_microphone: Option<ZeroAuthority>,
    },
    Silence(ZeroAuthority),
}

/// Immutable accepted range cancelled by a newer admitted input. This is not a
/// completed reply. Following zero writes never enlarge or renew this range.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CancelledTailRetirement {
    pub owner: TurnOwner,
    pub superseded_by: TurnOwner,
    pub first_sample: PlaybackCursor,
    pub end_sample: PlaybackCursor,
    pub queued_frames: usize,
    pub speech_frames: usize,
    pub started_at_us: u64,
}

#[derive(Clone, Copy, Debug)]
struct RetiringTail {
    receipt: CancelledTailRetirement,
    permit: OutputPermit,
    microphone: ZeroAuthority,
    permit_deadline: MonoTime,
    deadline: MonoTime,
}

/// Exclusive sample position within one uninterrupted device-queue epoch.
/// A reset never retires an old cursor; it invalidates that entire epoch.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaybackCursor {
    pub epoch: u64,
    pub frame: u64,
}

/// Outstanding accepted frames, not a claim about sound already in the room.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QueuedRange {
    pub epoch: u64,
    pub retired_through: u64,
    pub accepted_through: u64,
}

/// Fixed-size evidence retained before a guard-triggered physical discard.
/// Mixed ownership cannot be attributed safely to only the failing owner.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct OwnershipFailure {
    pub owner: TurnOwner,
    pub reason: InteractionError,
    pub queued_frames: usize,
    pub other_owners: bool,
    pub range: QueuedRange,
}

/// Tracks every still-queued write in FIFO order, including partial front writes.
/// Allocation occurs at construction, not during bounded push/retirement.
#[derive(Debug)]
pub struct PlaybackOwnership {
    queue: VecDeque<Pending>,
    pending_frames: usize,
    epoch: u64,
    accepted: u64,
    retired: u64,
    tail: Option<RetiringTail>,
}

impl Default for PlaybackOwnership {
    fn default() -> Self {
        Self::new()
    }
}

impl PlaybackOwnership {
    pub fn new() -> Self {
        Self {
            queue: VecDeque::with_capacity(MAX_QUEUED_FRAMES),
            pending_frames: 0,
            epoch: 1,
            accepted: 0,
            retired: 0,
            tail: None,
        }
    }

    /// Record exactly the positive frame count accepted by the device, after a
    /// final-boundary permit check. Do not record generated or rejected PCM.
    /// On error this ledger is unchanged; the caller must flush accepted audio
    /// that could not be accounted for instead of continuing without metadata.
    pub fn push(&mut self, permit: OutputPermit, frames: usize) -> Result<(), QueueError> {
        self.push_speech(permit, None, frames)
    }

    /// Continuous-clock acceptance only: retain the privacy token checked at
    /// the original write. A later token must never be substituted for it.
    pub fn push_clock(
        &mut self,
        permit: OutputPermit,
        accepted_microphone: ZeroAuthority,
        frames: usize,
    ) -> Result<(), QueueError> {
        self.push_speech(permit, Some(accepted_microphone), frames)
    }

    fn push_speech(
        &mut self,
        permit: OutputPermit,
        accepted_microphone: Option<ZeroAuthority>,
        frames: usize,
    ) -> Result<(), QueueError> {
        if permit.kind() != OutputKind::Speech {
            return Err(QueueError::NotSpeech);
        }
        if self.tail.is_some() {
            return Err(QueueError::RetiringTail);
        }
        self.push_authority(
            FrameAuthority::Speech {
                permit,
                accepted_microphone,
            },
            frames,
        )
    }

    /// Record only zeros the zero-only device API actually accepted. Both kinds
    /// use the same bounded FIFO; silence cannot hide or renew speech authority.
    pub fn push_silence(&mut self, token: ZeroAuthority, frames: usize) -> Result<(), QueueError> {
        self.push_authority(FrameAuthority::Silence(token), frames)
    }

    fn push_authority(
        &mut self,
        authority: FrameAuthority,
        frames: usize,
    ) -> Result<(), QueueError> {
        if frames == 0 {
            return Err(QueueError::EmptyWrite);
        }
        if frames > MAX_QUEUED_FRAMES - self.pending_frames {
            return Err(QueueError::Full);
        }
        let accepted = self
            .accepted
            .checked_add(frames as u64)
            .ok_or(QueueError::CounterExhausted)?;
        // Every entry owns at least one frame, so this cannot exceed the
        // preallocated entry capacity while the total frame bound holds.
        self.queue.push_back(Pending { authority, frames });
        self.pending_frames += frames;
        self.accepted = accepted;
        Ok(())
    }

    /// Bound the next device write before any PCM can be accepted. Availability
    /// describes writable ring space, not proof that previously written frames
    /// have finished: ALSA delay can also include downstream device latency.
    /// Only `retire_to` grants ownership credit. Zero means ordinary backpressure.
    pub fn writable_frames(&self, requested: usize, available: usize) -> usize {
        requested
            .min(available)
            .min(MAX_QUEUED_FRAMES - self.pending_frames)
    }

    /// Remove metadata for the oldest frames the device reports as consumed.
    /// An unexpectedly larger delay cannot prove any tracked frame was consumed:
    /// retain everything in that case. The caller handles device discontinuities.
    /// Zero delay retires all metadata; it is not proof of acoustic silence.
    pub fn retire_to(&mut self, alsa_delay_frames: usize) {
        let target = self.pending_frames.min(alsa_delay_frames);
        let mut consumed = self.pending_frames - target;
        self.retired += consumed as u64;
        while consumed > 0 {
            let Some(front) = self.queue.front_mut() else {
                // The private ledger invariant makes this unreachable. Keeping
                // the assertion local prevents silently hiding bookkeeping bugs.
                unreachable!("pending PCM frames must have ownership metadata");
            };
            if front.frames > consumed {
                front.frames -= consumed;
                consumed = 0;
            } else {
                consumed -= front.frames;
                self.queue.pop_front();
            }
        }
        self.pending_frames = target;
    }

    /// Every queued lease must still be valid. A failure does not delete evidence
    /// or flush hardware: the sole writer must perform and confirm that discard.
    /// An empty ledger has no queued output to authorize, so this returns Ok.
    pub fn check(&self, now: MonoTime, guard: &mut BoundaryGuard) -> Result<(), InteractionError> {
        self.check_owned(now, guard)
            .map_err(|failure| failure.reason)?;
        self.check_silence(now, guard)
    }

    pub fn check_owned(
        &self,
        now: MonoTime,
        guard: &mut BoundaryGuard,
    ) -> Result<(), OwnershipFailure> {
        for pending in &self.queue {
            let FrameAuthority::Speech { permit, .. } = pending.authority else {
                continue;
            };
            if let Err(reason) = guard.check(now, permit, OutputKind::Speech) {
                return Err(self.failure(permit.owner(), reason));
            }
        }
        Ok(())
    }

    /// Opt-in clock mode only. A newer active admitted input may supersede one
    /// immutable old owner's accepted tail. It does not authorize new writes.
    /// Call before and after retirement observations, and before every write.
    pub fn check_clock_owned(
        &mut self,
        now: MonoTime,
        guard: &mut BoundaryGuard,
    ) -> Result<(), OwnershipFailure> {
        if let Some(tail) = self.tail {
            let owner = tail.receipt.owner;
            guard
                .check_accepted_speech_retirement(now, tail.permit, tail.microphone)
                .map_err(|reason| self.failure(owner, reason))?;
            if now >= tail.permit_deadline {
                return Err(self.failure(owner, InteractionError::ExpiredPermit));
            }
            if now >= tail.deadline {
                return Err(self.failure(owner, InteractionError::InvalidLease));
            }
            for entry in &self.queue {
                if let FrameAuthority::Speech {
                    permit,
                    accepted_microphone,
                } = entry.authority
                {
                    if permit.owner() != owner {
                        return Err(self.failure(owner, InteractionError::StaleOwner));
                    }
                    let microphone = accepted_microphone
                        .ok_or_else(|| self.failure(owner, InteractionError::StaleOwner))?;
                    guard
                        .check_accepted_speech_retirement(now, permit, microphone)
                        .map_err(|reason| self.failure(owner, reason))?;
                }
            }
            return Ok(());
        }

        let failure = match self.check_owned(now, guard) {
            Ok(()) => return Ok(()),
            Err(failure) => failure,
        };
        // OwnerNone, already-ended successor input, mixed owners and callers
        // without acceptance privacy lineage retain the strict discard path.
        if failure.reason != InteractionError::StaleOwner
            || failure.other_owners
            || !guard.state().is_some_and(|state| state.input_active())
        {
            return Err(failure);
        }
        let mut first = None;
        let mut permit_deadline = None;
        for entry in &self.queue {
            if let FrameAuthority::Speech {
                permit,
                accepted_microphone,
            } = entry.authority
            {
                let microphone = accepted_microphone.ok_or(failure)?;
                let successor = guard
                    .check_accepted_speech_retirement(now, permit, microphone)
                    .map_err(|reason| self.failure(failure.owner, reason))?;
                first.get_or_insert((permit, microphone, successor));
                permit_deadline = Some(
                    permit_deadline.map_or(permit.expires_at(), |old: MonoTime| {
                        old.min(permit.expires_at())
                    }),
                );
            }
        }
        let (permit, microphone, superseded_by) = first.ok_or(failure)?;
        let deadline = now
            .checked_add(MAX_CANCELLED_TAIL_US)
            .ok_or_else(|| self.failure(failure.owner, InteractionError::CounterExhausted))?;
        self.tail = Some(RetiringTail {
            receipt: CancelledTailRetirement {
                owner: failure.owner,
                superseded_by,
                first_sample: PlaybackCursor {
                    epoch: self.epoch,
                    frame: self.retired,
                },
                end_sample: self.accepted_cursor(),
                queued_frames: self.pending_frames,
                speech_frames: self.pending_speech_frames(),
                started_at_us: now.as_micros(),
            },
            permit,
            microphone,
            permit_deadline: permit_deadline.ok_or(failure)?,
            deadline,
        });
        Ok(())
    }

    fn failure(&self, owner: TurnOwner, reason: InteractionError) -> OwnershipFailure {
        OwnershipFailure {
            owner,
            reason,
            queued_frames: self.pending_frames,
            other_owners: self.queue.iter().any(|entry| {
                matches!(entry.authority, FrameAuthority::Speech { permit, .. } if permit.owner() != owner)
            }),
            range: self.queued_range(),
        }
    }

    /// Remains true after retirement until its sole receipt has been collected.
    /// Successor speech is blocked; fresh guarded zeros may maintain the clock.
    pub fn is_retiring_tail(&self) -> bool {
        self.tail.is_some()
    }

    /// Call only after a successful `check_clock_owned` around a fresh device
    /// observation. A reset cannot generate this receipt or complete old speech.
    pub fn take_retired_tail(&mut self) -> Option<CancelledTailRetirement> {
        let tail = self.tail?;
        self.is_retired(tail.receipt.end_sample)
            .then(|| self.tail.take().expect("checked tail").receipt)
    }

    /// Session-clock loss is separate from an immutable speech-owner receipt.
    /// The writer must stop on either error; check speech first to retain any
    /// affected answer's identity before a privacy or authority failure flush.
    pub fn check_silence(
        &self,
        now: MonoTime,
        guard: &mut BoundaryGuard,
    ) -> Result<(), InteractionError> {
        for pending in &self.queue {
            if let FrameAuthority::Silence(token) = pending.authority {
                guard.check_zero(now, token)?;
            }
        }
        Ok(())
    }

    /// Clear only after the speaker worker has discarded/reset the device queue.
    /// The preallocated storage is retained for subsequent playback.
    pub fn clear(&mut self) -> Result<QueuedRange, QueueError> {
        let epoch = self
            .epoch
            .checked_add(1)
            .ok_or(QueueError::CounterExhausted)?;
        let discarded = self.queued_range();
        self.queue.clear();
        self.pending_frames = 0;
        self.epoch = epoch;
        self.accepted = 0;
        self.retired = 0;
        self.tail = None;
        Ok(discarded)
    }

    pub fn has_pending(&self) -> bool {
        self.pending_frames != 0
    }

    pub fn pending_frames(&self) -> usize {
        self.pending_frames
    }

    pub fn queued_range(&self) -> QueuedRange {
        QueuedRange {
            epoch: self.epoch,
            retired_through: self.retired,
            accepted_through: self.accepted,
        }
    }

    pub fn accepted_cursor(&self) -> PlaybackCursor {
        PlaybackCursor {
            epoch: self.epoch,
            frame: self.accepted,
        }
    }

    /// Only normal delay-based consumption in the same epoch proves retirement.
    /// Later idle zeros may remain queued when the final speech cursor retires.
    pub fn is_retired(&self, cursor: PlaybackCursor) -> bool {
        cursor.epoch == self.epoch && cursor.frame > 0 && cursor.frame <= self.retired
    }

    pub fn pending_speech_frames(&self) -> usize {
        self.queue
            .iter()
            .filter_map(|entry| match entry.authority {
                FrameAuthority::Speech { .. } => Some(entry.frames),
                FrameAuthority::Silence(_) => None,
            })
            .sum()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum QueueError {
    EmptyWrite,
    NotSpeech,
    Full,
    CounterExhausted,
    RetiringTail,
}

impl fmt::Display for QueueError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::EmptyWrite => "queued playback metadata requires a positive accepted frame count",
            Self::NotSpeech => "queued playback metadata requires a speech permit",
            Self::Full => "queued playback ownership exceeds the 960-frame bound",
            Self::CounterExhausted => "playback sample or epoch counter exhausted",
            Self::RetiringTail => "cancelled accepted tail must retire before new speech",
        })
    }
}

impl std::error::Error for QueueError {}
