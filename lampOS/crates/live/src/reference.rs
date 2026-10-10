//! Accepted-render continuity and bounded DSP call accounting. These cursors are
//! not physical capture timestamps: the two USB devices have separate clocks.
//! No missing packet is interpreted as silence, and no samples are invented.
use crate::wire::{
    ClockPrimed, ReferenceFault, ReferenceFaultKind as Kind, ReferenceTiming, RenderPayload,
    RenderReference,
};
use lamp_audio::{CAPTURE_RATE, EchoProcessor, RENDER_RATE, RENDER_SAMPLES, RenderBlock};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
pub const PRIME_FRAMES: usize = RENDER_SAMPLES * 2;
const MAX_REFERENCE_AGE_US: u64 = 40_000;
const MAX_PARTIAL_US: u64 = 50_000;
const MAX_AHEAD_BLOCKS: u64 = 8;
const MAX_CAPTURE_BEFORE_START_NOTICE: usize = PRIME_FRAMES / RENDER_SAMPLES;

pub struct ReferenceAssembly {
    timing: ReferenceTiming,
    sequence: u64,
    privacy_generation: u64,
    samples: RenderBlock,
    len: usize,
    partial_since: u64,
    prime_observed_at_us: u64,
    // A restart does not stop the microphone. Keep only the two host read
    // completions permitted by the certified prime until ClockStarted arrives.
    // Classify them against the conservative start-call request bound, never
    // against call completion or message receipt time.
    captures_before_start_notice: [u64; MAX_CAPTURE_BEFORE_START_NOTICE],
    captures_before_start_notice_len: usize,
    last_capture_read_at_us: Option<u64>,
}

impl Default for ReferenceAssembly {
    fn default() -> Self {
        Self {
            timing: ReferenceTiming::default(),
            sequence: 0,
            privacy_generation: 0,
            samples: [0; RENDER_SAMPLES],
            len: 0,
            partial_since: 0,
            prime_observed_at_us: 0,
            captures_before_start_notice: [0; MAX_CAPTURE_BEFORE_START_NOTICE],
            captures_before_start_notice_len: 0,
            last_capture_read_at_us: None,
        }
    }
}

/// Post-write authority checks may physically discard an accepted write. Its
/// old cursors are evidence of loss, never permission to relabel it after reset.
pub fn accepted_write_survived(
    first: lamp_audio::ownership::PlaybackCursor,
    end: lamp_audio::ownership::PlaybackCursor,
    current: lamp_audio::ownership::QueuedRange,
    frames: usize,
) -> std::io::Result<bool> {
    if first.epoch == 0
        || first.epoch != end.epoch
        || frames > RENDER_SAMPLES
        || first.frame.checked_add(frames as u64) != Some(end.frame)
        || end.epoch > current.epoch
        || (end.epoch == current.epoch && end.frame > current.accepted_through)
    {
        return Err(std::io::Error::other(
            "invalid accepted-write cursor accounting",
        ));
    }
    Ok(end.epoch == current.epoch)
}

impl ReferenceAssembly {
    pub fn timing(&self) -> ReferenceTiming {
        self.timing
    }

    pub fn fault(&self, reason: Kind, now: u64) -> ReferenceFault {
        ReferenceFault {
            reason,
            at_us: now,
            expected_sequence: self.sequence.saturating_add(1),
            received_sequence: None,
            received_first_sample: None,
            timing: self.timing,
        }
    }

