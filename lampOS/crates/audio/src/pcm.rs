//! Linux ALSA output boundary. This module never changes mixer volume or routing.
//!
//! Use the board's `plug:device_speaker` alias to preserve its soft-volume stage.
//! One supervisor must own the physical speaker; opening an arbitrary ALSA alias
//! is not an exclusive ownership mechanism. Audio writes are nonblocking and at
//! most 10 ms. The speaker worker must pump priority state and `maintain` even
//! while its audio queue is empty. Hardware delay and acoustic latency differ.
use crate::{
    RENDER_RATE, RENDER_SAMPLES, Result,
    ownership::{OwnershipFailure, PlaybackCursor, PlaybackOwnership, QueuedRange},
};
use alsa::{
    Direction, ValueOr,
    pcm::{Access, Format, HwParams, PCM, State},
};
use lamp_interaction::{
    BootId, BoundaryGuard, MonoTime, OutputKind, OutputPermit, Snapshot, ZeroAuthority,
};
use lamp_ipc::monotonic_us;
use serde::Serialize;
use std::io;

/// 40 ms maximum negotiated PCM queue plus 60 ms scheduling/device margin.
const MAX_FINISH_US: u64 = 100_000;

#[derive(Clone, Copy, Debug, Serialize)]
pub struct PcmFormat {
    pub rate: u32,
    pub period_frames: i64,
    pub buffer_frames: i64,
}

#[derive(Clone, Copy, Debug)]
pub struct Written {
    /// Only these accepted frames may be forwarded to the AEC reference path.
    pub frames: usize,
    pub completed_at_us: u64,
    /// ALSA software/hardware queue delay; not a physical speaker timestamp.
    pub queued_frames: i64,
    pub queue_observed_at_us: u64,
    pub first_sample: PlaybackCursor,
    pub end_sample: PlaybackCursor,
}

/// The last ALSA queue observation, not proof of acoustic silence in the room.
#[derive(Clone, Copy, Debug, Serialize)]
pub struct PlaybackStatus {
    pub observed_at_us: u64,
    pub queued_frames: usize,
    pub tracked_authorized_frames: usize,
    pub tracked_speech_frames: usize,
    pub range: QueuedRange,
}

pub struct Speaker {
    pcm: PCM,
    format: PcmFormat,
    guard: BoundaryGuard,
    pending: PlaybackOwnership,
    flush_count: u64,
    last_poll: Option<PlaybackStatus>,
    finish_deadline: Option<MonoTime>,
    discarded: Option<OwnershipFailure>,
    clock_mode: bool,
    clock_started: bool,
    last_discard: Option<QueuedRange>,
    #[cfg(test)]
    post_write_check_at: Option<MonoTime>,
}

impl Speaker {
    pub fn open(device: &str, boot: BootId) -> Result<Self> {
        Self::open_mode(device, boot, false)
    }

    /// Prepare a continuous PCM clock without starting playback. Only accepted
    /// zero-only writes may prime it, then `start_clock` starts explicitly after
    /// the capture worker acknowledges the matching reference epoch. A newer
    /// active admitted owner may retire a fixed accepted tail without resetting
    /// this clock; all new writes and every hard revocation remain strict.
    pub fn open_clock(device: &str, boot: BootId) -> Result<Self> {
        Self::open_mode(device, boot, true)
    }

