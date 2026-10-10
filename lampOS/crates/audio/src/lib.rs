//! Bounded audio processing for the Lamp microphone process.
//!
//! The caller supplies the speaker's actual accepted PCM as render reference.
//! Provider-generated but unplayed audio must never enter this reference path.
//! Capture and render are independent 10 ms streams. Device discontinuities
//! must be surfaced and the processor reset; they must not be hidden as silence.
use sonora::config::{EchoCanceller, NoiseSuppression};
use sonora::{AudioProcessing, Config, StreamConfig};
use std::io;

#[cfg(target_os = "linux")]
pub mod capture;
pub mod ownership;
#[cfg(target_os = "linux")]
pub mod pcm;
pub mod qualification;
#[cfg(unix)]
pub mod replay;

pub const CAPTURE_RATE: u32 = 16_000;
pub const RENDER_RATE: u32 = 24_000;
pub const CAPTURE_SAMPLES: usize = 160;
pub const RENDER_SAMPLES: usize = 240;
pub const BLOCK_MS: u32 = 10;

pub type CaptureBlock = [i16; CAPTURE_SAMPLES];
pub type RenderBlock = [i16; RENDER_SAMPLES];
pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;

/// A single microphone worker owns this processor; no shared mutex or network I/O.
pub struct EchoProcessor {
    inner: AudioProcessing,
    noise_suppression: bool,
    render_input: [f32; RENDER_SAMPLES],
    render_output: [f32; RENDER_SAMPLES],
    capture_input: [f32; CAPTURE_SAMPLES],
    capture_output: [f32; CAPTURE_SAMPLES],
}

impl EchoProcessor {
    pub fn new(noise_suppression: bool) -> Self {
        Self {
            inner: Self::build(noise_suppression),
            noise_suppression,
            render_input: [0.0; RENDER_SAMPLES],
            render_output: [0.0; RENDER_SAMPLES],
            capture_input: [0.0; CAPTURE_SAMPLES],
            capture_output: [0.0; CAPTURE_SAMPLES],
        }
    }

    fn build(noise_suppression: bool) -> AudioProcessing {
        AudioProcessing::builder()
            .config(Config {
                echo_canceller: Some(EchoCanceller::default()),
                noise_suppression: noise_suppression.then(NoiseSuppression::default),
                // Do not silently change mic gain or boost background speech.
                ..Config::default()
            })
            .capture_config(StreamConfig::new(CAPTURE_RATE, 1))
            .render_config(StreamConfig::new(RENDER_RATE, 1))
            .build()
    }

    /// Reinitialize after lost reference/capture samples, device reopen or privacy.
    /// Reset is a cold operation and must be recorded as a discontinuity.
    pub fn reset(&mut self) {
        self.inner = Self::build(self.noise_suppression);
        self.render_input.fill(0.0);
        self.render_output.fill(0.0);
        self.capture_input.fill(0.0);
        self.capture_output.fill(0.0);
    }

    pub fn render(&mut self, samples: &RenderBlock) -> Result<()> {
        pcm_to_float(samples, &mut self.render_input);
        self.inner
            .process_render_f32(&[&self.render_input], &mut [&mut self.render_output])
            .map_err(|e| io::Error::other(format!("AEC render: {e:?}")))?;
        Ok(())
    }

    /// Delay is measured render/capture device latency, not an assumed network lag.
    pub fn capture(
        &mut self,
        samples: &CaptureBlock,
        device_delay_ms: u16,
    ) -> Result<CaptureBlock> {
        if device_delay_ms > 500 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "AEC device delay exceeds 500 ms",
            )
            .into());
        }
        self.inner
            .set_stream_delay_ms(i32::from(device_delay_ms))
            .map_err(|e| io::Error::other(format!("AEC delay: {e:?}")))?;
        pcm_to_float(samples, &mut self.capture_input);
        self.inner
            .process_capture_f32(&[&self.capture_input], &mut [&mut self.capture_output])
            .map_err(|e| io::Error::other(format!("AEC capture: {e:?}")))?;
        if self.capture_output.iter().any(|sample| !sample.is_finite()) {
            return Err(io::Error::other("AEC returned non-finite audio").into());
        }
        Ok(self.capture_output.map(float_to_pcm))
    }
}

fn pcm_to_float<const N: usize>(samples: &[i16; N], output: &mut [f32; N]) {
    for (source, target) in samples.iter().zip(output.iter_mut()) {
        *target = f32::from(*source) / 32768.0;
    }
}

pub fn float_to_pcm(sample: f32) -> i16 {
    // Rust's float-to-int conversion saturates; caller-generated audio is finite.
    (sample.clamp(-1.0, 1.0) * 32768.0).round() as i16
}
