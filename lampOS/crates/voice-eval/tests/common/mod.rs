//! Shared helpers: private temporary directories and a synthetic acoustic cache
//! built through lamp-acoustic's own cache API. Synthetic tones stand in for
//! speech only to exercise code paths; they are never fixture speech.
#![allow(dead_code)]
use lamp_acoustic::{Asset, Cache, RenderReport, SceneAsset, hash, write_wave};
use lamp_voice_eval::stimulus::StimulusCatalog;
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};

static NEXT: AtomicU64 = AtomicU64::new(0);

pub struct Private {
    pub path: PathBuf,
}
impl Private {
    pub fn new(label: &str) -> Self {
        // Short paths: Unix socket names are limited to ~100 bytes.
        let path = PathBuf::from(format!(
            "/tmp/lve-{}-{}-{}",
            label,
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        Self { path }
    }
    pub fn join(&self, name: &str) -> PathBuf {
        self.path.join(name)
    }
}
impl Drop for Private {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

/// A 200 Hz tone where each clip of the scene is "spoken". lamp-live's VAD
/// scores such a tone as speech, which is itself a finding about the gate.
pub fn tone_scene(catalog: &StimulusCatalog, scene: &str, amplitude: f32) -> Vec<f32> {
    let timing = catalog.estimate(scene).unwrap();
    let mut samples = vec![0.0_f32; timing.duration_ms as usize * 16];
    for clip in &timing.clips {
        for (index, sample) in samples[clip.start_ms as usize * 16..clip.end_ms as usize * 16]
            .iter_mut()
            .enumerate()
        {
            *sample =
                amplitude * (2.0 * std::f32::consts::PI * 200.0 * index as f32 / 16_000.0).sin();
        }
    }
    samples
}

/// Write synthetic scene mixes into a fresh cache and a matching manifest.
pub fn synthetic_cache(
    dir: &Path,
    catalog: &StimulusCatalog,
    scenes: &[(&str, Vec<f32>)],
) -> PathBuf {
    let cache = Cache::new(dir.join("cache")).unwrap();
    let mut entries = Vec::new();
    for (scene, samples) in scenes {
        let key = hash(format!("synthetic-test-{scene}").as_bytes());
        let (asset, _): (Asset, bool) = cache
            .create(&key, |path| {
                write_wave(path, samples)?;
                Ok(1.0)
            })
            .unwrap();
        entries.push(SceneAsset {
            scene: catalog.scene(scene).unwrap().clone(),
            asset,
        });
    }
    let report = RenderReport {
        catalog_sha256: catalog.merged_sha256.clone(),
        renderer_version: 1,
        engine_fingerprint: "synthetic-test-tones".into(),
        generated_speech: 0,
        reused_speech: 0,
        generated_scenes: entries.len(),
        reused_scenes: 0,
        scenes: entries,
        status: "synthetic_test_only".into(),
        limitations: vec!["Synthetic tones, not speech.".into()],
    };
    let manifest = dir.join("manifest.json");
    fs::write(&manifest, serde_json::to_vec(&report).unwrap()).unwrap();
    manifest
}