    fn open_mode(device: &str, boot: BootId, clock_mode: bool) -> Result<Self> {
        let pcm = PCM::new(device, Direction::Playback, true)?;
        let format = {
            let params = HwParams::any(&pcm)?;
            params.set_access(Access::RWInterleaved)?;
            params.set_format(Format::s16())?;
            params.set_channels(1)?;
            params.set_rate(RENDER_RATE, ValueOr::Nearest)?;
            params.set_period_size_near(RENDER_SAMPLES as i64, ValueOr::Nearest)?;
            params.set_buffer_size_near((RENDER_SAMPLES * 4) as i64)?;
            pcm.hw_params(&params)?;
            let current = pcm.hw_params_current()?;
            let format = PcmFormat {
                rate: current.get_rate()?,
                period_frames: current.get_period_size()?,
                buffer_frames: current.get_buffer_size()?,
            };
            if format.rate != RENDER_RATE
                || current.get_channels()? != 1
                || current.get_format()? != Format::s16()
                || format.period_frames <= 0
                || format.period_frames > RENDER_SAMPLES as i64
                || format.buffer_frames <= 0
                || format.buffer_frames > (RENDER_SAMPLES * 4) as i64
                || (clock_mode && format.buffer_frames < (RENDER_SAMPLES * 2) as i64)
            {
                return Err(io::Error::other(format!(
                    "ALSA cannot meet bounded speaker format: {format:?}"
                ))
                .into());
            }
            let software = pcm.sw_params_current()?;
            software.set_start_threshold(if clock_mode {
                // A threshold beyond the buffer disables automatic write-start.
                software.get_boundary()?
            } else {
                format.period_frames
            })?;
            software.set_avail_min(1)?;
            pcm.sw_params(&software)?;
            format
        };
        Ok(Self {
            pcm,
            format,
            guard: BoundaryGuard::new(boot, now()),
            pending: PlaybackOwnership::new(),
            flush_count: 0,
            last_poll: None,
            finish_deadline: None,
            discarded: None,
            clock_mode,
            clock_started: false,
            last_discard: None,
            #[cfg(test)]
            post_write_check_at: None,
        })
    }

    pub fn format(&self) -> PcmFormat {
        self.format
    }

    /// Install the newest authoritative state before handling another audio block.
    /// Stale packets cannot disturb newer audio. A latched control fault flushes it.
    pub fn install(&mut self, snapshot: Snapshot) -> Result<()> {
        self.install_with_maintenance(snapshot, Self::maintain)
    }

    // The injected maintenance observation lets tests model queued PCM while
    // using ALSA's null sink, which itself consumes every frame immediately.
    fn install_with_maintenance(
        &mut self,
        snapshot: Snapshot,
        maintain: impl FnOnce(&mut Self) -> Result<()>,
    ) -> Result<()> {
        if let Err(error) = self.guard.install(now(), snapshot) {
            if self.guard.is_faulted() {
                self.stop()?;
            } else {
                maintain(self)?;
            }
            return Err(error.into());
        }
        maintain(self)
    }

    /// Must run at least once per worker tick, independent of new PCM arrival.
    /// Hard authority loss or an expired tail deadline flushes buffered audio.
    /// Ordinary admitted supersession may instead retire the fixed accepted tail.
    pub fn maintain(&mut self) -> Result<()> {
        self.maintain_available().map(|_| ())
    }

    fn maintain_available(&mut self) -> Result<usize> {
        let (_, available) = self.poll_available()?;
        self.check_pending(now(), available)
    }

    fn check_pending(&mut self, at: MonoTime, available: usize) -> Result<usize> {
        let speech = if self.clock_mode && self.clock_started {
            self.pending.check_clock_owned(at, &mut self.guard)
        } else {
            self.pending.check_owned(at, &mut self.guard)
        };
        if let Err(failure) = speech {
            self.stop()?;
            if self.discarded.is_some() {
                self.guard.invalidate();
                return Err(
                    io::Error::other("unreported playback discard exceeded one receipt").into(),
                );
            }
            self.discarded = Some(failure);
            // Discard/reset invalidates the observation. Let the next worker
            // tick obtain fresh space before accepting a later valid write.
            return Ok(0);
        }
        if let Err(error) = self.pending.check_silence(at, &mut self.guard) {
            self.stop()?;
            return Err(error.into());
        }
        // The sole writer must report the discard before any newer PCM/reference
        // can be accepted. There is one receipt, never an unbounded event queue.
        Ok(if self.discarded.is_some() {
            0
        } else {
            available
        })
    }

    /// Retire authorization only for samples ALSA reports no longer queued.
    /// Call after the last write too, so natural completion can end speaking cues.
    pub fn poll(&mut self) -> Result<PlaybackStatus> {
        self.poll_available().map(|(status, _)| status)
    }

