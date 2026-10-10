//! Local neural speech activity with finite pre-roll and explicit endpointing.
//! This detector does not identify a speaker or an addressee. Its thresholds
//! are initial experiment settings, not socially qualified release defaults.
use lamp_audio::{CAPTURE_RATE, CAPTURE_SAMPLES, CaptureBlock};
use sonora_agc2::vad_wrapper::VoiceActivityDetectorWrapper;
use std::collections::VecDeque;

pub const PRE_ROLL_BLOCKS: usize = 30;
pub const START_BLOCKS: u16 = 6;
pub const END_SILENCE_BLOCKS: u16 = 60;
pub const MAX_UTTERANCE_BLOCKS: u32 = 12_000;

pub struct SpeechProbability {
    vad: VoiceActivityDetectorWrapper,
    buffer: [f32; CAPTURE_SAMPLES],
}

impl Default for SpeechProbability {
    fn default() -> Self {
        Self {
            vad: VoiceActivityDetectorWrapper::new(
                sonora_simd::detect_backend(),
                CAPTURE_RATE as i32,
            ),
            buffer: [0.0; CAPTURE_SAMPLES],
        }
    }
}

impl SpeechProbability {
    pub fn analyze(&mut self, block: &CaptureBlock) -> f32 {
        // Sonora's internal WebRTC VAD takes FloatS16 samples, not [-1, 1].
        for (dst, &src) in self.buffer.iter_mut().zip(block) {
            *dst = f32::from(src);
        }
        self.vad.analyze(&self.buffer)
    }

    pub fn reset(&mut self) {
        *self = Self::default();
    }
}

#[derive(Clone, Debug)]
pub struct ObservedAudio {
    pub sequence: u64,
    pub captured_at_us: u64,
    pub samples: CaptureBlock,
}

#[derive(Debug)]
pub enum Activity {
    Quiet,
    Start(Vec<ObservedAudio>),
    Continue(ObservedAudio),
    End(ObservedAudio),
    Fault(&'static str),
}

pub struct TurnDetector {
    prefix: VecDeque<ObservedAudio>,
    start_count: u16,
    silence_count: u16,
    active_blocks: u32,
    active: bool,
    last: Option<(u64, u64)>,
}

impl Default for TurnDetector {
    fn default() -> Self {
        Self {
            prefix: VecDeque::with_capacity(PRE_ROLL_BLOCKS),
            start_count: 0,
            silence_count: 0,
            active_blocks: 0,
            active: false,
            last: None,
        }
    }
}

impl TurnDetector {
    pub fn push(&mut self, frame: ObservedAudio, probability: f32) -> Activity {
        if !probability.is_finite() || !(0.0..=1.0).contains(&probability) {
            self.reset();
            return Activity::Fault("invalid speech probability");
        }
        if frame.sequence == 0
            || self.last.is_some_and(|(sequence, at)| {
                sequence.checked_add(1) != Some(frame.sequence)
                    || frame.captured_at_us < at
                    || frame.captured_at_us.saturating_sub(at) > 50_000
            })
        {
            self.reset();
            return Activity::Fault("capture discontinuity");
        }
        self.last = Some((frame.sequence, frame.captured_at_us));
        if self.active {
            self.active_blocks += 1;
            if self.active_blocks >= MAX_UTTERANCE_BLOCKS {
                self.reset();
                return Activity::Fault("utterance exceeded 120 second bound");
            }
            self.silence_count = if probability < 0.35 {
                self.silence_count + 1
            } else {
                0
            };
            if self.silence_count >= END_SILENCE_BLOCKS {
                self.active = false;
                self.start_count = 0;
                self.silence_count = 0;
                return Activity::End(frame);
            }
            return Activity::Continue(frame);
        }
        if self.prefix.len() == PRE_ROLL_BLOCKS {
            self.prefix.pop_front();
        }
        self.prefix.push_back(frame);
        self.start_count = if probability >= 0.80 {
            self.start_count + 1
        } else {
            0
        };
        if self.start_count >= START_BLOCKS {
            self.active = true;
            self.active_blocks = self.prefix.len() as u32;
            self.silence_count = 0;
            return Activity::Start(self.prefix.drain(..).collect());
        }
        Activity::Quiet
    }

    pub fn reset(&mut self) {
        self.prefix.clear();
        self.start_count = 0;
        self.silence_count = 0;
        self.active_blocks = 0;
        self.active = false;
        self.last = None;
    }
}