    pub fn prime(
        &mut self,
        prime: ClockPrimed,
        now: u64,
        privacy: u64,
        echo: &mut EchoProcessor,
    ) -> Result<()> {
        let reset_valid = match (self.timing.playback_epoch, prime.discarded) {
            (0, None) => prime.playback_epoch == 1,
            (epoch, Some(old)) => {
                old.epoch == epoch
                    && old.retired_through <= old.accepted_through
                    && old.accepted_through >= self.timing.accepted_through
                    && epoch.checked_add(1) == Some(prime.playback_epoch)
            }
            _ => false,
        };
        if !reset_valid
            || privacy == 0
            || prime.privacy_generation != privacy
            || prime.accepted_zero_frames != PRIME_FRAMES
            || prime.queued_frames != PRIME_FRAMES
            || prime.accepted_at_us > prime.queue_observed_at_us
            || !fresh(prime.accepted_at_us, now, 100_000)
            || !fresh(prime.queue_observed_at_us, now, 100_000)
        {
            return Err(self.fault(Kind::InvalidPrime, now).into());
        }
        // Only the speaker's certified, actually accepted prime reaches here.
        // This resets acoustic adaptation; silence does not train room response.
        echo.reset();
        for _ in 0..2 {
            echo.render(&[0; RENDER_SAMPLES])?;
        }
        self.timing = ReferenceTiming {
            playback_epoch: prime.playback_epoch,
            accepted_through: PRIME_FRAMES as u64,
            analysed_through: PRIME_FRAMES as u64,
            accepted_at_us: prime.accepted_at_us,
            analysed_at_us: now,
            queue_observed_at_us: prime.queue_observed_at_us,
            queued_frames: prime.queued_frames,
            ..ReferenceTiming::default()
        };
        self.sequence = 0;
        self.privacy_generation = privacy;
        self.prime_observed_at_us = prime.queue_observed_at_us;
        self.captures_before_start_notice.fill(0);
        self.captures_before_start_notice_len = 0;
        self.clear_partial();
        Ok(())
    }

    pub fn started(
        &mut self,
        epoch: u64,
        requested_at: u64,
        completed_at: u64,
        now: u64,
    ) -> Result<()> {
        if epoch == 0
            || epoch != self.timing.playback_epoch
            || self.timing.clock_started_at_us.is_some()
            || requested_at < self.prime_observed_at_us
            || requested_at > completed_at
            || !fresh(requested_at, now, MAX_REFERENCE_AGE_US)
            || !fresh(completed_at, now, MAX_REFERENCE_AGE_US)
        {
            return Err(self.fault(Kind::InvalidStart, now).into());
        }
        let origin = self.captures_before_start_notice[..self.captures_before_start_notice_len]
            .iter()
            .filter(|read_at| **read_at < requested_at)
            .count() as u64;
        if (self.timing.accepted_through / RENDER_SAMPLES as u64)
            .saturating_sub(self.timing.capture_blocks_processed - origin)
            > MAX_AHEAD_BLOCKS
        {
            return Err(self.fault(Kind::ReferenceBacklog, now).into());
        }
        self.timing.capture_blocks_before_clock_start = origin;
        self.timing.clock_start_requested_at_us = Some(requested_at);
        self.timing.clock_started_at_us = Some(completed_at);
        self.captures_before_start_notice.fill(0);
        self.captures_before_start_notice_len = 0;
        Ok(())
    }

