//! Stimuli: the existing `desk-v1` catalog plus voice-eval additions, resolved
//! through the existing verified acoustic cache. Nothing here synthesizes speech.
use crate::{Result, invalid};
use lamp_acoustic::{Cache, Catalog, RenderReport, Scene, hash};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

pub const DESK_SOURCE: &str = include_str!("../../../fixtures/desk-v1.json");
pub const EXTENSION_SOURCE: &str = include_str!("../../../fixtures/voice-eval-stimuli-v1.json");
pub const BLOCK_SAMPLES: usize = 160;
const RATE: u32 = lamp_acoustic::RATE;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TriggerKind {
    SceneStart,
    LampSpeechStarted,
    LampSpeechEnded,
}

/// `desk-v1` merged with the voice-eval extension. Extension utterances that
/// reuse a desk ID must be byte-for-byte equivalent definitions, so their cache
/// keys (and cached audio) are shared rather than regenerated.
#[derive(Clone, Debug)]
pub struct StimulusCatalog {
    pub catalog: Catalog,
    pub desk_file_sha256: String,
    pub extension_file_sha256: String,
    /// Hash of the merged catalog serialization, as `lamp-acoustic` reports it.
    pub merged_sha256: String,
    pub extension_utterances: BTreeSet<String>,
    pub extension_scenes: BTreeSet<String>,
}

impl StimulusCatalog {
    pub fn load() -> Result<Self> {
        Self::merge(DESK_SOURCE, EXTENSION_SOURCE)
    }

    pub fn merge(desk_source: &str, extension_source: &str) -> Result<Self> {
        let desk: Catalog = serde_json::from_str(desk_source)?;
        desk.validate()?;
        let extension: Catalog = serde_json::from_str(extension_source)?;
        if extension.version != desk.version {
            return Err(invalid(
                "stimulus extension version differs from desk catalog",
            ));
        }
        let mut merged = desk.clone();
        let mut extension_utterances = BTreeSet::new();
        for (id, speech) in extension.utterances {
            match merged.utterances.get(&id) {
                Some(existing)
                    if serde_json::to_value(existing)? != serde_json::to_value(&speech)? =>
                {
                    return Err(invalid(&format!("utterance {id} conflicts with desk-v1")));
                }
                Some(_) => {}
                None => {
                    extension_utterances.insert(id.clone());
                    merged.utterances.insert(id, speech);
                }
            }
        }
        let mut extension_scenes = BTreeSet::new();
        for scene in extension.scenes {
            if merged.scenes.iter().any(|existing| existing.id == scene.id) {
                return Err(invalid(&format!(
                    "scene {} conflicts with desk-v1",
                    scene.id
                )));
            }
            extension_scenes.insert(scene.id.clone());
            merged.scenes.push(scene);
        }
        merged.validate()?;
        Ok(Self {
            merged_sha256: hash(&serde_json::to_vec(&merged)?),
            catalog: merged,
            desk_file_sha256: hash(desk_source.as_bytes()),
            extension_file_sha256: hash(extension_source.as_bytes()),
            extension_utterances,
            extension_scenes,
        })
    }

    pub fn scene(&self, id: &str) -> Result<&Scene> {
        self.catalog
            .scenes
            .iter()
            .find(|scene| scene.id == id)
            .ok_or_else(|| invalid(&format!("unknown scene {id}")))
    }

