//! Deterministic DSP qualification, not a simulation of social turn-taking.
//!
//! Exported fixtures contain exactly the samples used by the in-memory bench.
//! Both paths preload all PCM and compute provenance before processing timing.
//! PCM16 conversions inside the processor remain inside that timing boundary.
use crate::{
    CAPTURE_RATE, CAPTURE_SAMPLES, CaptureBlock, EchoProcessor, RENDER_RATE, RENDER_SAMPLES,
    RenderBlock, Result, float_to_pcm,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{self, BufWriter, Cursor, Read, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::Instant,
};

pub const MIN_MEASURED_BLOCKS: usize = 500;
pub const MAX_MEASURED_BLOCKS: usize = 60_000;
const WARMUP_BLOCKS: usize = 500;
const NEAR_MEASURED_BLOCKS: usize = 500;
const NEAR_TOTAL_BLOCKS: usize = WARMUP_BLOCKS + NEAR_MEASURED_BLOCKS;
// Keep early echo leakage visible instead of discarding it as warm-up. These
// non-overlapping windows cover every one of the first 500 ten-millisecond blocks.
const COLD_WINDOWS: [(usize, usize); 5] = [(0, 25), (25, 50), (50, 100), (100, 200), (200, 500)];
const FIXTURE_ID: &str = "lamp-audio-synthetic-echo-v1";
const MAX_MANIFEST_BYTES: usize = 16_384;
const MAX_WAV_HEADER_BYTES: usize = 4_096;
static TEMP_ID: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct WavAsset {
    pub file: String,
    pub sha256: String,
    pub sample_rate: u32,
    pub frames: usize,
    pub file_bytes: usize,
}

#[derive(Clone, Debug, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct NearEndFixture {
    pub warmup_blocks: usize,
    pub measured_blocks: usize,
    pub delay_ms: u16,
    pub reset_before: bool,
    pub render: WavAsset,
    pub capture: WavAsset,
}

#[derive(Clone, Debug, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct FixtureManifest {
    pub schema_version: u32,
    pub fixture_id: String,
    pub block_ms: u32,
    pub sample_format: String,
    pub channels: u16,
    pub warmup_blocks: usize,
    pub measured_blocks: usize,
    pub delay_ms: u16,
    pub noise_suppression: bool,
    pub render: WavAsset,
    pub capture: WavAsset,
    pub near_end: NearEndFixture,
}

#[derive(Clone, Debug, Serialize, Eq, PartialEq)]
pub struct PcmHashes {
    /// SHA256 of decoded signed PCM16 little-endian bytes, without WAV headers.
    pub render: String,
    pub capture: String,
    pub near_render: String,
    pub near_capture: String,
}

/// Signal energy in an exact cold-start interval, before the steady-state score.
/// A synthetic echo reduction is not a voice-quality or double-talk metric.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct ColdQualityWindow {
    pub start_ms: usize,
    pub end_ms_exclusive: usize,
    pub frames: usize,
    pub input_rms_pcm: f64,
    pub output_rms_pcm: f64,
    /// None for zero input: silence cannot establish preservation or rejection.
    pub output_input_rms_ratio: Option<f64>,
    /// None if either energy is zero; do not publish infinity or clamp it to a score.
    pub attenuation_db: Option<f64>,
}

#[derive(Clone, Copy, Default)]
struct WindowEnergy {
    frames: usize,
    input: f64,
    output: f64,
}

#[derive(Default)]
struct ColdQuality([WindowEnergy; COLD_WINDOWS.len()]);

impl ColdQuality {
    fn observe(&mut self, block: usize, input: &CaptureBlock, output: &CaptureBlock) {
        if let Some(index) = COLD_WINDOWS
            .iter()
            .position(|&(start, end)| (start..end).contains(&block))
        {
            self.0[index].frames += CAPTURE_SAMPLES;
            self.0[index].input += energy(input);
            self.0[index].output += energy(output);
        }
    }