    /// Returns false only for a packet from a previous, explicitly discarded
    /// epoch that was already queued on the separate data socket at reset time.
    pub fn accept(
        &mut self,
        reference: RenderReference,
        now: u64,
        privacy: u64,
        echo: &mut EchoProcessor,
    ) -> Result<bool> {
        if reference.playback_epoch > 0 && reference.playback_epoch < self.timing.playback_epoch {
            self.timing.ignored_old_epoch_packets =
                self.timing.ignored_old_epoch_packets.saturating_add(1);
            return Ok(false);
        }
        let count = reference.payload.frames();
        let reason = if reference.playback_epoch == 0
            || reference.playback_epoch != self.timing.playback_epoch
            || count == 0
            || count > RENDER_SAMPLES
        {
            Some(Kind::InvalidReference)
        } else if reference.privacy_generation != self.privacy_generation
            || privacy != self.privacy_generation
        {
            Some(Kind::PrivacyMismatch)
        } else if self.sequence.checked_add(1) != Some(reference.sequence) {
            Some(Kind::SequenceGap)
        } else if reference.first_sample != self.timing.accepted_through {
            Some(Kind::CursorGap)
        } else if reference.queued_frames > 960
            || reference.accepted_at_us < self.timing.accepted_at_us
            || reference.accepted_at_us > reference.queue_observed_at_us
            || !fresh(reference.accepted_at_us, now, MAX_REFERENCE_AGE_US)
            || !fresh(reference.queue_observed_at_us, now, MAX_REFERENCE_AGE_US)
        {
            Some(Kind::QueueTiming)
        } else {
            None
        };
        if let Some(reason) = reason {
            let mut fault = self.fault(reason, now);
            fault.received_sequence = Some(reference.sequence);
            fault.received_first_sample = Some(reference.first_sample);
            return Err(fault.into());
        }
        let accepted = self
            .timing
            .accepted_through
            .checked_add(count as u64)
            .ok_or_else(|| self.fault(Kind::CursorGap, now))?;
        let full_blocks = accepted / RENDER_SAMPLES as u64;
        if full_blocks.saturating_sub(self.running_capture_blocks()) > MAX_AHEAD_BLOCKS {
            return Err(self.fault(Kind::ReferenceBacklog, now).into());
        }
        let zeros = [0; RENDER_SAMPLES];
        let samples = match &reference.payload {
            RenderPayload::Silence { .. } => &zeros[..count],
            RenderPayload::Speech { samples, .. } => samples.as_slice(),
        };
        let mut offset = 0;
        while offset < samples.len() {
            if self.len == 0 {
                self.partial_since = now;
            }
            let copied = (samples.len() - offset).min(RENDER_SAMPLES - self.len);
            self.samples[self.len..self.len + copied]
                .copy_from_slice(&samples[offset..offset + copied]);
            self.len += copied;
            offset += copied;
            if self.len == RENDER_SAMPLES {
                echo.render(&self.samples)?;
                self.timing.analysed_through += RENDER_SAMPLES as u64;
                self.timing.analysed_at_us = now;
                self.clear_partial();
            }
        }
        self.sequence = reference.sequence;
        self.timing.accepted_through = accepted;
        self.timing.accepted_at_us = reference.accepted_at_us;
        self.timing.queue_observed_at_us = reference.queue_observed_at_us;
        self.timing.queued_frames = reference.queued_frames;
        Ok(true)
    }

    /// Call after draining direct reference, immediately before processing every
    /// retained capture block. This never sleeps for reference or pads a gap.
    /// The balance is a software buffer invariant, not oscillator synchronization.
    pub fn before_capture(&self, capture_delayed_frames: i64, now: u64) -> Result<u16> {
        let latest_clock_evidence = self
            .timing
            .accepted_at_us
            .max(self.timing.clock_started_at_us.unwrap_or(0));
        if self.timing.playback_epoch == 0
            || !fresh(latest_clock_evidence, now, MAX_REFERENCE_AGE_US)
            || self.timing.analysed_through / RENDER_SAMPLES as u64 <= self.running_capture_blocks()
            || (self.timing.clock_started_at_us.is_none()
                && self.captures_before_start_notice_len == MAX_CAPTURE_BEFORE_START_NOTICE)
        {
            return Err(self.fault(Kind::ReferenceLate, now).into());
        }
        if self.len > 0 && now.saturating_sub(self.partial_since) >= MAX_PARTIAL_US {
            return Err(self.fault(Kind::PartialExpired, now).into());
        }
        if !(0..=640).contains(&capture_delayed_frames) {
            return Err(self.fault(Kind::QueueTiming, now).into());
        }
        // Nominal-rate interpolation of the last queue observation, explicitly
        // not measured speaker/ADC time or proof of two-USB-clock synchronization.
        let elapsed = now.saturating_sub(
            self.timing
                .queue_observed_at_us
                .max(self.timing.clock_start_requested_at_us.unwrap_or(0)),
        );
        let consumed = (u128::from(elapsed) * u128::from(RENDER_RATE) / 1_000_000)
            .min(usize::MAX as u128) as usize;
        let render_frames = self.timing.queued_frames.saturating_sub(consumed);
        if self.timing.clock_started_at_us.is_some() && render_frames == 0 {
            // An origin offset is not credit for an exhausted observed queue.
            // Fail on nominal starvation; never manufacture a silence block.
            return Err(self.fault(Kind::ReferenceLate, now).into());
        }
        let delay = render_frames as u64 * 1_000 / u64::from(RENDER_RATE)
            + capture_delayed_frames as u64 * 1_000 / u64::from(CAPTURE_RATE);
        Ok(delay as u16)
    }