    fn poll_available(&mut self) -> Result<(PlaybackStatus, usize)> {
        self.check_finish_deadline()?;
        // A returning poll must not erase expired/cancelled tail metadata before
        // its hard fences have been checked. Stop still means immediate drop.
        if self.pending.is_retiring_tail() {
            self.check_pending(now(), 0)?;
        }
        // ALSA synchronizes both values with the same hardware observation.
        // Available ring space and total I/O delay are different quantities;
        // never retire ownership using buffer_frames - available.
        let (available, queued) = match self.pcm.avail_delay() {
            Ok((available, delay)) if available >= 0 && delay >= 0 => {
                (available as usize, delay as usize)
            }
            Ok(_) if self.expected_end_state() => (0, 0),
            Ok(_) => {
                self.fault()?;
                return Err(io::Error::other(
                    "negative ALSA availability/delay: playback discontinuity",
                )
                .into());
            }
            Err(error)
                if self.expected_end_state()
                    && (error.errno() == rustix::io::Errno::PIPE.raw_os_error()
                        || error.errno() == rustix::io::Errno::BADFD.raw_os_error()) =>
            {
                (0, 0)
            }
            Err(error) => {
                self.fault()?;
                return Err(error.into());
            }
        };
        self.check_finish_deadline()?;
        if !self.clock_mode || self.clock_started {
            self.pending.retire_to(queued);
        }
        let status = PlaybackStatus {
            observed_at_us: monotonic_us(),
            queued_frames: queued,
            tracked_authorized_frames: self.pending.pending_frames(),
            tracked_speech_frames: self.pending.pending_speech_frames(),
            range: self.pending.queued_range(),
        };
        self.last_poll = Some(status);
        Ok((status, available))
    }

    /// The caller must consume priority control first. Never cache this permission
    /// check or hand a whole utterance to ALSA. Partial writes retain the same owner.
    pub fn try_write(&mut self, permit: OutputPermit, samples: &[i16]) -> Result<Written> {
        self.write(permit, samples, false)
    }