    fn report(self) -> [ColdQualityWindow; COLD_WINDOWS.len()] {
        std::array::from_fn(|index| {
            let (start, end) = COLD_WINDOWS[index];
            let window = self.0[index];
            // Only a completed benchmark publishes a report. A programming error
            // that skipped cold blocks must not become a reassuring empty score.
            assert_eq!(window.frames, (end - start) * CAPTURE_SAMPLES);
            ColdQualityWindow {
                start_ms: start * 10,
                end_ms_exclusive: end * 10,
                frames: window.frames,
                input_rms_pcm: (window.input / window.frames as f64).sqrt(),
                output_rms_pcm: (window.output / window.frames as f64).sqrt(),
                output_input_rms_ratio: (window.input > 0.0)
                    .then(|| (window.output / window.input).sqrt()),
                attenuation_db: (window.input > 0.0 && window.output > 0.0)
                    .then(|| 10.0 * (window.input / window.output).log10()),
            }
        })
    }
}

#[derive(Debug, Serialize)]
pub struct DspReport {
    pub status: &'static str,
    pub processor: &'static str,
    pub timing_scope: &'static str,
    pub input_source: &'static str,
    pub fixture_manifest_sha256: Option<String>,
    pub fixture_manifest: Option<FixtureManifest>,
    pub input_pcm_sha256: PcmHashes,
    pub target_os: &'static str,
    pub target_arch: &'static str,
    pub measured_blocks: usize,
    pub warmup_blocks: usize,
    pub near_end_measured_blocks: usize,
    pub near_end_warmup_blocks: usize,
    pub initialization_us: u128,
    pub first_block_us: f64,
    pub processing_p50_us: f64,
    pub processing_p95_us: f64,
    pub processing_p99_us: f64,
    pub processing_max_us: f64,
    pub blocks_over_10_ms: usize,
    pub realtime_factor: f64,
    /// These include all five seconds excluded from the steady-state fields below.
    pub cold_start_echo: [ColdQualityWindow; COLD_WINDOWS.len()],
    pub cold_start_near_end_with_silent_render: [ColdQualityWindow; COLD_WINDOWS.len()],
    pub synthetic_echo_reduction_db: f64,
    pub near_end_with_silent_render_output_input_rms_ratio: f64,
}

/// Only validated/generated bounded input can construct this type. No disk I/O
/// or fixture generation occurs inside `benchmark_loaded`'s processing timers.
#[derive(Debug)]
pub struct LoadedFixture {
    measured_blocks: usize,
    render: Vec<i16>,
    capture: Vec<i16>,
    near_render: Vec<i16>,
    near_capture: Vec<i16>,
    manifest: Option<FixtureManifest>,
    manifest_sha256: Option<String>,
    hashes: PcmHashes,
}

impl LoadedFixture {
    pub fn manifest(&self) -> Option<&FixtureManifest> {
        self.manifest.as_ref()
    }
    pub fn hashes(&self) -> &PcmHashes {
        &self.hashes
    }
    pub fn render_samples(&self) -> &[i16] {
        &self.render
    }
    pub fn capture_samples(&self) -> &[i16] {
        &self.capture
    }
    pub fn near_render_samples(&self) -> &[i16] {
        &self.near_render
    }
    pub fn near_capture_samples(&self) -> &[i16] {
        &self.near_capture
    }
}