    pub fn captured(&mut self, read_completed_at_us: u64, now: u64) -> Result<()> {
        if self.timing.playback_epoch == 0
            || read_completed_at_us > now
            || self
                .last_capture_read_at_us
                .is_some_and(|previous| read_completed_at_us <= previous)
        {
            return Err(self.fault(Kind::CaptureTiming, now).into());
        }
        let processed = self
            .timing
            .capture_blocks_processed
            .checked_add(1)
            .ok_or_else(|| self.fault(Kind::CaptureTiming, now))?;
        if self.timing.clock_started_at_us.is_none() {
            if self.captures_before_start_notice_len == MAX_CAPTURE_BEFORE_START_NOTICE {
                return Err(self.fault(Kind::ReferenceLate, now).into());
            }
            self.captures_before_start_notice[self.captures_before_start_notice_len] =
                read_completed_at_us;
            self.captures_before_start_notice_len += 1;
        }
        self.timing.capture_blocks_processed = processed;
        self.last_capture_read_at_us = Some(read_completed_at_us);
        Ok(())
    }

    fn running_capture_blocks(&self) -> u64 {
        // This is a one-time phase origin, not a claim that the USB clocks are
        // synchronized. Total DSP calls remain separately observable and drift
        // after start still consumes the existing bounded reference lead.
        self.timing.capture_blocks_processed - self.timing.capture_blocks_before_clock_start
    }

    fn clear_partial(&mut self) {
        self.samples.fill(0);
        self.len = 0;
        self.partial_since = 0;
    }
}

fn fresh(at: u64, now: u64, maximum: u64) -> bool {
    now.checked_sub(at).is_some_and(|age| age < maximum)
}

