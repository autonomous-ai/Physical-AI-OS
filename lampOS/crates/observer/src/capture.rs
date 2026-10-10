//! Fixed-capacity callback handoff. This module never opens an audio device.
use rtrb::{Consumer, Producer, RingBuffer};
use serde::Serialize;
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering},
};

pub const QUEUE_PACKETS: usize = 32;
pub const MAX_SAMPLES: usize = 8192;
pub const MAX_CALLBACKS: u64 = 250_000;
pub const REQUIRED_TAIL_CALLBACKS: u64 = 2;
pub const MALFORMED: u64 = 1;
pub const OVERSIZED: u64 = 2;
pub const QUEUE_FULL: u64 = 4;
pub const TIMESTAMP: u64 = 8;
pub const NONFINITE: u64 = 16;
pub const CLIPPING: u64 = 32;
pub const STREAM_ERROR: u64 = 64;
pub const CALLBACK_LIMIT: u64 = 128;
pub const XRUN: u64 = 256;
pub const CLIP_THRESHOLD: f32 = 1.0 - 1.0 / 32768.0;

#[derive(Clone, Copy, Debug, Serialize)]
pub struct Format {
    pub sample_rate: u32,
    pub channels: u16,
}

impl Format {
    pub fn validate(self, seconds: u16) -> std::io::Result<u64> {
        if !(1..=600).contains(&seconds)
            || !(8_000..=192_000).contains(&self.sample_rate)
            || !(1..=8).contains(&self.channels)
        {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "duration must be 1..600 seconds, rate 8000..192000 Hz, channels 1..8",
            ));
        }
        let frames = u64::from(seconds) * u64::from(self.sample_rate);
        if frames * u64::from(self.channels) * 4 > 1024 * 1024 * 1024 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "requested float WAV exceeds the 1 GiB data limit",
            ));
        }
        Ok(frames)
    }
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct Times {
    pub host_monotonic_ns: u64,
    pub capture_stream_ns: u64,
    pub callback_stream_ns: u64,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct PacketInfo {
    pub callback_index: u64,
    pub source_frame_start: u64,
    pub input_frames: u64,
    pub retained_frames: u64,
    pub times: Times,
    pub timestamp_discontinuity: bool,
    pub clipped_samples: u64,
    pub nonfinite_samples: u64,
}

pub struct Packet {
    pub info: PacketInfo,
    pub len: usize,
    pub samples: [f32; MAX_SAMPLES],
}

#[derive(Default)]
pub struct Shared {
    pub done: AtomicBool,
    pub producer_stopped: AtomicBool,
    pub writer_failed: AtomicBool,
    pub written_frames: AtomicU64,
    pub faults: AtomicU64,
    pub callbacks: AtomicU64,
    pub tail_callbacks: AtomicU64,
    pub tail_input_frames: AtomicU64,
    pub tail_first_host_ns: AtomicU64,
    pub tail_last_host_ns: AtomicU64,
    pub tail_last_capture_ns: AtomicU64,
    pub received_frames: AtomicU64,
    pub retained_frames: AtomicU64,
    pub queue_dropped_frames: AtomicU64,
    pub rejected_frames: AtomicU64,
    pub clipped_samples: AtomicU64,
    pub nonfinite_samples: AtomicU64,
    pub nonzero_samples: AtomicU64,
    pub timestamp_discontinuities: AtomicU64,
    pub first_host_ns: AtomicU64,
    pub last_host_ns: AtomicU64,
    pub first_capture_ns: AtomicU64,
    pub last_capture_ns: AtomicU64,
    pub max_host_callback_gap_ns: AtomicU64,
    pub min_callback_frames: AtomicU64,
    pub max_callback_frames: AtomicU64,
    pub stream_errors: AtomicU64,
    pub backend_xruns: AtomicU64,
    pub stream_error_kinds: AtomicU64,
    pub peak_bits: AtomicU32,
}

#[derive(Clone, Debug, Serialize)]
pub struct Snapshot {
    pub done: bool,
    pub faults: u64,
    pub callbacks: u64,
    pub tail_callbacks: u64,
    pub tail_input_frames: u64,
    pub tail_first_host_ns: u64,
    pub tail_last_host_ns: u64,
    pub tail_last_capture_ns: u64,
    pub received_frames: u64,
    pub retained_frames: u64,
    pub written_frames: u64,
    pub queue_dropped_frames: u64,
    pub rejected_frames: u64,
    pub clipped_samples: u64,
    pub nonfinite_samples: u64,
    pub nonzero_samples: u64,
    pub timestamp_discontinuities: u64,
    pub first_host_ns: u64,
    pub last_host_ns: u64,
    pub first_capture_ns: u64,
    pub last_capture_ns: u64,
    pub max_host_callback_gap_ns: u64,
    pub min_callback_frames: u64,
    pub max_callback_frames: u64,
    pub stream_errors: u64,
    pub backend_xruns: u64,
    pub stream_error_kinds: u64,
    pub peak: f32,
    pub writer_failed: bool,
}

impl Shared {
    pub fn snapshot(&self) -> Snapshot {
        let read = |field: &AtomicU64| field.load(Ordering::Acquire);
        Snapshot {
            done: self.done.load(Ordering::Acquire),
            faults: read(&self.faults),
            callbacks: read(&self.callbacks),
            tail_callbacks: read(&self.tail_callbacks),
            tail_input_frames: read(&self.tail_input_frames),
            tail_first_host_ns: read(&self.tail_first_host_ns),
            tail_last_host_ns: read(&self.tail_last_host_ns),
            tail_last_capture_ns: read(&self.tail_last_capture_ns),
            received_frames: read(&self.received_frames),
            retained_frames: read(&self.retained_frames),
            written_frames: read(&self.written_frames),
            queue_dropped_frames: read(&self.queue_dropped_frames),
            rejected_frames: read(&self.rejected_frames),
            clipped_samples: read(&self.clipped_samples),
            nonfinite_samples: read(&self.nonfinite_samples),
            nonzero_samples: read(&self.nonzero_samples),
            timestamp_discontinuities: read(&self.timestamp_discontinuities),
            first_host_ns: read(&self.first_host_ns),
            last_host_ns: read(&self.last_host_ns),
            first_capture_ns: read(&self.first_capture_ns),
            last_capture_ns: read(&self.last_capture_ns),
            max_host_callback_gap_ns: read(&self.max_host_callback_gap_ns),
            min_callback_frames: read(&self.min_callback_frames),
            max_callback_frames: read(&self.max_callback_frames),
            stream_errors: read(&self.stream_errors),
            backend_xruns: read(&self.backend_xruns),
            stream_error_kinds: read(&self.stream_error_kinds),
            peak: f32::from_bits(self.peak_bits.load(Ordering::Acquire)),
            writer_failed: self.writer_failed.load(Ordering::Acquire),
        }
    }

    pub fn note_xrun(&self) {
        self.faults.fetch_or(XRUN, Ordering::Relaxed);
        self.backend_xruns.fetch_add(1, Ordering::Release);
    }

    pub fn note_stream_error(&self, kind: u64) {
        self.faults.fetch_or(STREAM_ERROR, Ordering::Relaxed);
        self.stream_error_kinds.fetch_or(kind, Ordering::Relaxed);
        self.stream_errors.fetch_add(1, Ordering::Release);
    }
}

impl Snapshot {
    pub fn valid_for(&self, expected_frames: u64) -> bool {
        self.done
            && self.faults == 0
            && !self.writer_failed
            && self.received_frames == expected_frames
            && self.retained_frames == expected_frames
            && self.written_frames == expected_frames
            && self.callbacks > 0
            && self.nonzero_samples > 0
            && self.tail_callbacks >= REQUIRED_TAIL_CALLBACKS
    }
}

pub struct Ingress {
    producer: Producer<Packet>,
    shared: Arc<Shared>,
    format: Format,
    target: u64,
    source_frames: u64,
    previous: Option<(Times, u64)>,
}

pub fn channel(
    format: Format,
    seconds: u16,
) -> std::io::Result<(Ingress, Consumer<Packet>, Arc<Shared>)> {
    let target = format.validate(seconds)?;
    let (mut producer, mut consumer) = RingBuffer::new(QUEUE_PACKETS);
    // Touch the fixed queue storage before any real-time callback owns it.
    for _ in 0..QUEUE_PACKETS {
        let packet = Packet {
            info: PacketInfo {
                callback_index: 0,
                source_frame_start: 0,
                input_frames: 0,
                retained_frames: 0,
                times: Times {
                    host_monotonic_ns: 0,
                    capture_stream_ns: 0,
                    callback_stream_ns: 0,
                },
                timestamp_discontinuity: false,
                clipped_samples: 0,
                nonfinite_samples: 0,
            },
            len: 0,
            samples: [0.0; MAX_SAMPLES],
        };
        assert!(producer.push(packet).is_ok());
    }
    for _ in 0..QUEUE_PACKETS {
        assert!(consumer.pop().is_ok());
    }
    let shared = Arc::new(Shared::default());
    let ingress = Ingress {
        producer,
        shared: shared.clone(),
        format,
        target,
        source_frames: 0,
        previous: None,
    };
    Ok((ingress, consumer, shared))
}

impl Ingress {
    fn check_clock(&mut self, times: Times, input_frames: u64) -> bool {
        let mut discontinuity = times.host_monotonic_ns == 0
            || times.capture_stream_ns == 0
            || times.callback_stream_ns < times.capture_stream_ns;
        if let Some((previous, frames)) = self.previous {
            let expected = frames * 1_000_000_000 / u64::from(self.format.sample_rate);
            // Two sample periods tolerate timestamp rounding, not an entire missing callback.
            let tolerance = 2_000_000_000_u64.div_ceil(u64::from(self.format.sample_rate));
            discontinuity |= times
                .capture_stream_ns
                .checked_sub(previous.capture_stream_ns)
                .is_none_or(|delta| delta.abs_diff(expected) > tolerance);
            discontinuity |= times.host_monotonic_ns <= previous.host_monotonic_ns
                || times.callback_stream_ns <= previous.callback_stream_ns;
            self.shared.max_host_callback_gap_ns.fetch_max(
                times
                    .host_monotonic_ns
                    .saturating_sub(previous.host_monotonic_ns),
                Ordering::Relaxed,
            );
        }
        self.previous = Some((times, input_frames));
        if discontinuity {
            self.shared
                .timestamp_discontinuities
                .fetch_add(1, Ordering::Relaxed);
            self.shared.faults.fetch_or(TIMESTAMP, Ordering::Relaxed);
        }
        discontinuity
    }

    /// No allocation, locks, disk I/O, retries, logging or waits in this method.
    pub fn receive<T: Copy>(&mut self, input: &[T], times: Times, convert: fn(T) -> f32) {
        if self.shared.done.load(Ordering::Relaxed) {
            // CoreAudio can report an xrun on its following cycle. Observe two
            // later callbacks and their clocks without retaining extra audio.
            let channels = usize::from(self.format.channels);
            if input.is_empty() || !input.len().is_multiple_of(channels) {
                self.shared.faults.fetch_or(MALFORMED, Ordering::Relaxed);
                return;
            }
            let frames = (input.len() / channels) as u64;
            self.check_clock(times, frames);
            if input.len() > MAX_SAMPLES {
                self.shared.faults.fetch_or(OVERSIZED, Ordering::Relaxed);
            }
            if self.shared.tail_callbacks.load(Ordering::Relaxed) == 0 {
                self.shared
                    .tail_first_host_ns
                    .store(times.host_monotonic_ns, Ordering::Relaxed);
            }
            self.shared
                .tail_input_frames
                .fetch_add(frames, Ordering::Relaxed);
            self.shared
                .tail_last_host_ns
                .store(times.host_monotonic_ns, Ordering::Relaxed);
            self.shared
                .tail_last_capture_ns
                .store(times.capture_stream_ns, Ordering::Relaxed);
            self.shared.tail_callbacks.fetch_add(1, Ordering::Release);
            return;
        }
        let index = self.shared.callbacks.fetch_add(1, Ordering::Relaxed);
        if index >= MAX_CALLBACKS {
            self.shared
                .faults
                .fetch_or(CALLBACK_LIMIT, Ordering::Relaxed);
            self.shared.done.store(true, Ordering::Release);
            return;
        }
        let channels = usize::from(self.format.channels);
        if input.is_empty() || !input.len().is_multiple_of(channels) {
            self.shared.faults.fetch_or(MALFORMED, Ordering::Relaxed);
            return;
        }
        let input_frames = (input.len() / channels) as u64;
        let count = input_frames.min(self.target - self.source_frames);
        let source_frame_start = self.source_frames;
        self.source_frames += count;
        self.shared
            .received_frames
            .store(self.source_frames, Ordering::Relaxed);
        if index == 0 {
            self.shared
                .first_host_ns
                .store(times.host_monotonic_ns, Ordering::Relaxed);
            self.shared
                .first_capture_ns
                .store(times.capture_stream_ns, Ordering::Relaxed);
            self.shared
                .min_callback_frames
                .store(input_frames, Ordering::Relaxed);
        }
        self.shared
            .min_callback_frames
            .fetch_min(input_frames, Ordering::Relaxed);
        self.shared
            .max_callback_frames
            .fetch_max(input_frames, Ordering::Relaxed);
        self.shared
            .last_host_ns
            .store(times.host_monotonic_ns, Ordering::Release);
        self.shared
            .last_capture_ns
            .store(times.capture_stream_ns, Ordering::Relaxed);
        let discontinuity = self.check_clock(times, input_frames);
        if input.len() > MAX_SAMPLES {
            self.shared.faults.fetch_or(OVERSIZED, Ordering::Relaxed);
            self.shared
                .rejected_frames
                .fetch_add(count, Ordering::Relaxed);
        } else {
            let mut packet = Packet {
                info: PacketInfo {
                    callback_index: index,
                    source_frame_start,
                    input_frames,
                    retained_frames: count,
                    times,
                    timestamp_discontinuity: discontinuity,
                    clipped_samples: 0,
                    nonfinite_samples: 0,
                },
                len: count as usize * channels,
                samples: [0.0; MAX_SAMPLES],
            };
            let mut peak = 0.0_f32;
            let mut nonzero = 0;
            for (output, source) in packet.samples[..packet.len].iter_mut().zip(input) {
                let value = convert(*source);
                if !value.is_finite() {
                    packet.info.nonfinite_samples += 1;
                    continue; // Invalid recording; replacement is explicitly counted.
                }
                peak = peak.max(value.abs());
                nonzero += u64::from(value != 0.0);
                packet.info.clipped_samples += u64::from(value.abs() >= CLIP_THRESHOLD);
                *output = value;
            }
            self.shared
                .peak_bits
                .fetch_max(peak.to_bits(), Ordering::Relaxed);
            self.shared
                .nonzero_samples
                .fetch_add(nonzero, Ordering::Relaxed);
            self.shared
                .clipped_samples
                .fetch_add(packet.info.clipped_samples, Ordering::Relaxed);
            self.shared
                .nonfinite_samples
                .fetch_add(packet.info.nonfinite_samples, Ordering::Relaxed);
            if packet.info.clipped_samples > 0 {
                self.shared.faults.fetch_or(CLIPPING, Ordering::Relaxed);
            }
            if packet.info.nonfinite_samples > 0 {
                self.shared.faults.fetch_or(NONFINITE, Ordering::Relaxed);
            }
            if self.producer.push(packet).is_err() {
                self.shared.faults.fetch_or(QUEUE_FULL, Ordering::Relaxed);
                self.shared
                    .queue_dropped_frames
                    .fetch_add(count, Ordering::Relaxed);
            } else {
                self.shared
                    .retained_frames
                    .fetch_add(count, Ordering::Release);
            }
        }
        if self.source_frames == self.target {
            self.shared.done.store(true, Ordering::Release);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn format() -> Format {
        Format {
            sample_rate: 16_000,
            channels: 1,
        }
    }
    fn times(block: u64) -> Times {
        Times {
            host_monotonic_ns: 1_000_000_000 + block * 10_000_000,
            capture_stream_ns: 2_000_000_000 + block * 10_000_000,
            callback_stream_ns: 2_001_000_000 + block * 10_000_000,
        }
    }
    #[test]
    fn validates_duration_format_and_disk_bound() {
        assert!(format().validate(0).is_err());
        assert!(format().validate(601).is_err());
        assert!(
            Format {
                sample_rate: 192_000,
                channels: 8
            }
            .validate(600)
            .is_err()
        );
        assert!(
            Format {
                sample_rate: 48_000,
                channels: 2
            }
            .validate(600)
            .is_ok()
        );
    }
    #[test]
    fn saturation_is_explicit_without_overwriting_old_packets() {
        let (mut input, mut output, state) = channel(format(), 1).unwrap();
        for i in 0..=QUEUE_PACKETS {
            input.receive(&[0.1; 160], times(i as u64), |x| x);
        }
        assert_eq!(state.snapshot().queue_dropped_frames, 160);
        assert_ne!(state.snapshot().faults & QUEUE_FULL, 0);
        for i in 0..QUEUE_PACKETS {
            assert_eq!(output.pop().unwrap().info.callback_index, i as u64);
        }
        assert!(output.pop().is_err());
    }
    #[test]
    fn missing_callback_clock_is_not_a_valid_continuous_recording() {
        let (mut input, _, state) = channel(format(), 1).unwrap();
        input.receive(&[0.1; 160], times(0), |x| x);
        input.receive(&[0.1; 160], times(2), |x| x);
        assert_eq!(state.snapshot().timestamp_discontinuities, 1);
    }
    #[test]
    fn callback_arrival_jitter_does_not_invent_a_capture_gap() {
        let (mut input, _, state) = channel(format(), 1).unwrap();
        input.receive(&[0.1; 160], times(0), |x| x);
        let mut delayed = times(1);
        delayed.host_monotonic_ns += 8_000_000;
        input.receive(&[0.1; 160], delayed, |x| x);
        assert_eq!(state.snapshot().timestamp_discontinuities, 0);
        assert_eq!(state.snapshot().max_host_callback_gap_ns, 18_000_000);
    }
    #[test]
    fn oversized_and_incomplete_callbacks_fail_closed() {
        let (mut input, _, state) = channel(format(), 1).unwrap();
        input.receive(&[0.1; MAX_SAMPLES + 1], times(0), |x| x);
        assert_eq!(state.snapshot().rejected_frames, (MAX_SAMPLES + 1) as u64);
        assert_ne!(state.snapshot().faults & OVERSIZED, 0);
        let (mut stereo, _, state) = channel(
            Format {
                sample_rate: 16_000,
                channels: 2,
            },
            1,
        )
        .unwrap();
        stereo.receive(&[0.1; 3], times(0), |x| x);
        assert_ne!(state.snapshot().faults & MALFORMED, 0);
    }
    #[test]
    fn clipping_and_nonfinite_are_counted_and_invalid() {
        let (mut input, mut output, state) = channel(format(), 1).unwrap();
        input.receive(&[1.0, f32::NAN], times(0), |x| x);
        let packet = output.pop().unwrap();
        assert_eq!(packet.samples[0], 1.0);
        assert_eq!(packet.samples[1], 0.0);
        assert_eq!(state.snapshot().nonfinite_samples, 1);
        assert_eq!(state.snapshot().clipped_samples, 1);
    }
    #[test]
    fn exact_sample_budget_and_completion_require_written_audio() {
        let (mut input, mut output, state) = channel(format(), 1).unwrap();
        for i in 0..100 {
            input.receive(&[0.1; 160], times(i), |x| x);
            let packet = output.pop().unwrap();
            assert_eq!(packet.info.source_frame_start, i * 160);
            state
                .written_frames
                .fetch_add(packet.info.retained_frames, Ordering::Release);
        }
        assert!(!state.snapshot().valid_for(16_000));
        input.receive(&[0.1; 160], times(100), |x| x);
        assert!(!state.snapshot().valid_for(16_000));
        input.receive(&[0.1; 160], times(101), |x| x);
        assert!(state.snapshot().valid_for(16_000));
        assert_eq!(state.snapshot().tail_callbacks, 2);
        assert!(output.pop().is_err());
        assert_eq!(state.snapshot().callbacks, 100);
        state.written_frames.store(15_999, Ordering::Release);
        assert!(!state.snapshot().valid_for(16_000));
    }
    #[test]
    fn last_callback_is_trimmed_to_requested_frame_count() {
        let (mut input, mut output, state) = channel(format(), 1).unwrap();
        for i in 0..3 {
            let t = Times {
                host_monotonic_ns: 1_000_000_000 + i * 400_000_000,
                capture_stream_ns: 2_000_000_000 + i * 400_000_000,
                callback_stream_ns: 2_001_000_000 + i * 400_000_000,
            };
            input.receive(&[0.1; 6400], t, |x| x);
            let packet = output.pop().unwrap();
            assert_eq!(packet.info.retained_frames, if i < 2 { 6400 } else { 3200 });
            state
                .written_frames
                .fetch_add(packet.info.retained_frames, Ordering::Release);
        }
        for i in 3..5 {
            input.receive(
                &[0.1; 6400],
                Times {
                    host_monotonic_ns: 1_000_000_000 + i * 400_000_000,
                    capture_stream_ns: 2_000_000_000 + i * 400_000_000,
                    callback_stream_ns: 2_001_000_000 + i * 400_000_000,
                },
                |x| x,
            );
        }
        assert!(state.snapshot().valid_for(16_000));
        assert!(output.pop().is_err());
    }
    #[test]
    fn late_backend_faults_and_missing_tail_clocks_invalidate_recording() {
        let (mut input, mut output, state) = channel(format(), 1).unwrap();
        for i in 0..100 {
            input.receive(&[0.1; 160], times(i), |x| x);
            output.pop().unwrap();
            state.written_frames.fetch_add(160, Ordering::Release);
        }
        input.receive(&[0.1; 160], times(101), |x| x);
        input.receive(&[0.1; 160], times(102), |x| x);
        assert_eq!(state.snapshot().timestamp_discontinuities, 1);
        assert!(!state.snapshot().valid_for(16_000));
        state.note_xrun();
        assert_eq!(state.snapshot().backend_xruns, 1);
        assert_ne!(state.snapshot().faults & XRUN, 0);
    }
    #[test]
    fn all_zero_and_stream_error_are_not_success() {
        let (mut input, mut output, state) = channel(format(), 1).unwrap();
        for i in 0..100 {
            input.receive(&[0.0; 160], times(i), |x| x);
            output.pop().unwrap();
            state.written_frames.fetch_add(160, Ordering::Release);
        }
        assert!(!state.snapshot().valid_for(16_000));
        state.note_stream_error(1);
        assert_eq!(state.snapshot().stream_errors, 1);
    }
}