/// Pure source fixture: band-limited deterministic noise, separate from voice assets.
fn source(sample_at_8k: f64) -> f32 {
    if sample_at_8k < 0.0 {
        return 0.0;
    }
    fn noise(index: u64) -> f32 {
        let mut x = index.wrapping_add(0x9e3779b97f4a7c15);
        x = (x ^ (x >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
        x = (x ^ (x >> 27)).wrapping_mul(0x94d049bb133111eb);
        ((x ^ (x >> 31)) >> 40) as f32 / 8_388_608.0 - 1.0
    }
    let i = sample_at_8k.floor() as u64;
    let f = sample_at_8k.fract() as f32;
    (noise(i) * (1.0 - f) + noise(i + 1) * f) * 0.22
}

pub fn synthetic_echo_block(block: usize) -> (RenderBlock, CaptureBlock) {
    let render = std::array::from_fn(|offset| {
        float_to_pcm(source(
            (block * RENDER_SAMPLES + offset) as f64 * 8_000.0 / f64::from(RENDER_RATE),
        ))
    });
    let capture = std::array::from_fn(|offset| {
        let t = (block * CAPTURE_SAMPLES + offset) as f64 / f64::from(CAPTURE_RATE);
        // Direct plus reflected path. Both signals derive from one continuous clock.
        float_to_pcm(0.60 * source((t - 0.070) * 8_000.0) + 0.16 * source((t - 0.077) * 8_000.0))
    });
    (render, capture)
}

fn energy(samples: &[i16]) -> f64 {
    samples.iter().map(|&x| f64::from(x).powi(2)).sum()
}

fn validate_blocks(blocks: usize) -> Result<()> {
    if !(MIN_MEASURED_BLOCKS..=MAX_MEASURED_BLOCKS).contains(&blocks) {
        return Err(invalid("benchmark blocks must be 500..60000"));
    }
    Ok(())
}

fn generated_fixture(blocks: usize) -> Result<LoadedFixture> {
    validate_blocks(blocks)?;
    let total = blocks + WARMUP_BLOCKS;
    let mut render = Vec::new();
    let mut capture = Vec::new();
    render.try_reserve_exact(total * RENDER_SAMPLES)?;
    capture.try_reserve_exact(total * CAPTURE_SAMPLES)?;
    for i in 0..total {
        let (render_block, capture_block) = synthetic_echo_block(i);
        render.extend_from_slice(&render_block);
        capture.extend_from_slice(&capture_block);
    }
    let near_capture = capture[..NEAR_TOTAL_BLOCKS * CAPTURE_SAMPLES].to_vec();
    let near_render = vec![0; NEAR_TOTAL_BLOCKS * RENDER_SAMPLES];
    let hashes = input_hashes(&render, &capture, &near_render, &near_capture);
    Ok(LoadedFixture {
        measured_blocks: blocks,
        render,
        capture,
        near_render,
        near_capture,
        manifest: None,
        manifest_sha256: None,
        hashes,
    })
}

/// Reserve a new directory exclusively and publish its manifest only after every
/// WAV has been finalized and hashed. Readers require that commit marker and
/// never accept a partial fixture. Ordinary errors remove this call's incomplete
/// directory. Existing destinations, including empty directories, are untouched.
/// Abrupt process/system termination can leave a directory without a manifest;
/// loading rejects it and this function never overwrites it.
pub fn export_fixture(path: impl AsRef<Path>, measured_blocks: usize) -> Result<FixtureManifest> {
    export_fixture_using(path.as_ref(), measured_blocks, write_wave)
}

fn export_fixture_using(
    path: &Path,
    measured_blocks: usize,
    mut write: impl FnMut(&Path, u32, &[i16]) -> Result<WavAsset>,
) -> Result<FixtureManifest> {
    validate_blocks(measured_blocks)?;
    fs::create_dir(path)?;
    let mut cleanup = IncompleteDirectory(Some(path.to_path_buf()));
    let fixture = generated_fixture(measured_blocks)?;
    let render = write(&path.join("render.wav"), RENDER_RATE, &fixture.render)?;
    let capture = write(&path.join("capture.wav"), CAPTURE_RATE, &fixture.capture)?;
    let near_render = write(
        &path.join("near_render.wav"),
        RENDER_RATE,
        &fixture.near_render,
    )?;
    let near_capture = write(
        &path.join("near_capture.wav"),
        CAPTURE_RATE,
        &fixture.near_capture,
    )?;
    let manifest = FixtureManifest {
        schema_version: 1,
        fixture_id: FIXTURE_ID.to_owned(),
        block_ms: 10,
        sample_format: "pcm_s16le".to_owned(),
        channels: 1,
        warmup_blocks: WARMUP_BLOCKS,
        measured_blocks,
        delay_ms: 70,
        noise_suppression: true,
        render,
        capture,
        near_end: NearEndFixture {
            warmup_blocks: WARMUP_BLOCKS,
            measured_blocks: NEAR_MEASURED_BLOCKS,
            delay_ms: 0,
            reset_before: true,
            render: near_render,
            capture: near_capture,
        },
    };
    let bytes = serde_json::to_vec_pretty(&manifest)?;
    write_new_atomic(&path.join("manifest.json"), &bytes)?;
    cleanup.0 = None;
    Ok(manifest)
}

fn write_wave(path: &Path, sample_rate: u32, samples: &[i16]) -> Result<WavAsset> {
    let mut file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(path)?;
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    {
        let mut buffered = BufWriter::new(&mut file);
        let mut writer = hound::WavWriter::new(&mut buffered, spec)?;
        for &sample in samples {
            writer.write_sample(sample)?;
        }
        writer.finalize()?;
        buffered.flush()?;
    }
    file.sync_all()?;
    let file_bytes = usize::try_from(file.metadata()?.len())?;
    let bytes = read_regular_bounded(path, file_bytes)?;
    Ok(WavAsset {
        file: path
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| invalid("fixture filename is not UTF-8"))?
            .to_owned(),
        sha256: sha256(&bytes),
        sample_rate,
        frames: samples.len(),
        file_bytes,
    })
}

/// Validate every byte/hash/count/format before returning preloaded PCM. Filenames
/// are fixed by this schema, so a manifest cannot escape the fixture directory.
/// At most 60,500 main blocks plus 1,000 near-end blocks are decoded. WAV headers
/// have a bounded allowance, and the manifest is at most 16 KiB.
pub fn load_fixture(path: impl AsRef<Path>) -> Result<LoadedFixture> {
    let path = path.as_ref();
    if !fs::symlink_metadata(path)?.file_type().is_dir() {
        return Err(invalid("fixture must be a real directory"));
    }
    let manifest_bytes = read_regular_bounded(&path.join("manifest.json"), MAX_MANIFEST_BYTES)?;
    let manifest: FixtureManifest = serde_json::from_slice(&manifest_bytes)?;
    validate_manifest(&manifest)?;
    let render = load_wave(path, &manifest.render)?;
    let capture = load_wave(path, &manifest.capture)?;
    let near_render = load_wave(path, &manifest.near_end.render)?;
    let near_capture = load_wave(path, &manifest.near_end.capture)?;
    if render.iter().all(|&sample| sample == 0) || capture.iter().all(|&sample| sample == 0) {
        return Err(invalid(
            "main render and capture must contain nonzero signals",
        ));
    }
    if near_render.iter().any(|&sample| sample != 0) {
        return Err(invalid("near-end render fixture must be silent"));
    }
    if near_capture != capture[..NEAR_TOTAL_BLOCKS * CAPTURE_SAMPLES] {
        return Err(invalid(
            "near-end capture must equal the first 1000 main capture blocks",
        ));
    }
    let hashes = input_hashes(&render, &capture, &near_render, &near_capture);
    Ok(LoadedFixture {
        measured_blocks: manifest.measured_blocks,
        render,
        capture,
        near_render,
        near_capture,
        manifest: Some(manifest),
        manifest_sha256: Some(sha256(&manifest_bytes)),
        hashes,
    })
}

fn validate_manifest(manifest: &FixtureManifest) -> Result<()> {
    validate_blocks(manifest.measured_blocks)?;
    if manifest.schema_version != 1
        || manifest.fixture_id != FIXTURE_ID
        || manifest.block_ms != 10
        || manifest.sample_format != "pcm_s16le"
        || manifest.channels != 1
        || manifest.warmup_blocks != WARMUP_BLOCKS
        || manifest.delay_ms != 70
        || !manifest.noise_suppression
        || manifest.near_end.warmup_blocks != WARMUP_BLOCKS
        || manifest.near_end.measured_blocks != NEAR_MEASURED_BLOCKS
        || manifest.near_end.delay_ms != 0
        || !manifest.near_end.reset_before
    {
        return Err(invalid("unsupported or inconsistent DSP fixture settings"));
    }
    let total = manifest.measured_blocks + WARMUP_BLOCKS;
    validate_asset(
        &manifest.render,
        "render.wav",
        RENDER_RATE,
        total * RENDER_SAMPLES,
    )?;
    validate_asset(
        &manifest.capture,
        "capture.wav",
        CAPTURE_RATE,
        total * CAPTURE_SAMPLES,
    )?;
    validate_asset(
        &manifest.near_end.render,
        "near_render.wav",
        RENDER_RATE,
        NEAR_TOTAL_BLOCKS * RENDER_SAMPLES,
    )?;
    validate_asset(
        &manifest.near_end.capture,
        "near_capture.wav",
        CAPTURE_RATE,
        NEAR_TOTAL_BLOCKS * CAPTURE_SAMPLES,
    )?;
    Ok(())
}

fn validate_asset(asset: &WavAsset, name: &str, rate: u32, frames: usize) -> Result<()> {
    let pcm_bytes = frames * 2;
    if asset.file != name
        || asset.sample_rate != rate
        || asset.frames != frames
        || !(pcm_bytes + 44..=pcm_bytes + MAX_WAV_HEADER_BYTES).contains(&asset.file_bytes)
        || asset.sha256.len() != 64
        || !asset
            .sha256
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(invalid(
            "fixture WAV metadata does not match bounded schema",
        ));
    }
    Ok(())
}