#[cfg(test)]
mod tests {
    use super::*;
    use lamp_audio::ownership::QueuedRange;
    #[test]
    fn a_write_discarded_during_its_post_write_check_is_never_new_epoch_reference() {
        use lamp_audio::ownership::PlaybackCursor;
        let first = PlaybackCursor {
            epoch: 1,
            frame: 480,
        };
        let end = PlaybackCursor {
            epoch: 1,
            frame: 720,
        };
        let after = QueuedRange {
            epoch: 2,
            retired_through: 0,
            accepted_through: 480,
        };
        assert!(!accepted_write_survived(first, end, after, 240).unwrap());
        let current = QueuedRange {
            epoch: 1,
            retired_through: 500,
            accepted_through: 720,
        };
        assert!(accepted_write_survived(first, end, current, 240).unwrap());
        assert!(accepted_write_survived(first, end, current, 239).is_err());
        assert!(
            accepted_write_survived(
                first,
                PlaybackCursor {
                    epoch: 2,
                    frame: 720
                },
                after,
                240
            )
            .is_err()
        );
    }
    fn prime(epoch: u64, discarded: Option<QueuedRange>) -> ClockPrimed {
        ClockPrimed {
            playback_epoch: epoch,
            privacy_generation: 2,
            accepted_zero_frames: 480,
            accepted_at_us: 10_000,
            queue_observed_at_us: 10_001,
            queued_frames: 480,
            discarded,
        }
    }
    fn reference(sequence: u64, first: u64, frames: u16) -> RenderReference {
        RenderReference {
            playback_epoch: 1,
            privacy_generation: 2,
            sequence,
            first_sample: first,
            accepted_at_us: 10_100,
            queue_observed_at_us: 10_101,
            queued_frames: 480,
            payload: RenderPayload::Silence { frames },
        }
    }
    #[test]
    fn no_reference_without_certified_prime_and_empty_link_is_not_silence() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        assert!(state.before_capture(0, 10_002).is_err());
        let mut wrong = prime(1, None);
        wrong.accepted_zero_frames = 240;
        assert!(state.prime(wrong, 10_002, 2, &mut echo).is_err());
        assert!(state.prime(prime(1, None), 10_002, 3, &mut echo).is_err());
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        state.started(1, 10_003, 10_003, 10_004).unwrap();
        for index in 0..2 {
            state.before_capture(0, 10_010 + index).unwrap();
            state.captured(10_010 + index, 10_010 + index).unwrap();
        }
        let error = state.before_capture(0, 10_011).unwrap_err();
        assert_eq!(
            error.downcast_ref::<ReferenceFault>().unwrap().reason,
            Kind::ReferenceLate
        );
        assert_eq!(state.timing.analysed_through, 480);
    }
    #[test]
    fn partial_writes_keep_exact_cursor_and_duplicate_overlap_gap_never_reach_dsp() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        state
            .accept(reference(1, 480, 73), 10_102, 2, &mut echo)
            .unwrap();
        assert_eq!(state.len, 73);
        assert_eq!(state.timing.analysed_through, 480);
        for packet in [
            reference(1, 553, 167),
            reference(2, 552, 167),
            reference(3, 553, 167),
        ] {
            assert!(state.accept(packet, 10_102, 2, &mut echo).is_err());
        }
        assert_eq!(state.len, 73);
        state
            .accept(reference(2, 553, 167), 10_102, 2, &mut echo)
            .unwrap();
        assert_eq!(state.timing.analysed_through, 720);
        assert_eq!(state.len, 0);
    }
    #[test]
    fn reset_drops_unplayed_tail_and_delayed_old_packets_cannot_enter_new_epoch() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        state
            .accept(reference(1, 480, 73), 10_102, 2, &mut echo)
            .unwrap();
        state.captured(10_102, 10_102).unwrap();
        let old = QueuedRange {
            epoch: 1,
            retired_through: 100,
            accepted_through: 553,
        };
        let mut reset = prime(2, Some(old));
        reset.accepted_at_us = 10_200;
        reset.queue_observed_at_us = 10_201;
        state.prime(reset, 10_202, 2, &mut echo).unwrap();
        assert_eq!(state.len, 0);
        assert_eq!(state.timing.capture_blocks_processed, 0);
        assert!(
            !state
                .accept(reference(2, 553, 100), 10_203, 2, &mut echo)
                .unwrap()
        );
        assert_eq!(state.timing.accepted_through, 480);
        assert_eq!(state.timing.ignored_old_epoch_packets, 1);
        assert!(state.started(1, 10_204, 10_204, 10_205).is_err());
        state.started(2, 10_204, 10_204, 10_205).unwrap();
    }
    #[test]
    fn excess_reference_backlog_and_nominal_clock_starvation_are_explicit_faults() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        for sequence in 1..=6 {
            state
                .accept(
                    reference(sequence, 480 + (sequence - 1) * 240, 240),
                    10_102,
                    2,
                    &mut echo,
                )
                .unwrap();
        }
        let error = state
            .accept(reference(7, 1920, 240), 10_102, 2, &mut echo)
            .unwrap_err();
        assert_eq!(
            error.downcast_ref::<ReferenceFault>().unwrap().reason,
            Kind::ReferenceBacklog
        );
        assert!(state.before_capture(0, 50_100).is_err());
        assert!(state.before_capture(641, 10_102).is_err());
    }

    fn assert_fault(result: Result<impl Sized>, reason: Kind) {
        let error = match result {
            Ok(_) => panic!("expected {reason:?}"),
            Err(error) => error,
        };
        assert_eq!(
            error.downcast_ref::<ReferenceFault>().unwrap().reason,
            reason
        );
    }

    fn capture(state: &mut ReferenceAssembly, read_at: u64) {
        state.before_capture(0, read_at).unwrap();
        state.captured(read_at, read_at + 1).unwrap();
    }

    #[test]
    fn restart_origin_preserves_the_observed_queue_at_the_second_cancellation() {
        // Model the relevant host/driver chronology from directed-pilot-fvackatg.
        // That run recorded only start completion. The request below is a
        // simulated bound, not a historical measurement or acoustic timestamp.
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        let mut reset = prime(
            2,
            Some(QueuedRange {
                epoch: 1,
                retired_through: 100,
                accepted_through: 480,
            }),
        );
        reset.accepted_at_us = 177_656_668_319;
        reset.queue_observed_at_us = 177_656_668_320;
        state.prime(reset, 177_656_673_021, 2, &mut echo).unwrap();
        capture(&mut state, 177_656_673_103);
        state
            .started(2, 177_656_674_000, 177_656_674_519, 177_656_674_520)
            .unwrap();
        capture(&mut state, 177_656_680_247);
        for block in 3..=256 {
            let accepted_at = 177_659_215_892 - (256 - block) * 10_000;
            let mut packet = reference(block - 2, (block - 1) * 240, 240);
            packet.playback_epoch = 2;
            packet.accepted_at_us = accepted_at;
            packet.queue_observed_at_us = accepted_at + 8;
            packet.queued_frames = 456;
            state
                .accept(packet, accepted_at + 10, 2, &mut echo)
                .unwrap();
            capture(&mut state, accepted_at + 4_048);
        }
        let before = state.timing();
        assert_eq!(before.capture_blocks_processed, 256);
        assert_eq!(before.capture_blocks_before_clock_start, 1);
        assert_eq!(before.analysed_through, 61_440);
        assert_eq!(state.running_capture_blocks(), 255);

        // The last queue observation still has 112 frames at the nominal rate:
        // 456 - floor(14,342 us * 24,000 / 1,000,000). No new render is accepted.
        let at = 177_659_230_242;
        assert_eq!(at - before.queue_observed_at_us, 14_342);
        assert_eq!(
            before.queued_frames
                - ((at - before.queue_observed_at_us) * 24_000 / 1_000_000) as usize,
            112
        );
        assert_eq!(state.before_capture(0, at).unwrap(), 4);
        state.captured(at, at + 1).unwrap();
        assert_eq!(state.timing().capture_blocks_processed, 257);
        assert_eq!(state.timing().accepted_through, before.accepted_through);
        assert_eq!(state.timing().analysed_through, before.analysed_through);
        // This grants one demonstrated phase correction, never recurring credit.
        assert_fault(state.before_capture(0, at + 1), Kind::ReferenceLate);
    }

    #[test]
    fn delayed_start_notice_classifies_read_completion_against_start_request() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        capture(&mut state, 11_000);
        capture(&mut state, 15_000);
        // Arrival is later than both reads; only the first preceded the request.
        state.started(1, 12_000, 12_000, 20_000).unwrap();
        assert_eq!(state.timing().capture_blocks_processed, 2);
        assert_eq!(state.timing().capture_blocks_before_clock_start, 1);
        assert_eq!(state.running_capture_blocks(), 1);
        capture(&mut state, 21_000);
        assert_eq!(state.timing().capture_blocks_processed, 3);
        assert_fault(state.before_capture(0, 21_001), Kind::ReferenceLate);
        assert_eq!(state.timing().analysed_through, 480);
    }

    #[test]
    fn captures_at_or_after_start_request_never_receive_a_late_notice_exemption() {
        for first_read in [12_000, 13_000] {
            let mut state = ReferenceAssembly::default();
            let mut echo = EchoProcessor::new(false);
            state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
            capture(&mut state, first_read);
            capture(&mut state, 15_000);
            state.started(1, 12_000, 12_000, 20_000).unwrap();
            assert_eq!(state.timing().capture_blocks_before_clock_start, 0);
            assert_fault(state.before_capture(0, 20_001), Kind::ReferenceLate);
        }
    }

    #[test]
    fn future_wrong_epoch_and_duplicate_start_do_not_rebase_counters() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        assert_fault(state.started(0, 10_000, 10_000, 10_001), Kind::InvalidStart);
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        capture(&mut state, 11_000);
        assert_fault(state.started(1, 12_000, 12_000, 11_999), Kind::InvalidStart);
        assert_fault(state.started(2, 12_000, 12_000, 12_001), Kind::InvalidStart);
        assert_fault(state.started(1, 9_999, 9_999, 12_001), Kind::InvalidStart);
        assert_eq!(state.timing().clock_started_at_us, None);
        assert_eq!(state.timing().capture_blocks_before_clock_start, 0);
        state.started(1, 12_000, 12_000, 12_001).unwrap();
        capture(&mut state, 13_000);
        for at in [12_000, 14_000] {
            assert_fault(state.started(1, at, at, 14_001), Kind::InvalidStart);
        }
        assert_eq!(state.timing().clock_started_at_us, Some(12_000));
        assert_eq!(state.timing().capture_blocks_before_clock_start, 1);
        assert_eq!(state.timing().capture_blocks_processed, 2);
    }

    #[test]
    fn ambiguous_reads_during_the_start_call_receive_no_exemption() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        capture(&mut state, 11_000);
        capture(&mut state, 13_000);
        state.started(1, 12_000, 18_000, 20_000).unwrap();
        assert_eq!(state.timing().capture_blocks_before_clock_start, 1);
        assert_eq!(state.timing().capture_blocks_processed, 2);
        assert_eq!(state.timing().clock_start_requested_at_us, Some(12_000));
        assert_eq!(state.timing().clock_started_at_us, Some(18_000));
        // Queue aging also uses the earliest possible start bound, not the
        // later completion (which would overstate the remaining queue by 6 ms).
        assert_eq!(state.before_capture(0, 21_000).unwrap(), 11);
        assert_fault(state.before_capture(0, 32_000), Kind::ReferenceLate);
    }

    #[test]
    fn failed_or_late_start_cannot_publish_a_valid_origin_or_readiness() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        capture(&mut state, 11_000);
        for (request, completion, receipt) in [
            (12_001, 12_000, 14_000), // Reversed method-call interval.
            (12_000, 16_000, 15_999), // Completion in the future.
            (9_999, 13_000, 14_000),  // Request before the actual prime.
            (12_000, 50_000, 52_000), // Request is already 40 ms old.
        ] {
            assert_fault(
                state.started(1, request, completion, receipt),
                Kind::InvalidStart,
            );
            assert_eq!(state.timing().clock_start_requested_at_us, None);
            assert_eq!(state.timing().clock_started_at_us, None);
            assert_eq!(state.timing().capture_blocks_before_clock_start, 0);
        }
        // A failed speaker start sends no successful notice. Its absence never
        // grants an origin: only the existing two retained blocks are allowed.
        capture(&mut state, 23_000);
        assert_fault(state.before_capture(0, 24_000), Kind::ReferenceLate);
    }

    #[test]
    fn more_than_two_capture_calls_without_a_start_notice_still_fault() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        capture(&mut state, 11_000);
        capture(&mut state, 12_000);
        // Even extra actual reference cannot extend the bounded unknown phase.
        let mut packet = reference(1, 480, 240);
        packet.accepted_at_us = 12_500;
        packet.queue_observed_at_us = 12_501;
        state.accept(packet, 12_502, 2, &mut echo).unwrap();
        assert_fault(state.before_capture(0, 13_000), Kind::ReferenceLate);
        assert_fault(state.captured(13_000, 13_001), Kind::ReferenceLate);
        assert_eq!(state.timing().capture_blocks_processed, 2);
    }

    #[test]
    fn each_explicit_reset_gets_its_own_bounded_immutable_phase_origin() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        for epoch in 1..=3 {
            let base = epoch * 100_000;
            let old = (epoch > 1).then(|| QueuedRange {
                epoch: epoch - 1,
                retired_through: 100,
                accepted_through: state.timing().accepted_through,
            });
            let mut next = prime(epoch, old);
            next.accepted_at_us = base;
            next.queue_observed_at_us = base + 1;
            state.prime(next, base + 2, 2, &mut echo).unwrap();
            assert_eq!(state.timing().capture_blocks_processed, 0);
            assert_eq!(state.timing().capture_blocks_before_clock_start, 0);
            capture(&mut state, base + 1_000);
            if epoch == 2 {
                capture(&mut state, base + 2_000);
            }
            state
                .started(epoch, base + 3_000, base + 3_000, base + 3_001)
                .unwrap();
            let origin = if epoch == 2 { 2 } else { 1 };
            assert_eq!(state.timing().capture_blocks_before_clock_start, origin);
            capture(&mut state, base + 4_000);
            capture(&mut state, base + 5_000);
            assert_eq!(state.timing().capture_blocks_processed, origin + 2);
            assert_fault(state.before_capture(0, base + 5_001), Kind::ReferenceLate);
        }
    }

    #[test]
    fn capture_clock_drift_cannot_rebase_the_start_origin() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        capture(&mut state, 11_000);
        state.started(1, 12_000, 12_000, 12_001).unwrap();
        capture(&mut state, 13_000);
        for block in 1..=4 {
            let at = 14_000 + block * 1_000;
            let mut packet = reference(block, 480 + (block - 1) * 240, 240);
            packet.accepted_at_us = at;
            packet.queue_observed_at_us = at;
            state.accept(packet, at, 2, &mut echo).unwrap();
            capture(&mut state, at + 1);
        }
        // An independent faster capture clock consumes the remaining lead.
        capture(&mut state, 19_000);
        assert_eq!(state.timing().capture_blocks_before_clock_start, 1);
        assert_eq!(state.timing().capture_blocks_processed, 7);
        assert_fault(state.before_capture(0, 19_001), Kind::ReferenceLate);
    }

    #[test]
    fn phase_origin_does_not_hide_a_truly_exhausted_observed_queue() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        capture(&mut state, 11_000);
        state.started(1, 12_000, 12_000, 12_001).unwrap();
        assert_eq!(state.running_capture_blocks(), 0);
        assert!(state.before_capture(0, 31_999).is_ok());
        // 480 actual prime frames have nominally elapsed, still within 40 ms.
        assert_fault(state.before_capture(0, 32_000), Kind::ReferenceLate);
        assert_eq!(state.timing().accepted_through, 480);
        assert_eq!(state.timing().analysed_through, 480);
    }

    #[test]
    fn origin_classification_cannot_expand_the_eight_block_backlog_bound() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        capture(&mut state, 11_000);
        capture(&mut state, 12_000);
        for sequence in 1..=7 {
            let mut packet = reference(sequence, 480 + (sequence - 1) * 240, 240);
            packet.accepted_at_us = 14_000 + sequence;
            packet.queue_observed_at_us = packet.accepted_at_us;
            state.accept(packet, 15_000, 2, &mut echo).unwrap();
        }
        // Control was delayed behind nine actual reference blocks; excluding
        // both pre-start reads cannot make that excessive backlog acceptable.
        assert_fault(
            state.started(1, 13_000, 13_000, 15_001),
            Kind::ReferenceBacklog,
        );
        assert_eq!(state.timing().clock_started_at_us, None);
        assert_eq!(state.timing().capture_blocks_before_clock_start, 0);
        assert_eq!(state.timing().capture_blocks_processed, 2);
    }

    #[test]
    fn a_delayed_start_notice_may_follow_newer_accepted_reference() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        capture(&mut state, 11_000);
        let mut packet = reference(1, 480, 240);
        packet.accepted_at_us = 13_000;
        packet.queue_observed_at_us = 13_001;
        state.accept(packet, 13_002, 2, &mut echo).unwrap();
        capture(&mut state, 14_000);
        state.started(1, 12_000, 12_000, 15_000).unwrap();
        assert_eq!(state.timing().capture_blocks_before_clock_start, 1);
        assert_eq!(state.timing().accepted_at_us, 13_000);
        assert_eq!(state.timing().clock_started_at_us, Some(12_000));
        assert_eq!(state.running_capture_blocks(), 1);
        assert_eq!(state.timing().analysed_through, 720);
    }

    #[test]
    fn reference_and_start_notice_freshness_remain_strictly_under_40_ms() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        capture(&mut state, 11_000);
        assert_fault(state.started(1, 12_000, 12_000, 52_000), Kind::InvalidStart);
        state.started(1, 12_000, 12_000, 12_001).unwrap();
        let mut packet = reference(1, 480, 240);
        packet.accepted_at_us = 12_000;
        packet.queue_observed_at_us = 12_001;
        assert_fault(
            state.accept(packet, 52_000, 2, &mut echo),
            Kind::QueueTiming,
        );
        assert_fault(state.before_capture(0, 52_000), Kind::ReferenceLate);
        assert_eq!(state.timing().capture_blocks_before_clock_start, 1);
    }

    #[test]
    fn invalid_capture_timestamps_cannot_create_a_start_exemption() {
        let mut state = ReferenceAssembly::default();
        let mut echo = EchoProcessor::new(false);
        state.prime(prime(1, None), 10_002, 2, &mut echo).unwrap();
        assert_fault(state.captured(12_000, 11_000), Kind::CaptureTiming);
        capture(&mut state, 11_000);
        for at in [10_999, 11_000] {
            assert_fault(state.captured(at, 12_000), Kind::CaptureTiming);
        }
        state.started(1, 12_000, 12_000, 12_001).unwrap();
        assert_eq!(state.timing().capture_blocks_processed, 1);
        assert_eq!(state.timing().capture_blocks_before_clock_start, 1);
    }
}
