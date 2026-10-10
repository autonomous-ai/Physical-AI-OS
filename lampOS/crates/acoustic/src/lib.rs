//! Repeatable acoustic fixtures. Rendering is not an end-to-end test result.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    error::Error,
    fs::{self, File},
    io,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    thread,
    time::{Duration, Instant},
};

pub type Result<T> = std::result::Result<T, Box<dyn Error + Send + Sync>>;
pub const RATE: u32 = 16_000;
const MAX_MS: u32 = 60_000;
const RENDER_VERSION: u32 = 1;
static TEMP_ID: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Catalog {
    pub version: u32,
    pub utterances: BTreeMap<String, Speech>,
    pub scenes: Vec<Scene>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Speech {
    pub text: String,
    pub voice: String,
    pub wpm: u16,
    pub voice_revision: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Scene {
    pub id: String,
    pub description: String,
    pub expectation: String,
    pub precondition: String,
    pub trigger: Trigger,
    pub duration_ms: u32,
    pub clips: Vec<Clip>,
    pub noise: Option<Noise>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case", deny_unknown_fields)]
pub enum Trigger {
    SceneStart,
    LampSpeechStarted { delay_ms: u32 },
    LampSpeechEnded { delay_ms: u32 },
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Clip {
    pub utterance: String,
    pub at_ms: u32,
    pub gain_db: f32,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Noise {
    pub kind: NoiseKind,
    pub seed: u32,
    pub rms_dbfs: f32,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NoiseKind {
    Ventilation,
    Keyboard,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Asset {
    pub key: String,
    pub wav_sha256: String,
    pub frames: usize,
    pub sample_rate: u32,
    pub peak: f32,
    pub rms: f32,
    /// Global attenuation used to prevent clipping. Applies to every source.
    pub attenuation: f32,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct SceneAsset {
    pub scene: Scene,
    pub asset: Asset,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct RenderReport {
    pub catalog_sha256: String,
    pub renderer_version: u32,
    pub engine_fingerprint: String,
    pub generated_speech: usize,
    pub reused_speech: usize,
    pub generated_scenes: usize,
    pub reused_scenes: usize,
    pub scenes: Vec<SceneAsset>,
    pub status: String,
    pub limitations: Vec<String>,
}

pub trait Synthesizer {
    fn fingerprint(&self) -> Result<String>;
    fn synthesize(&mut self, speech: &Speech, destination: &Path) -> Result<()>;
}

pub struct MacSay;

impl Synthesizer for MacSay {
    fn fingerprint(&self) -> Result<String> {
        if !cfg!(target_os = "macos") {
            return Err("Mac speech rendering requires macOS".into());
        }
        let build = Command::new("/usr/bin/sw_vers")
            .arg("-buildVersion")
            .output()?;
        if !build.status.success() {
            return Err("could not identify the macOS speech engine".into());
        }
        let executable = hash(&fs::read("/usr/bin/say")?);
        Ok(format!(
            "macOS:{}:say:{}",
            String::from_utf8(build.stdout)?.trim(),
            executable
        ))
    }

    fn synthesize(&mut self, speech: &Speech, destination: &Path) -> Result<()> {
        let mut child = Command::new("/usr/bin/say")
            .args(["-v", &speech.voice, "-r", &speech.wpm.to_string(), "-o"])
            .arg(destination)
            .args([
                "--file-format=WAVE",
                "--data-format=LEI16@16000",
                &speech.text,
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()?;
        let started = Instant::now();
        loop {
            match child.try_wait() {
                Ok(Some(status)) if status.success() => return Ok(()),
                Ok(Some(status)) => return Err(format!("say failed: {status}").into()),
                Ok(None) if started.elapsed() < Duration::from_secs(30) => {
                    thread::sleep(Duration::from_millis(20));
                }
                other => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(format!("say did not finish within its limit: {other:?}").into());
                }
            }
        }
    }
}

pub fn catalog() -> Result<Catalog> {
    let catalog: Catalog = serde_json::from_str(include_str!("../../../fixtures/desk-v1.json"))?;
    catalog.validate()?;
    Ok(catalog)
}

fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 80
        && id
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}

impl Catalog {
    pub fn validate(&self) -> Result<()> {
        if self.version != 1 || self.scenes.is_empty() || self.scenes.len() > 128 {
            return Err("unsupported or unbounded fixture catalog".into());
        }
        if self.utterances.is_empty() || self.utterances.len() > 256 {
            return Err("invalid utterance count".into());
        }
        for (id, speech) in &self.utterances {
            if !valid_id(id)
                || speech.text.is_empty()
                || speech.text.len() > 1500
                || !(100..=240).contains(&speech.wpm)
                || speech.voice.is_empty()
                || speech.voice.len() > 100
                || speech.voice_revision.is_empty()
            {
                return Err(format!("invalid speech fixture {id}").into());
            }
        }
        let mut ids = BTreeSet::new();
        for scene in &self.scenes {
            if !valid_id(&scene.id)
                || !ids.insert(&scene.id)
                || scene.duration_ms == 0
                || scene.duration_ms > MAX_MS
                || scene.clips.len() > 16
                || scene.expectation.is_empty()
                || scene.precondition.is_empty()
                || scene.description.is_empty()
            {
                return Err(format!("invalid scene {}", scene.id).into());
            }
            for clip in &scene.clips {
                if !self.utterances.contains_key(&clip.utterance)
                    || clip.at_ms >= scene.duration_ms
                    || !clip.gain_db.is_finite()
                    || !(-36.0..=6.0).contains(&clip.gain_db)
                {
                    return Err(format!("invalid clip in {}", scene.id).into());
                }
            }
            if let Some(noise) = &scene.noise
                && (!noise.rms_dbfs.is_finite()
                    || !(-60.0..=-12.0).contains(&noise.rms_dbfs)
                    || noise.seed == 0)
            {
                return Err(format!("invalid noise in {}", scene.id).into());
            }
            match scene.trigger {
                Trigger::LampSpeechStarted { delay_ms } | Trigger::LampSpeechEnded { delay_ms }
                    if delay_ms > MAX_MS =>
                {
                    return Err(format!("invalid trigger in {}", scene.id).into());
                }
                _ => {}
            }
        }
        Ok(())
    }
}

pub fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

pub fn read_wave(path: &Path) -> Result<Vec<f32>> {
    let mut reader = hound::WavReader::open(path)?;
    let spec = reader.spec();
    if spec.channels != 1
        || spec.sample_rate != RATE
        || spec.bits_per_sample != 16
        || spec.sample_format != hound::SampleFormat::Int
        || reader.duration() == 0
        || reader.duration() > RATE * MAX_MS / 1000
    {
        return Err(format!("unexpected or unbounded PCM format: {}", path.display()).into());
    }
    let samples = reader
        .samples::<i16>()
        .map(|v| v.map(|v| f32::from(v) / 32768.0))
        .collect::<std::result::Result<Vec<_>, _>>()?;
    Ok(samples)
}

pub fn write_wave(path: &Path, samples: &[f32]) -> Result<()> {
    if samples.is_empty()
        || samples.len() > (RATE * MAX_MS / 1000) as usize
        || samples.iter().any(|v| !v.is_finite() || v.abs() > 1.0)
    {
        return Err("invalid output PCM".into());
    }
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate: RATE,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let mut writer = hound::WavWriter::create(path, spec)?;
    for &sample in samples {
        writer.write_sample((sample * 32767.0).round() as i16)?;
    }
    writer.finalize()?;
    Ok(())
}

fn levels(samples: &[f32]) -> (f32, f32) {
    let peak = samples.iter().copied().map(f32::abs).fold(0.0, f32::max);
    let rms = (samples.iter().map(|&v| f64::from(v).powi(2)).sum::<f64>()
        / samples.len().max(1) as f64)
        .sqrt() as f32;
    (peak, rms)
}

pub fn mix(scene: &Scene, clips: &BTreeMap<String, Vec<f32>>) -> Result<(Vec<f32>, f32)> {
    if scene.duration_ms == 0 || scene.duration_ms > MAX_MS {
        return Err("invalid scene duration".into());
    }
    let mut output =
        vec![0.0_f32; (u64::from(scene.duration_ms) * u64::from(RATE) / 1000) as usize];
    for clip in &scene.clips {
        let samples = clips
            .get(&clip.utterance)
            .ok_or("missing rendered utterance")?;
        let offset = (u64::from(clip.at_ms) * u64::from(RATE) / 1000) as usize;
        let end = offset
            .checked_add(samples.len())
            .ok_or("clip length overflow")?;
        if end > output.len() {
            return Err(format!("{} truncates utterance {}", scene.id, clip.utterance).into());
        }
        let gain = 10.0_f32.powf(clip.gain_db / 20.0);
        if !gain.is_finite() || samples.iter().any(|v| !v.is_finite()) {
            return Err("non-finite mix source".into());
        }
        for (&sample, target) in samples.iter().zip(&mut output[offset..end]) {
            *target += sample * gain;
        }
    }
    if let Some(noise) = &scene.noise {
        let mut state = noise.seed;
        let mut low_pass = 0.0_f32;
        let mut bed = vec![0.0; output.len()];
        for (index, sample) in bed.iter_mut().enumerate() {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            let white = (f64::from(state) / f64::from(u32::MAX) * 2.0 - 1.0) as f32;
            *sample = match noise.kind {
                NoiseKind::Ventilation => {
                    low_pass = low_pass * 0.98 + white * 0.02;
                    low_pass
                }
                NoiseKind::Keyboard => {
                    // Synthetic click bursts; not a recording of a real keyboard.
                    let position = index % 3840;
                    if position < 180 {
                        white * (1.0 - position as f32 / 180.0)
                    } else {
                        0.0
                    }
                }
            };
        }
        let (_, rms) = levels(&bed);
        if rms > 0.0 {
            let gain = 10.0_f32.powf(noise.rms_dbfs / 20.0) / rms;
            for (target, sample) in output.iter_mut().zip(bed) {
                *target += sample * gain;
            }
        }
    }
    let (peak, _) = levels(&output);
    let attenuation = if peak > 0.85 { 0.85 / peak } else { 1.0 };
    output.iter_mut().for_each(|v| *v *= attenuation);
    Ok((output, attenuation))
}

pub struct Cache {
    root: PathBuf,
}

struct Staging(PathBuf);
impl Drop for Staging {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

impl Cache {
    pub fn new(root: impl Into<PathBuf>) -> Result<Self> {
        let root = root.into();
        fs::create_dir_all(root.join("objects"))?;
        fs::create_dir_all(root.join("locks"))?;
        Ok(Self { root })
    }

    pub fn wav_path(&self, key: &str) -> Result<PathBuf> {
        if key.len() != 64 || !key.bytes().all(|c| c.is_ascii_hexdigit()) {
            return Err("invalid cache key".into());
        }
        Ok(self.root.join("objects").join(key).join("audio.wav"))
    }

    pub fn get(&self, key: &str) -> Result<Option<Asset>> {
        let wav = self.wav_path(key)?;
        let folder = wav.parent().ok_or("invalid object path")?;
        if !folder.exists() {
            return Ok(None);
        }
        let manifest = folder.join("asset.json");
        if fs::metadata(&manifest)?.len() > 4096 || fs::metadata(&wav)?.len() > 2_000_000 {
            return Err(format!("unbounded cached object {key}").into());
        }
        let asset: Asset = serde_json::from_slice(&fs::read(manifest)?)?;
        if asset.key != key || asset.wav_sha256 != hash(&fs::read(&wav)?) {
            return Err(format!("corrupt cached object {key}; refusing to reuse it").into());
        }
        let samples = read_wave(&wav)?;
        let (peak, rms) = levels(&samples);
        if asset.frames != samples.len()
            || asset.sample_rate != RATE
            || !asset.attenuation.is_finite()
            || !(0.0..=1.0).contains(&asset.attenuation)
            || asset.peak != peak
            || asset.rms != rms
        {
            return Err(format!("cached PCM metadata mismatch {key}").into());
        }
        Ok(Some(asset))
    }

    pub fn create<F>(&self, key: &str, generate: F) -> Result<(Asset, bool)>
    where
        F: FnOnce(&Path) -> Result<f32>,
    {
        self.wav_path(key)?;
        let lock = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(self.root.join("locks").join(format!("{key}.lock")))?;
        let started = Instant::now();
        loop {
            match lock.try_lock() {
                Ok(()) => break,
                Err(fs::TryLockError::WouldBlock)
                    if started.elapsed() < Duration::from_secs(40) =>
                {
                    thread::sleep(Duration::from_millis(20));
                }
                Err(error) => return Err(format!("cache lock failed: {error}").into()),
            }
        }
        if let Some(asset) = self.get(key)? {
            return Ok((asset, true));
        }
        let final_path = self
            .wav_path(key)?
            .parent()
            .ok_or("invalid path")?
            .to_owned();
        let temporary = self.root.join("objects").join(format!(
            ".staging-{}-{}",
            std::process::id(),
            TEMP_ID.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&temporary)?;
        let staging = Staging(temporary);
        let wav = staging.0.join("audio.wav");
        let attenuation = generate(&wav)?;
        if !attenuation.is_finite() || !(0.0..=1.0).contains(&attenuation) {
            return Err("invalid cache attenuation".into());
        }
        let samples = read_wave(&wav)?;
        let (peak, rms) = levels(&samples);
        let asset = Asset {
            key: key.to_owned(),
            wav_sha256: hash(&fs::read(&wav)?),
            frames: samples.len(),
            sample_rate: RATE,
            peak,
            rms,
            attenuation,
        };
        fs::write(
            staging.0.join("asset.json"),
            serde_json::to_vec_pretty(&asset)?,
        )?;
        File::open(&wav)?.sync_all()?;
        File::open(staging.0.join("asset.json"))?.sync_all()?;
        match fs::rename(&staging.0, final_path) {
            Ok(()) => Ok((asset, false)),
            // Another renderer can publish first. Reuse only a verified complete object.
            Err(_) if self.get(key)?.is_some() => {
                Ok((self.get(key)?.ok_or("cache publication disappeared")?, true))
            }
            Err(error) => Err(error.into()),
        }
    }

    pub fn verify(&self) -> Result<usize> {
        let mut verified = 0;
        for entry in fs::read_dir(self.root.join("objects"))? {
            let entry = entry?;
            let key = entry.file_name().to_string_lossy().into_owned();
            if key.starts_with(".staging-") {
                continue;
            }
            self.get(&key)?.ok_or("cache object disappeared")?;
            verified += 1;
        }
        Ok(verified)
    }
}

pub fn render<S: Synthesizer>(
    catalog: &Catalog,
    cache: &Cache,
    synthesizer: &mut S,
) -> Result<RenderReport> {
    catalog.validate()?;
    let engine = synthesizer.fingerprint()?;
    let mut speech_assets = BTreeMap::new();
    let mut samples = BTreeMap::new();
    let mut report = RenderReport {
        catalog_sha256: hash(&serde_json::to_vec(catalog)?),
        renderer_version: RENDER_VERSION,
        engine_fingerprint: engine.clone(),
        generated_speech: 0,
        reused_speech: 0,
        generated_scenes: 0,
        reused_scenes: 0,
        scenes: Vec::new(),
        status: "rendered_not_run_or_scored".into(),
        limitations: vec![
            "Synthetic voices from one playback location do not validate spatial speaker direction.".into(),
            "Noise levels are digital dBFS, not room dBA or measured acoustic SNR.".into(),
            "Speech-start/end triggers must be supplied by the acoustic test runner; rendering does not execute them.".into(),
            "Clip schedules are not measured acoustic response latency.".into(),
        ],
    };
    for (id, speech) in &catalog.utterances {
        let key = hash(&serde_json::to_vec(&(
            RENDER_VERSION,
            &engine,
            RATE,
            speech,
        ))?);
        let (asset, reused) = cache.create(&key, |path| {
            synthesizer.synthesize(speech, path)?;
            Ok(1.0)
        })?;
        if reused {
            report.reused_speech += 1;
        } else {
            report.generated_speech += 1;
        }
        let pcm = read_wave(&cache.wav_path(&key)?)?;
        let previous: usize = samples.values().map(Vec::len).sum();
        if previous + pcm.len() > RATE as usize * 300 {
            return Err("catalog exceeds the 300-second decoded speech budget".into());
        }
        samples.insert(id.clone(), pcm);
        speech_assets.insert(id.clone(), asset.wav_sha256);
    }
    for scene in &catalog.scenes {
        let source_hashes = scene
            .clips
            .iter()
            .map(|c| &speech_assets[&c.utterance])
            .collect::<Vec<_>>();
        let key = hash(&serde_json::to_vec(&(
            RENDER_VERSION,
            RATE,
            scene.duration_ms,
            &scene.clips,
            &scene.noise,
            source_hashes,
        ))?);
        let (asset, reused) = cache.create(&key, |path| {
            let (output, attenuation) = mix(scene, &samples)?;
            write_wave(path, &output)?;
            Ok(attenuation)
        })?;
        if reused {
            report.reused_scenes += 1;
        } else {
            report.generated_scenes += 1;
        }
        report.scenes.push(SceneAsset {
            scene: scene.clone(),
            asset,
        });
    }
    Ok(report)
}

/// Publish a run manifest without replacing an existing run's evidence.
pub fn save_report(destination: &Path, report: &RenderReport) -> Result<()> {
    use std::io::Write;
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(destination)?;
    file.write_all(&serde_json::to_vec_pretty(report)?)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    Ok(())
}

pub fn invalid_argument(message: &str) -> Box<dyn Error + Send + Sync> {
    io::Error::new(io::ErrorKind::InvalidInput, message).into()
}