    /// Declare the final block before its post-write poll. Partial writes do not
    /// begin finishing until the entire supplied remainder is actually accepted.
    /// The caller retains this flag and permit across partial writes.
    pub fn try_write_final(&mut self, permit: OutputPermit, samples: &[i16]) -> Result<Written> {
        if self.clock_mode {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "continuous playback ends speech by its final sample cursor, not drain",
            )
            .into());
        }
        self.write(permit, samples, true)
    }

    pub fn zero_authority(&mut self) -> Result<ZeroAuthority> {
        Ok(self.guard.zero_authority(now())?)
    }

    /// No sample argument exists, so a session-only token cannot smuggle speech.
    /// Real ALSA-accepted zeros consume the same 960-frame ledger as speech.
    pub fn try_write_silence(
        &mut self,
        authority: ZeroAuthority,
        frames: usize,
    ) -> Result<Written> {
        if !self.clock_mode {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "zero clock requires open_clock",
            )
            .into());
        }
        self.write_authorized(
            None,
            Some(authority),
            &[0; RENDER_SAMPLES][..frames.min(RENDER_SAMPLES)],
            false,
            frames,
        )
    }

    /// Explicitly start only a complete, acknowledged two-block zero prime.
    /// Callers retain responsibility for the bounded cross-worker ACK deadline.
    pub fn start_clock(&mut self, authority: ZeroAuthority) -> Result<u64> {
        self.guard.check_zero(now(), authority)?;
        if !self.clock_mode
            || self.clock_started
            || self.discarded.is_some()
            || self.pending.pending_speech_frames() != 0
            || self.pending.pending_frames() != RENDER_SAMPLES * 2
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "PCM clock requires exactly 480 accepted zero frames",
            )
            .into());
        }
        if let Err(error) = self.pcm.start() {
            self.fault()?;
            return Err(error.into());
        }
        let started_at_us = monotonic_us();
        if let Err(error) = self.guard.check_zero(now(), authority) {
            self.stop()?;
            return Err(error.into());
        }
        self.clock_started = true;
        Ok(started_at_us)
    }

    pub fn clock_started(&self) -> bool {
        self.clock_started
    }

    pub fn queued_range(&self) -> QueuedRange {
        self.pending.queued_range()
    }

    pub fn speech_retired(&self, final_sample: PlaybackCursor) -> bool {
        self.pending.is_retired(final_sample)
    }

    pub fn is_retiring_tail(&self) -> bool {
        self.pending.is_retiring_tail()
    }

    /// Exactly one cancelled-tail receipt, only after checked ALSA retirement.
    /// This neither completes the old answer nor authorizes a successor write.
    pub fn take_retired_tail(
        &mut self,
    ) -> Result<Option<crate::ownership::CancelledTailRetirement>> {
        self.check_pending(now(), 0)?;
        Ok(self.pending.take_retired_tail())
    }

    pub fn last_discard(&self) -> Option<QueuedRange> {
        self.last_discard
    }

    fn write(
        &mut self,
        permit: OutputPermit,
        samples: &[i16],
        final_block: bool,
    ) -> Result<Written> {
        if self.clock_mode && !self.clock_started {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "PCM reference clock has not started",
            )
            .into());
        }
        self.write_authorized(Some(permit), None, samples, final_block, samples.len())
    }

    fn write_authorized(
        &mut self,
        permit: Option<OutputPermit>,
        zero: Option<ZeroAuthority>,
        samples: &[i16],
        final_block: bool,
        requested: usize,
    ) -> Result<Written> {
        if self.is_finishing() {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "speaker is finishing declared output",
            )
            .into());
        }
        if requested == 0 || requested > RENDER_SAMPLES || requested != samples.len() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "speaker write must contain 1..240 samples",
            )
            .into());
        }
        let available = self.maintain_available()?;
        let allowed = match (permit, zero) {
            (Some(permit), None) => self.guard.check(now(), permit, OutputKind::Speech),
            (None, Some(zero)) => self.guard.check_zero(now(), zero),
            _ => unreachable!("private callers specify exactly one authority"),
        };
        if let Err(error) = allowed {
            // A stale PCM packet is not permission to stop a newer valid answer.
            if self.guard.is_faulted() {
                self.stop()?;
            }
            return Err(error.into());
        }
        let accepted_microphone = if permit.is_some() && self.clock_mode {
            Some(self.guard.zero_authority(now())?)
        } else {
            None
        };
        // The device may consume more PCM between observation and write, making
        // additional space available. Reserve only the ownership credit proven
        // by our observation, so an accepted write can never overrun the ledger.
        let offered = if permit.is_some() && self.pending.is_retiring_tail() {
            // No successor PCM may mix with or conceal the old fixed tail.
            // The caller may keep the clock running through the zero-only API.
            0
        } else {
            self.pending.writable_frames(samples.len(), available)
        };
        let written = if offered == 0 {
            Ok(0)
        } else {
            self.pcm.io_i16()?.writei(&samples[..offered])
        };
        let frames = match written {
            Ok(frames) => frames,
            Err(error)
                if io::Error::from_raw_os_error(error.errno()).kind()
                    == io::ErrorKind::WouldBlock =>
            {
                0
            }
            Err(error) => {
                // An underrun, disconnect or suspend is a discontinuity. Do not
                // recover by replaying queued speech under an old permission.
                self.fault()?;
                return Err(error.into());
            }
        };
        let tracked_before = self.pending.pending_frames();
        let first_sample = self.pending.accepted_cursor();
        let accounting_error = if frames > offered {
            Some("ALSA accepted more frames than offered".to_owned())
        } else if frames > 0 {
            match (permit, zero) {
                (Some(permit), None) => match accepted_microphone {
                    Some(token) => self.pending.push_clock(permit, token, frames),
                    None => self.pending.push(permit, frames),
                },
                (None, Some(zero)) => self.pending.push_silence(zero, frames),
                _ => unreachable!("private callers specify exactly one authority"),
            }
            .err()
            .map(|e| e.to_string())
        } else {
            None
        };
        if let Some(error) = accounting_error {
            let observed_delay = self.last_poll.map(|status| status.queued_frames);
            self.fault()?;
            return Err(io::Error::other(format!(
                "{error}; requested={} offered={offered} accepted={frames} \
                 available={available} tracked_before={tracked_before} \
                 observed_delay={observed_delay:?}",
                samples.len()
            ))
            .into());
        }
        let completed_at_us = monotonic_us();
        let end_sample = self.pending.accepted_cursor();
        // Recheck after writei while all accepted frames still have lineage.
        // A discard keeps its old-epoch receipt; never relabel the write with a
        // newer empty epoch or treat it as normally completed speech.
        #[cfg(test)]
        let post_write_now = self.post_write_check_at.take().unwrap_or_else(now);
        #[cfg(not(test))]
        let post_write_now = now();
        self.check_pending(post_write_now, 0)?;
        if final_block
            && frames == samples.len()
            && end_sample.epoch == self.pending.accepted_cursor().epoch
        {
            self.arm_finish()?;
        }
        let status = self.poll()?;
        Ok(Written {
            frames,
            completed_at_us,
            queued_frames: status.queued_frames as i64,
            queue_observed_at_us: status.observed_at_us,
            first_sample,
            end_sample,
        })
    }

    /// Start an explicitly declared end-of-speech drain. This PCM is nonblocking;
    /// `false` means it still has work. Continue priority control and `maintain`
    /// between `poll_finish` calls. Cancellation still uses immediate `stop`.
    pub fn begin_finish(&mut self) -> Result<bool> {
        if self.clock_mode {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "continuous PCM clock cannot drain per answer",
            )
            .into());
        }
        self.arm_finish()?;
        self.poll_finish()
    }

    /// True means ALSA completed its declared drain and was prepared for a later
    /// utterance. This is a device-queue observation, not measured acoustic silence.
    /// Unexpected underruns remain faults outside this explicitly declared state.
    /// Completion has a 100 ms deadline from final acceptance/declaration; repeated
    /// polls or begin_finish calls cannot extend it. Checks occur around returning
    /// ALSA calls; this is not a hard kernel deadline.
    pub fn poll_finish(&mut self) -> Result<bool> {
        if !self.is_finishing() {
            return Ok(true);
        }
        self.maintain()?;
        if !self.is_finishing() {
            // Priority cancellation already discarded and prepared the device.
            return Ok(true);
        }
        let result = self.pcm.drain();
        self.check_finish_deadline()?;
        let drained = match result {
            Ok(()) => true,
            Err(error)
                if io::Error::from_raw_os_error(error.errno()).kind()
                    == io::ErrorKind::WouldBlock =>
            {
                false
            }
            Err(error)
                if self.expected_end_state()
                    && (error.errno() == rustix::io::Errno::PIPE.raw_os_error()
                        || error.errno() == rustix::io::Errno::BADFD.raw_os_error()) =>
            {
                true
            }
            Err(error) => {
                self.fault()?;
                return Err(error.into());
            }
        };
        if !drained {
            return Ok(false);
        }
        self.pending.clear()?;
        self.finish_deadline = None;
        if let Err(error) = self.pcm.prepare() {
            self.guard.invalidate();
            return Err(error.into());
        }
        self.last_poll = Some(PlaybackStatus {
            observed_at_us: monotonic_us(),
            queued_frames: 0,
            tracked_authorized_frames: 0,
            tracked_speech_frames: 0,
            range: self.pending.queued_range(),
        });
        Ok(true)
    }

    fn expected_end_state(&self) -> bool {
        self.is_finishing() && matches!(self.pcm.state(), State::XRun | State::Setup)
    }

    pub fn is_finishing(&self) -> bool {
        self.finish_deadline.is_some()
    }

    fn arm_finish(&mut self) -> Result<()> {
        if self.finish_deadline.is_none() {
            let Some(deadline) = now().checked_add(MAX_FINISH_US) else {
                self.fault()?;
                return Err(io::Error::other("speaker finish deadline overflow").into());
            };
            self.finish_deadline = Some(deadline);
        }
        Ok(())
    }

    fn check_finish_deadline(&mut self) -> Result<()> {
        if self
            .finish_deadline
            .is_some_and(|deadline| now() >= deadline)
        {
            let mut message = "speaker declared finish exceeded 100 ms".to_owned();
            if let Err(error) = self.fault() {
                message.push_str(&format!("; discard/reset failed: {error}"));
            }
            return Err(io::Error::new(io::ErrorKind::TimedOut, message).into());
        }
        Ok(())
    }

    /// Immediate local discard, never an ALSA drain that finishes old speech.
    pub fn stop(&mut self) -> Result<()> {
        self.last_poll = None;
        self.finish_deadline = None;
        self.clock_started = false;
        if let Err(error) = self.pcm.drop() {
            // A failed discard is not confirmation that queued speech stopped.
            // Keep its authorization metadata and latch further output closed.
            self.guard.invalidate();
            return Err(error.into());
        }
        self.last_discard = Some(self.pending.clear()?);
        self.flush_count = self.flush_count.saturating_add(1);
        if let Err(error) = self.pcm.prepare() {
            self.guard.invalidate();
            return Err(error.into());
        }
        Ok(())
    }

    /// Control transport loss or malformed control input latches the guard closed.
    pub fn fault(&mut self) -> Result<()> {
        self.guard.invalidate();
        self.stop()
    }

    /// Based on the last poll; hardware may drain between polls.
    pub fn is_active(&self) -> bool {
        self.pending.has_pending()
    }

    pub fn flush_count(&self) -> u64 {
        self.flush_count
    }

    /// Consume once, after physical stop and before accepting newer PCM. Losing
    /// the reporting channel is a worker fault, never permission to hide a loss.
    pub fn take_discard(&mut self) -> Option<OwnershipFailure> {
        self.discarded.take()
    }

    pub fn last_poll(&self) -> Option<PlaybackStatus> {
        self.last_poll
    }
}