fn load_wave(directory: &Path, asset: &WavAsset) -> Result<Vec<i16>> {
    let bytes = read_regular_bounded(&directory.join(&asset.file), asset.file_bytes)?;
    if bytes.len() != asset.file_bytes || sha256(&bytes) != asset.sha256 {
        return Err(invalid("fixture WAV size or SHA256 mismatch"));
    }
    let mut reader = hound::WavReader::new(Cursor::new(bytes))?;
    let spec = reader.spec();
    if spec.channels != 1
        || spec.sample_rate != asset.sample_rate
        || spec.bits_per_sample != 16
        || spec.sample_format != hound::SampleFormat::Int
        || usize::try_from(reader.duration())? != asset.frames
    {
        return Err(invalid("fixture WAV format or frame count mismatch"));
    }
    let mut samples = Vec::new();
    samples.try_reserve_exact(asset.frames)?;
    for sample in reader.samples::<i16>() {
        if samples.len() == asset.frames {
            return Err(invalid("fixture WAV contains excess samples"));
        }
        samples.push(sample?);
    }
    if samples.len() != asset.frames {
        return Err(invalid("fixture WAV is truncated"));
    }
    Ok(samples)
}

fn read_regular_bounded(path: &Path, limit: usize) -> Result<Vec<u8>> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.file_type().is_file() || metadata.len() > u64::try_from(limit)? {
        return Err(invalid("fixture asset is not a bounded regular file"));
    }
    let file = open_same_regular(path, &metadata)?;
    let mut bytes = Vec::new();
    bytes.try_reserve_exact(usize::try_from(metadata.len())?)?;
    file.take(u64::try_from(limit)? + 1)
        .read_to_end(&mut bytes)?;
    let current = fs::symlink_metadata(path)?;
    if bytes.len() > limit
        || bytes.len() != usize::try_from(metadata.len())?
        || !same_file(&metadata, &current)
    {
        return Err(invalid("fixture asset changed or exceeds its bound"));
    }
    Ok(bytes)
}