    /// Text/rate estimate of each clip's speech interval. This is a declared
    /// model for scheduling and attribution, never an acoustic boundary.
    pub fn estimate(&self, scene_id: &str) -> Result<SceneTiming> {
        let scene = self.scene(scene_id)?;
        let mut clips = Vec::with_capacity(scene.clips.len());
        let mut voices = BTreeSet::new();
        for clip in &scene.clips {
            let speech = &self.catalog.utterances[&clip.utterance];
            voices.insert(speech.voice.clone());
            let words = speech.text.split_whitespace().count().max(1) as u64;
            let speech_ms = (words * 60_000 / u64::from(speech.wpm)) as u32;
            clips.push(ClipTiming {
                utterance: clip.utterance.clone(),
                text: speech.text.clone(),
                voice: speech.voice.clone(),
                gain_db: clip.gain_db,
                start_ms: clip.at_ms,
                end_ms: (clip.at_ms + speech_ms).min(scene.duration_ms),
            });
        }
        Ok(SceneTiming {
            scene: scene.id.clone(),
            duration_ms: scene.duration_ms,
            speech_span_ms: span(&clips),
            clips,
            voices: voices.len(),
            noise: scene.noise.is_some(),
            source: TimingSource::TextEstimate,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TimingSource {
    /// Words and speaking rate; no audio inspected.
    TextEstimate,
    /// Digital energy of the cached scene mix; not room audio.
    MeasuredMix,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct ClipTiming {
    pub utterance: String,
    pub text: String,
    pub voice: String,
    pub gain_db: f32,
    /// Speech interval relative to the scene start.
    pub start_ms: u32,
    pub end_ms: u32,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct SceneTiming {
    pub scene: String,
    pub duration_ms: u32,
    pub clips: Vec<ClipTiming>,
    pub speech_span_ms: Option<(u32, u32)>,
    /// Distinct synthetic voices. They all leave one loudspeaker, so more than
    /// one voice never establishes spatial speaker discrimination.
    pub voices: usize,
    pub noise: bool,
    pub source: TimingSource,
}

fn span(clips: &[ClipTiming]) -> Option<(u32, u32)> {
    let start = clips.iter().map(|c| c.start_ms).min()?;
    let end = clips.iter().map(|c| c.end_ms).max()?;
    Some((start, end))
}

impl SceneTiming {
    /// Refine clip intervals from the exact cached mix. Each clip searches only
    /// its own window; overlapping clips keep the estimated boundary they share.
    pub fn measure(&mut self, pcm: &[i16]) {
        let levels: Vec<f32> = pcm
            .chunks(BLOCK_SAMPLES)
            .map(|block| {
                let energy = block.iter().map(|&s| f64::from(s).powi(2)).sum::<f64>()
                    / block.len().max(1) as f64;
                (10.0 * (energy.max(1e-3) / (32768.0_f64 * 32768.0)).log10()) as f32
            })
            .collect();
        if levels.is_empty() {
            return;
        }
        let mut sorted = levels.clone();
        sorted.sort_by(f32::total_cmp);
        let floor = sorted[sorted.len() / 2];
        let threshold = if self.noise {
            floor + 10.0
        } else {
            -50.0_f32.max(floor + 10.0)
        };
        let active = |block: usize| levels.get(block).is_some_and(|&v| v >= threshold);
        let ms_to_block = |ms: u32| (ms / 10) as usize;
        let count = self.clips.len();
        let mut measured_any = false;
        for index in 0..count {
            let estimated = self.clips[index].clone();
            let next_start = self.clips.get(index + 1).map(|next| next.start_ms);
            let overlaps_next = next_start.is_some_and(|next| estimated.end_ms > next);
            let window_end = match next_start {
                Some(next) if !overlaps_next => next,
                Some(_) => estimated.end_ms,
                None => self.duration_ms,
            };
            let (from, to) = (ms_to_block(estimated.start_ms), ms_to_block(window_end));
            let first = (from..to).find(|&b| active(b));
            let Some(first) = first else { continue };
            let last = if overlaps_next {
                None
            } else {
                (first..to).rev().find(|&b| active(b))
            };
            let clip = &mut self.clips[index];
            clip.start_ms = (first * 10) as u32;
            if let Some(last) = last {
                clip.end_ms = ((last + 1) * 10) as u32;
            }
            measured_any = true;
        }
        if measured_any {
            self.source = TimingSource::MeasuredMix;
            self.speech_span_ms = span(&self.clips);
        }
    }
}

/// Verified scene assets from one or more existing `lamp-acoustic` render
/// manifests. Each manifest scene must equal the current catalog definition.
pub struct AssetIndex {
    cache: Cache,
    scenes: BTreeMap<String, ResolvedAsset>,
}

#[derive(Clone, Debug, Serialize)]
pub struct ResolvedAsset {
    pub scene: String,
    pub key: String,
    pub wav_sha256: String,
    pub frames: usize,
    pub wav: PathBuf,
    pub manifest_sha256: String,
}

impl AssetIndex {
    pub fn load(
        cache_root: &Path,
        manifests: &[PathBuf],
        catalog: &StimulusCatalog,
    ) -> Result<Self> {
        if !cache_root.join("objects").is_dir() {
            return Err(invalid(
                "acoustic cache has no objects directory; cached speech is required and is not regenerated",
            ));
        }
        let cache = Cache::new(cache_root)?;
        let mut scenes = BTreeMap::new();
        for manifest in manifests {
            let bytes = fs::read(manifest)?;
            if bytes.len() > 4_000_000 {
                return Err(invalid("render manifest exceeds 4 MB"));
            }
            let report: RenderReport = serde_json::from_slice(&bytes)?;
            let manifest_sha256 = hash(&bytes);
            for entry in report.scenes {
                let Ok(current) = catalog.scene(&entry.scene.id) else {
                    continue;
                };
                if serde_json::to_value(current)? != serde_json::to_value(&entry.scene)? {
                    // A stale definition must not stand in for the current scene.
                    continue;
                }
                let asset = cache.get(&entry.asset.key)?.ok_or_else(|| {
                    invalid(&format!(
                        "cached object for scene {} is missing",
                        entry.scene.id
                    ))
                })?;
                if asset.wav_sha256 != entry.asset.wav_sha256 {
                    return Err(invalid(&format!(
                        "scene {} cache differs from its manifest",
                        entry.scene.id
                    )));
                }
                scenes.insert(
                    entry.scene.id.clone(),
                    ResolvedAsset {
                        scene: entry.scene.id.clone(),
                        wav: cache.wav_path(&asset.key)?,
                        key: asset.key,
                        wav_sha256: asset.wav_sha256,
                        frames: asset.frames,
                        manifest_sha256: manifest_sha256.clone(),
                    },
                );
            }
        }
        Ok(Self { cache, scenes })
    }

    pub fn get(&self, scene: &str) -> Option<&ResolvedAsset> {
        self.scenes.get(scene)
    }

    pub fn scenes(&self) -> impl Iterator<Item = &ResolvedAsset> {
        self.scenes.values()
    }

    /// Re-verify integrity at use time and return the exact 16 kHz PCM.
    pub fn pcm(&self, scene: &str) -> Result<Vec<i16>> {
        let resolved = self
            .get(scene)
            .ok_or_else(|| invalid(&format!("scene {scene} has no verified cached asset")))?;
        let asset = self
            .cache
            .get(&resolved.key)?
            .ok_or_else(|| invalid("cached asset disappeared"))?;
        if asset.wav_sha256 != resolved.wav_sha256 {
            return Err(invalid("cached asset changed after resolution"));
        }
        read_pcm16(&resolved.wav)
    }
}

pub fn read_pcm16(path: &Path) -> Result<Vec<i16>> {
    let mut reader = hound::WavReader::open(path)?;
    let spec = reader.spec();
    if spec.channels != 1
        || spec.sample_rate != RATE
        || spec.bits_per_sample != 16
        || spec.sample_format != hound::SampleFormat::Int
        || reader.duration() > RATE * 60
    {
        return Err(invalid("expected bounded mono 16 kHz PCM16"));
    }
    Ok(reader
        .samples::<i16>()
        .collect::<std::result::Result<_, _>>()?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merged_catalog_keeps_desk_definitions_and_adds_new_scenes() {
        let catalog = StimulusCatalog::load().unwrap();
        assert_eq!(
            catalog.desk_file_sha256,
            "636a5199cb3da36f0f91576bab8aabaf5af5e97c584ae7b82145f7a1463a675f",
            "desk-v1 must stay the audited fixture"
        );
        assert!(catalog.scene("quick-chat").is_ok());
        assert!(catalog.scene("quiet-question").is_ok());
        assert!(catalog.extension_scenes.contains("ack-yeah"));
        assert!(!catalog.extension_utterances.contains("math"));
        assert_eq!(catalog.extension_utterances.len(), 6);
    }

    #[test]
    fn conflicting_extension_definitions_are_rejected() {
        let conflicting = EXTENSION_SOURCE.replacen(
            "\"utterances\": {",
            "\"utterances\": {\"math\": {\"text\": \"Different.\", \"voice\": \"Daniel\", \"wpm\": 190, \"voice_revision\": \"builtin-en-v1\"},",
            1,
        );
        assert!(StimulusCatalog::merge(DESK_SOURCE, &conflicting).is_err());
        let duplicate_scene =
            EXTENSION_SOURCE.replacen("\"id\": \"ask-yes-no\"", "\"id\": \"quick-chat\"", 1);
        assert!(StimulusCatalog::merge(DESK_SOURCE, &duplicate_scene).is_err());
    }

    #[test]
    fn measured_mix_refines_only_within_each_clip_window() {
        let catalog = StimulusCatalog::load().unwrap();
        let mut timing = catalog.estimate("hesitant-sharing").unwrap();
        assert_eq!(timing.source, TimingSource::TextEstimate);
        let mut pcm = vec![0_i16; 24_000 * 16 / 1000 * 1000 / 16];
        pcm.resize(16 * 24_000, 0);
        // Speech-like energy at 1.1-5.9 s and 7.05-10.5 s.
        for (start, end) in [(1_100, 5_900), (7_050, 10_500)] {
            for sample in &mut pcm[start * 16..end * 16] {
                *sample = 6_000;
            }
        }
        timing.measure(&pcm);
        assert_eq!(timing.source, TimingSource::MeasuredMix);
        assert_eq!(
            (timing.clips[0].start_ms, timing.clips[0].end_ms),
            (1_100, 5_900)
        );
        assert_eq!(
            (timing.clips[1].start_ms, timing.clips[1].end_ms),
            (7_050, 10_500)
        );
        assert_eq!(timing.speech_span_ms, Some((1_100, 10_500)));
    }
}