impl Drop for Speaker {
    fn drop(&mut self) {
        let _ = self.pcm.drop();
    }
}

fn now() -> MonoTime {
    MonoTime::from_micros(monotonic_us())
}

#[cfg(test)]
mod finish_tests {
    use super::*;
    use lamp_interaction::{AdmissionState, AdmittedInput, CaptureState, Controller, Permission};

    fn clock_with_accepted_tail() -> (Speaker, Controller, OutputPermit) {
        let boot = BootId::new([120; 16]).unwrap();
        let mut speaker = Speaker::open_clock("null", boot).unwrap();
        let mut controller = Controller::new(boot, now());
        controller
            .set_microphone_permission(now(), Permission::Allowed)
            .unwrap();
        let until = now().checked_add(500_000).unwrap();
        controller
            .set_capture(now(), CaptureState::RetainingUntil(until))
            .unwrap();
        controller
            .set_admission(now(), AdmissionState::OpenUntil(until))
            .unwrap();
        let owner = controller.admit(now(), AdmittedInput::NewTurn).unwrap();
        controller.input_ended(now(), owner).unwrap();
        let plan = controller
            .plan_output(now(), owner, OutputKind::Speech, None)
            .unwrap();
        let permit = controller.issue_output(now(), plan, 250_000).unwrap();
        speaker
            .install(controller.snapshot(now()).unwrap())
            .unwrap();
        let token = speaker.zero_authority().unwrap();
        speaker.try_write_silence(token, 240).unwrap();
        speaker.try_write_silence(token, 240).unwrap();
        speaker.start_clock(token).unwrap();
        speaker.maintain().unwrap(); // The software null sink consumes its prime.
        assert!(!speaker.pending.has_pending());
        // Inject queued ownership for the null-sink model, not real hardware PCM.
        speaker.pending.push_clock(permit, token, 456).unwrap();
        (speaker, controller, permit)
    }