fn open_same_regular(path: &Path, before: &fs::Metadata) -> Result<File> {
    // A FIFO or symlink swapped in after metadata must not block this reader.
    // Unix is the supported runtime platform; use a safe owned descriptor.
    #[cfg(unix)]
    let file: File = rustix::fs::open(
        path,
        rustix::fs::OFlags::RDONLY
            | rustix::fs::OFlags::CLOEXEC
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::NONBLOCK,
        rustix::fs::Mode::empty(),
    )?
    .into();
    #[cfg(not(unix))]
    let file = File::open(path)?;
    if !same_file(before, &file.metadata()?) {
        return Err(invalid("fixture asset changed before opening"));
    }
    Ok(file)
}

fn same_file(before: &fs::Metadata, after: &fs::Metadata) -> bool {
    if !after.file_type().is_file() || before.len() != after.len() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        before.dev() == after.dev() && before.ino() == after.ino()
    }
    #[cfg(not(unix))]
    {
        before.modified().ok() == after.modified().ok()
    }
}

fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn pcm_hash(samples: &[i16]) -> String {
    let mut hash = Sha256::new();
    let mut bytes = [0_u8; 4_096];
    for samples in samples.chunks(bytes.len() / 2) {
        for (sample, pair) in samples.iter().zip(bytes.chunks_exact_mut(2)) {
            pair.copy_from_slice(&sample.to_le_bytes());
        }
        hash.update(&bytes[..samples.len() * 2]);
    }
    format!("{:x}", hash.finalize())
}