    #[test]
    fn continuous_supersession_keeps_clock_and_rejects_old_suffix_without_stopping_successor() {
        let (mut speaker, mut controller, old) = clock_with_accepted_tail();
        let before = speaker.queued_range();
        let flushes = speaker.flush_count();
        let new = controller
            .admit(now(), AdmittedInput::Interruption)
            .unwrap();
        speaker
            .install_with_maintenance(controller.snapshot(now()).unwrap(), |speaker| {
                // Model the observation before the null device consumes the tail.
                assert_eq!(speaker.check_pending(now(), 504)?, 504);
                Ok(())
            })
            .unwrap();
        assert!(speaker.is_retiring_tail());
        assert!(speaker.clock_started());
        assert_eq!(speaker.flush_count(), flushes);
        controller.input_ended(now(), new).unwrap();
        let plan = controller
            .plan_output(now(), new, OutputKind::Speech, None)
            .unwrap();
        let permit = controller.issue_output(now(), plan, 100_000).unwrap();
        speaker
            .install_with_maintenance(controller.snapshot(now()).unwrap(), |speaker| {
                speaker.check_pending(now(), 504).map(|_| ())
            })
            .unwrap();
        assert_eq!(
            speaker.try_write(permit, &[7; 240]).unwrap().frames,
            0,
            "retired-but-unreported tail still blocks successor PCM"
        );
        let tail = speaker.take_retired_tail().unwrap().unwrap();
        assert_eq!(tail.owner, old.owner());
        assert_eq!(tail.superseded_by, new);
        assert_eq!(tail.end_sample.frame, before.accepted_through);
        assert_eq!(
            speaker
                .try_write(old, &[8; 23])
                .unwrap_err()
                .downcast_ref::<lamp_interaction::Error>(),
            Some(&lamp_interaction::Error::StaleOwner)
        );
        assert_eq!(speaker.try_write(permit, &[7; 240]).unwrap().frames, 240);
        assert!(speaker.try_write(old, &[8; 23]).is_err());
        assert_eq!(
            speaker.flush_count(),
            flushes,
            "stale queued PCM cannot stop new output"
        );
        assert_eq!(speaker.queued_range().epoch, before.epoch);
        assert!(speaker.clock_started());
    }

    #[test]
    fn continuous_tail_hard_stop_privacy_and_exact_permit_expiry_still_drop() {
        for mode in 0..3 {
            let (mut speaker, mut controller, old) = clock_with_accepted_tail();
            controller
                .admit(now(), AdmittedInput::Interruption)
                .unwrap();
            speaker
                .install_with_maintenance(controller.snapshot(now()).unwrap(), |speaker| {
                    speaker.check_pending(now(), 504).map(|_| ())
                })
                .unwrap();
            let epoch = speaker.queued_range().epoch;
            let flushes = speaker.flush_count();
            match mode {
                0 => speaker.stop().unwrap(),
                1 => {
                    controller
                        .set_microphone_permission(now(), Permission::Denied)
                        .unwrap();
                    speaker
                        .install_with_maintenance(controller.snapshot(now()).unwrap(), |speaker| {
                            speaker.check_pending(now(), 504).map(|_| ())
                        })
                        .unwrap();
                    assert_eq!(
                        speaker.take_discard().unwrap().reason,
                        lamp_interaction::Error::PrivacyClosed
                    );
                }
                _ => {
                    speaker.check_pending(old.expires_at(), 504).unwrap();
                    // State expiry can be the earlier authoritative fence.
                    assert!(matches!(
                        speaker.take_discard().unwrap().reason,
                        lamp_interaction::Error::ExpiredState
                            | lamp_interaction::Error::ExpiredPermit
                    ));
                }
            }
            assert_eq!(speaker.flush_count(), flushes + 1);
            assert!(!speaker.clock_started());
            assert_eq!(speaker.queued_range().epoch, epoch + 1);
            assert!(!speaker.pending.is_retiring_tail());
            assert!(speaker.pending.take_retired_tail().is_none());
        }
    }

    #[test]
    fn snapshot_installation_discard_blocks_new_writes_until_its_receipt_is_taken() {
        let boot = BootId::new([95; 16]).unwrap();
        // Device setup is not the timed authority boundary under this test.
        let mut speaker = Speaker::open("null", boot).unwrap();
        let mut controller = Controller::new(boot, now());
        controller
            .set_microphone_permission(now(), Permission::Allowed)
            .unwrap();
        let until = now().checked_add(500_000).unwrap();
        controller
            .set_capture(now(), CaptureState::RetainingUntil(until))
            .unwrap();
        controller
            .set_admission(now(), AdmissionState::OpenUntil(until))
            .unwrap();
        let old = controller.admit(now(), AdmittedInput::NewTurn).unwrap();
        controller.input_ended(now(), old).unwrap();
        let old_plan = controller
            .plan_output(now(), old, OutputKind::Speech, None)
            .unwrap();
        let old_permit = controller.issue_output(now(), old_plan, 100_000).unwrap();
        speaker
            .install(controller.snapshot(now()).unwrap())
            .unwrap();
        // Model a queued write: the ALSA null sink would otherwise retire it
        // immediately. No physical output is opened by this test.
        speaker.pending.push(old_permit, 240).unwrap();
        let new = controller
            .admit(now(), AdmittedInput::Interruption)
            .unwrap();
        controller.input_ended(now(), new).unwrap();
        let new_plan = controller
            .plan_output(now(), new, OutputKind::Speech, None)
            .unwrap();
        let new_permit = controller.issue_output(now(), new_plan, 100_000).unwrap();
        let flushes = speaker.flush_count();
        speaker
            .install_with_maintenance(controller.snapshot(now()).unwrap(), |speaker| {
                // A fresh observation still sees all 240 old frames outstanding.
                assert_eq!(speaker.check_pending(now(), 720)?, 0);
                Ok(())
            })
            .unwrap();
        assert_eq!(speaker.flush_count(), flushes + 1);
        assert!(!speaker.pending.has_pending());
        assert_eq!(
            speaker
                .try_write(new_permit, &[1; RENDER_SAMPLES])
                .unwrap()
                .frames,
            0
        );
        let receipt = speaker.take_discard().unwrap();
        assert_eq!(receipt.owner, old);
        assert_eq!(receipt.reason, lamp_interaction::Error::StaleOwner);
        assert_eq!(receipt.queued_frames, 240);
        assert!(!receipt.other_owners);
        assert!(speaker.take_discard().is_none());
        assert_eq!(
            speaker
                .try_write(new_permit, &[1; RENDER_SAMPLES])
                .unwrap()
                .frames,
            RENDER_SAMPLES
        );
    }