fn input_hashes(
    render: &[i16],
    capture: &[i16],
    near_render: &[i16],
    near_capture: &[i16],
) -> PcmHashes {
    PcmHashes {
        render: pcm_hash(render),
        capture: pcm_hash(capture),
        near_render: pcm_hash(near_render),
        near_capture: pcm_hash(near_capture),
    }
}

/// Legacy CLI/library entry point, preserving its processing and quality metrics.
/// Sample generation and hashing happen before all timed processing.
pub fn benchmark(blocks: usize) -> Result<DspReport> {
    benchmark_loaded(&generated_fixture(blocks)?)
}

pub fn benchmark_fixture(path: impl AsRef<Path>) -> Result<DspReport> {
    benchmark_loaded(&load_fixture(path)?)
}

/// Time render plus capture processing for each 10 ms PCM16 block. Fixture load,
/// decode, sample generation, input hashes, result energy, sorting and near-end
/// quality processing are excluded. NS is Sonora's Moderate default; a V1
/// implementation's default NS must be reported separately, not assumed equal.
pub fn benchmark_loaded(fixture: &LoadedFixture) -> Result<DspReport> {
    let blocks = fixture.measured_blocks;
    let mut timings = Vec::with_capacity(blocks);
    let started = Instant::now();
    let mut processor = EchoProcessor::new(true);
    let initialization_us = started.elapsed().as_micros();
    let mut input_energy = 0.0;
    let mut output_energy = 0.0;
    let mut first_block_us = 0.0;
    let mut cold_echo = ColdQuality::default();
    for (i, (render, capture)) in fixture
        .render
        .chunks_exact(RENDER_SAMPLES)
        .zip(fixture.capture.chunks_exact(CAPTURE_SAMPLES))
        .enumerate()
    {
        let render: &RenderBlock = render.try_into()?;
        let capture: &CaptureBlock = capture.try_into()?;
        let started = Instant::now();
        processor.render(render)?;
        let output = processor.capture(capture, 70)?;
        let elapsed_us = started.elapsed().as_secs_f64() * 1_000_000.0;
        if i == 0 {
            first_block_us = elapsed_us;
        }
        cold_echo.observe(i, capture, &output);
        if i >= WARMUP_BLOCKS {
            timings.push(elapsed_us);
            input_energy += energy(capture);
            output_energy += energy(&output);
        }
    }
    let sum_us: f64 = timings.iter().sum();
    let blocks_over_10_ms = timings.iter().filter(|&&x| x > 10_000.0).count();
    timings.sort_by(f64::total_cmp);
    let percentile = |p: f64| {
        timings[((blocks as f64 * p).ceil() as usize)
            .saturating_sub(1)
            .min(blocks - 1)]
    };
    let mut near_input = 0.0;
    let mut near_output = 0.0;
    let mut cold_near = ColdQuality::default();
    // A new DSP instance, not only emptied input queues. A V1 adapter must use
    // equivalent reinitialization if its reset() does not reset DSP state.
    processor.reset();
    for (i, (render, capture)) in fixture
        .near_render
        .chunks_exact(RENDER_SAMPLES)
        .zip(fixture.near_capture.chunks_exact(CAPTURE_SAMPLES))
        .enumerate()
    {
        let render: &RenderBlock = render.try_into()?;
        let capture: &CaptureBlock = capture.try_into()?;
        processor.render(render)?;
        let output = processor.capture(capture, 0)?;
        cold_near.observe(i, capture, &output);
        if i >= WARMUP_BLOCKS {
            near_input += energy(capture);
            near_output += energy(&output);
        }
    }
    Ok(DspReport {
        status: "synthetic_dsp_qualification_not_acoustic_or_conversation_latency",
        processor: "sonora-0.2.0-aec3-moderate-ns-no-agc",
        timing_scope: "pcm16_render_plus_capture_processing_only",
        input_source: if fixture.manifest.is_some() {
            "validated_fixture_directory"
        } else {
            "generated_in_memory"
        },
        fixture_manifest_sha256: fixture.manifest_sha256.clone(),
        fixture_manifest: fixture.manifest.clone(),
        input_pcm_sha256: fixture.hashes.clone(),
        target_os: std::env::consts::OS,
        target_arch: std::env::consts::ARCH,
        measured_blocks: blocks,
        warmup_blocks: WARMUP_BLOCKS,
        near_end_measured_blocks: NEAR_MEASURED_BLOCKS,
        near_end_warmup_blocks: WARMUP_BLOCKS,
        initialization_us,
        first_block_us,
        processing_p50_us: percentile(0.50),
        processing_p95_us: percentile(0.95),
        processing_p99_us: percentile(0.99),
        processing_max_us: timings[blocks - 1],
        blocks_over_10_ms,
        realtime_factor: sum_us / (blocks as f64 * 10_000.0),
        cold_start_echo: cold_echo.report(),
        cold_start_near_end_with_silent_render: cold_near.report(),
        synthetic_echo_reduction_db: 10.0
            * (input_energy.max(1.0) / output_energy.max(1.0)).log10(),
        near_end_with_silent_render_output_input_rms_ratio: (near_output / near_input.max(1.0))
            .sqrt(),
    })
}