    #[test]
    fn expired_finish_faults_even_with_no_remaining_pcm_ledger() {
        let boot = BootId::new([93; 16]).unwrap();
        let mut speaker = Speaker::open("null", boot).unwrap();
        assert!(!speaker.pending.has_pending());
        // Inject an elapsed deadline rather than sleeping or opening hardware.
        speaker.finish_deadline = Some(MonoTime::from_micros(monotonic_us().saturating_sub(1)));
        let flushes = speaker.flush_count();
        let error = speaker.poll_finish().unwrap_err();
        assert_eq!(
            error.downcast_ref::<io::Error>().unwrap().kind(),
            io::ErrorKind::TimedOut
        );
        assert!(speaker.guard.is_faulted());
        assert!(!speaker.is_finishing());
        assert_eq!(speaker.flush_count(), flushes + 1);
    }

    #[test]
    fn repeated_finish_declaration_cannot_renew_its_original_deadline() {
        let boot = BootId::new([94; 16]).unwrap();
        let mut speaker = Speaker::open("null", boot).unwrap();
        let original = now().checked_add(50_000).unwrap();
        speaker.finish_deadline = Some(original);
        speaker.arm_finish().unwrap();
        assert_eq!(speaker.finish_deadline, Some(original));
        speaker.stop().unwrap();
        assert_eq!(speaker.finish_deadline, None);
        assert!(
            !speaker.guard.is_faulted(),
            "an ordinary cancel is not a timeout"
        );
    }

    #[test]
    fn accepted_write_crossing_exact_expiry_retains_loss_and_old_epoch_cursors() {
        let boot = BootId::new([96; 16]).unwrap();
        let mut speaker = Speaker::open("null", boot).unwrap();
        let mut controller = Controller::new(boot, now());
        controller
            .set_microphone_permission(now(), Permission::Allowed)
            .unwrap();
        let until = now().checked_add(500_000).unwrap();
        controller
            .set_capture(now(), CaptureState::RetainingUntil(until))
            .unwrap();
        controller
            .set_admission(now(), AdmissionState::OpenUntil(until))
            .unwrap();
        let owner = controller.admit(now(), AdmittedInput::NewTurn).unwrap();
        controller.input_ended(now(), owner).unwrap();
        let plan = controller
            .plan_output(now(), owner, OutputKind::Speech, None)
            .unwrap();
        let permit = controller.issue_output(now(), plan, 100_000).unwrap();
        speaker
            .install(controller.snapshot(now()).unwrap())
            .unwrap();
        // Only the test's post-write check advances to exact expiry. No sleep,
        // production clock override, or real device is involved.
        speaker.post_write_check_at = Some(permit.expires_at());
        let written = speaker.try_write(permit, &[111; RENDER_SAMPLES]).unwrap();
        assert_eq!(written.frames, RENDER_SAMPLES);
        assert_eq!(written.first_sample.epoch, written.end_sample.epoch);
        assert!(written.end_sample.epoch < speaker.queued_range().epoch);
        let loss = speaker.take_discard().unwrap();
        assert_eq!(loss.owner, owner);
        assert_eq!(loss.reason, lamp_interaction::Error::ExpiredPermit);
        assert_eq!(loss.queued_frames, RENDER_SAMPLES);
        assert_eq!(loss.range.accepted_through, written.end_sample.frame);
        assert!(!speaker.speech_retired(written.end_sample));
    }
}