/// Atomically publish a complete JSON report without replacing any existing path.
pub fn write_report_new(path: impl AsRef<Path>, report: &DspReport) -> Result<()> {
    write_new_atomic(path.as_ref(), &serde_json::to_vec_pretty(report)?)
}

fn write_new_atomic(path: &Path, bytes: &[u8]) -> Result<()> {
    if path.file_name().is_none() {
        return Err(invalid("output must name a new file"));
    }
    match fs::symlink_metadata(path) {
        Ok(_) => {
            return Err(
                io::Error::new(io::ErrorKind::AlreadyExists, "output already exists").into(),
            );
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    for _ in 0..128 {
        let candidate = parent.join(format!(
            ".lamp-dsp-{}-{}.partial",
            std::process::id(),
            TEMP_ID.fetch_add(1, Ordering::Relaxed)
        ));
        let mut file = match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&candidate)
        {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.into()),
        };
        let _cleanup = PartialFile(candidate.clone());
        file.write_all(bytes)?;
        file.sync_all()?;
        // Hard-link publication is atomic and fails if the destination exists,
        // unlike rename(), which can replace an existing report on Unix.
        fs::hard_link(&candidate, path)?;
        return Ok(());
    }
    Err(io::Error::new(
        io::ErrorKind::AlreadyExists,
        "cannot reserve a temporary report file",
    )
    .into())
}

struct IncompleteDirectory(Option<PathBuf>);
impl Drop for IncompleteDirectory {
    fn drop(&mut self) {
        if let Some(path) = &self.0 {
            let _ = fs::remove_dir_all(path);
        }
    }
}

struct PartialFile(PathBuf);
impl Drop for PartialFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

fn invalid(message: &'static str) -> Box<dyn std::error::Error + Send + Sync> {
    io::Error::new(io::ErrorKind::InvalidData, message).into()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cold_windows_cover_every_early_frame_and_keep_silence_unscored() {
        let mut quality = ColdQuality::default();
        for block in 0..WARMUP_BLOCKS {
            // Exact boundary changes make shifted, overlapping or missing windows
            // observable. The first window is deliberately silent.
            let amplitude = if block < 25 { 0 } else { 1000 };
            quality.observe(block, &[amplitude; CAPTURE_SAMPLES], &[0; CAPTURE_SAMPLES]);
        }
        // Warm samples must never leak into the cold score.
        quality.observe(
            WARMUP_BLOCKS,
            &[i16::MAX; CAPTURE_SAMPLES],
            &[i16::MAX; CAPTURE_SAMPLES],
        );
        let windows = quality.report();
        assert_eq!(
            windows.iter().map(|window| window.frames).sum::<usize>(),
            80_000
        );
        assert_eq!((windows[0].start_ms, windows[0].end_ms_exclusive), (0, 250));
        assert_eq!(windows[0].input_rms_pcm, 0.0);
        assert_eq!(windows[0].output_input_rms_ratio, None);
        assert_eq!(windows[0].attenuation_db, None);
        for window in &windows[1..] {
            assert_eq!(window.input_rms_pcm, 1000.0);
            assert_eq!(window.output_rms_pcm, 0.0);
            assert_eq!(window.output_input_rms_ratio, Some(0.0));
            assert_eq!(window.attenuation_db, None);
        }
        for pair in windows.windows(2) {
            assert_eq!(pair[0].end_ms_exclusive, pair[1].start_ms);
        }
        assert_eq!(windows.last().unwrap().end_ms_exclusive, 5000);
    }

    #[test]
    fn cold_quality_preserves_amplification_instead_of_clamping_it_to_success() {
        let mut quality = ColdQuality::default();
        for block in 0..WARMUP_BLOCKS {
            quality.observe(block, &[1000; CAPTURE_SAMPLES], &[2000; CAPTURE_SAMPLES]);
        }
        for window in quality.report() {
            assert_eq!(window.output_input_rms_ratio, Some(2.0));
            assert!((window.attenuation_db.unwrap() + 6.020599913279624).abs() < 1e-10);
        }
    }

    #[test]
    fn failed_export_has_no_manifest_and_removes_its_partial_assets() {
        let parent = std::env::temp_dir().join(format!(
            "lamp-dsp-transaction-{}-{}",
            std::process::id(),
            TEMP_ID.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&parent).unwrap();
        let _cleanup = IncompleteDirectory(Some(parent.clone()));
        let destination = parent.join("fixture");
        let mut writes = 0;
        let result = export_fixture_using(&destination, 500, |path, rate, samples| {
            writes += 1;
            assert!(!destination.join("manifest.json").exists());
            assert!(load_fixture(&destination).is_err());
            if writes == 2 {
                return Err(io::Error::other("injected second-WAV write failure").into());
            }
            write_wave(path, rate, samples)
        });
        assert!(result.is_err());
        assert_eq!(writes, 2);
        assert!(!destination.exists());
        assert_eq!(fs::read_dir(parent).unwrap().count(), 0);
    }

    #[cfg(unix)]
    #[test]
    fn file_substitution_is_rejected_including_fifo_on_linux() {
        use std::os::unix::fs::symlink;
        let parent = std::env::temp_dir().join(format!(
            "lamp-dsp-open-race-{}-{}",
            std::process::id(),
            TEMP_ID.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&parent).unwrap();
        let _cleanup = IncompleteDirectory(Some(parent.clone()));
        let path = parent.join("input");
        fs::write(&path, b"original").unwrap();
        let before = fs::symlink_metadata(&path).unwrap();
        fs::rename(&path, parent.join("original-inode")).unwrap();
        // rustix does not expose mkfifoat on Apple platforms. The target Linux
        // gate covers the FIFO race; all Unix hosts cover symlink/inode swaps.
        #[cfg(target_os = "linux")]
        {
            rustix::fs::mkfifoat(
                rustix::fs::CWD,
                &path,
                rustix::fs::Mode::RUSR | rustix::fs::Mode::WUSR,
            )
            .unwrap();
            assert!(open_same_regular(&path, &before).is_err());
            fs::remove_file(&path).unwrap();
        }
        let target = parent.join("target");
        fs::write(&target, b"original").unwrap();
        symlink(&target, &path).unwrap();
        assert!(open_same_regular(&path, &before).is_err());
        fs::remove_file(&path).unwrap();
        fs::rename(&target, &path).unwrap();
        assert!(open_same_regular(&path, &before).is_err());
    }
}
