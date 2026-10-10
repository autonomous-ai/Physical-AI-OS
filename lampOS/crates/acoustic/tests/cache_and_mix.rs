use lamp_acoustic::{
    Cache, Catalog, Clip, Noise, NoiseKind, RATE, Result, Scene, Speech, Synthesizer, Trigger,
    catalog, hash, mix, read_wave, render, save_report, write_wave,
};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    sync::{
        Arc, Barrier,
        atomic::{AtomicU64, Ordering},
    },
    thread,
};

static ID: AtomicU64 = AtomicU64::new(0);

struct TestDir(PathBuf);
impl TestDir {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "lamp-acoustic-test-{}-{}",
            std::process::id(),
            ID.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for TestDir {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

#[derive(Default)]
struct FakeSynth {
    calls: usize,
    fail: bool,
}
impl Synthesizer for FakeSynth {
    fn fingerprint(&self) -> Result<String> {
        Ok("fixture-engine-v1".into())
    }
    fn synthesize(&mut self, _: &Speech, destination: &Path) -> Result<()> {
        assert!(!self.fail, "a verified cache hit invoked the synthesizer");
        self.calls += 1;
        write_wave(destination, &vec![0.1; 800])
    }
}

fn tiny_catalog() -> Catalog {
    let mut full = catalog().unwrap();
    full.scenes.truncate(2);
    for scene in &mut full.scenes {
        scene.duration_ms = 1000;
        for clip in &mut scene.clips {
            clip.at_ms = 0;
        }
    }
    full.utterances.retain(|key, _| {
        full.scenes
            .iter()
            .any(|s| s.clips.iter().any(|c| &c.utterance == key))
    });
    full
}

#[test]
fn shipped_catalog_is_valid_and_covers_required_negative_cases() {
    let catalog = catalog().unwrap();
    for id in [
        "two-colleagues",
        "other-device",
        "computer-call",
        "background-media",
        "topic-change",
        "listener-acknowledgment",
        "hesitant-sharing",
        "short-yes",
        "fast-followup",
        "visual-chat",
        "noisy-question",
        "overlapping-speakers",
        "noise-only",
    ] {
        assert!(
            catalog.scenes.iter().any(|scene| scene.id == id),
            "missing {id}"
        );
    }
}

#[test]
fn repeat_run_reuses_every_wave_without_synthesis() {
    let dir = TestDir::new();
    let cache = Cache::new(&dir.0).unwrap();
    let catalog = tiny_catalog();
    let mut synth = FakeSynth::default();
    let first = render(&catalog, &cache, &mut synth).unwrap();
    assert_eq!(first.generated_speech, 2);
    assert_eq!(first.generated_scenes, 2);
    synth.fail = true;
    let second = render(&catalog, &cache, &mut synth).unwrap();
    assert_eq!(second.generated_speech, 0);
    assert_eq!(second.generated_scenes, 0);
    assert_eq!(second.reused_speech, 2);
    assert_eq!(second.reused_scenes, 2);
    assert_eq!(synth.calls, 2);
    assert_eq!(cache.verify().unwrap(), 4);
}

#[test]
fn voice_setting_changes_only_invalidate_affected_speech() {
    let dir = TestDir::new();
    let cache = Cache::new(&dir.0).unwrap();
    let mut catalog = tiny_catalog();
    let mut synth = FakeSynth::default();
    render(&catalog, &cache, &mut synth).unwrap();
    catalog.utterances.get_mut("hello").unwrap().wpm += 1;
    let second = render(&catalog, &cache, &mut synth).unwrap();
    assert_eq!(second.generated_speech, 1);
    assert_eq!(second.reused_speech, 1);
    // The fake synth generated identical PCM. Content-identical mixes stay reusable.
    assert_eq!(second.generated_scenes, 0);
}

#[test]
fn editing_expectations_or_triggers_does_not_regenerate_audio() {
    let dir = TestDir::new();
    let cache = Cache::new(&dir.0).unwrap();
    let mut catalog = tiny_catalog();
    let mut synth = FakeSynth::default();
    render(&catalog, &cache, &mut synth).unwrap();
    catalog.scenes[0].expectation = "Updated scoring rubric".into();
    catalog.scenes[0].trigger = Trigger::LampSpeechEnded { delay_ms: 900 };
    synth.fail = true;
    let next = render(&catalog, &cache, &mut synth).unwrap();
    assert_eq!(next.generated_scenes, 0);
    assert!(matches!(
        next.scenes[0].scene.trigger,
        Trigger::LampSpeechEnded { delay_ms: 900 }
    ));
}

#[test]
fn corruption_is_reported_without_synthesis() {
    let dir = TestDir::new();
    let cache = Cache::new(&dir.0).unwrap();
    let catalog = tiny_catalog();
    let mut synth = FakeSynth::default();
    let report = render(&catalog, &cache, &mut synth).unwrap();
    let wav = cache.wav_path(&report.scenes[0].asset.key).unwrap();
    fs::write(wav, b"not a WAV").unwrap();
    synth.fail = true;
    assert!(
        render(&catalog, &cache, &mut synth)
            .unwrap_err()
            .to_string()
            .contains("corrupt cached")
    );
}

#[test]
fn metadata_tampering_is_rejected() {
    let dir = TestDir::new();
    let cache = Cache::new(&dir.0).unwrap();
    let key = hash(b"metadata");
    let (mut asset, _) = cache
        .create(&key, |p| {
            write_wave(p, &[0.25; 800])?;
            Ok(1.0)
        })
        .unwrap();
    asset.frames += 1;
    let path = cache.wav_path(&key).unwrap().with_file_name("asset.json");
    fs::write(path, serde_json::to_vec(&asset).unwrap()).unwrap();
    assert!(
        cache
            .get(&key)
            .unwrap_err()
            .to_string()
            .contains("metadata mismatch")
    );
}

#[test]
fn failed_generation_publishes_nothing_and_cleans_staging() {
    let dir = TestDir::new();
    let cache = Cache::new(&dir.0).unwrap();
    let key = hash(b"failure");
    assert!(
        cache
            .create(&key, |_| Err("synthesis failed".into()))
            .is_err()
    );
    assert!(cache.get(&key).unwrap().is_none());
    assert_eq!(cache.verify().unwrap(), 0);
    assert_eq!(fs::read_dir(dir.0.join("objects")).unwrap().count(), 0);
}

#[test]
fn two_concurrent_renderers_publish_one_complete_asset() {
    let dir = TestDir::new();
    let root = dir.0.clone();
    let key = hash(b"concurrent");
    let barrier = Arc::new(Barrier::new(2));
    let generators = Arc::new(AtomicU64::new(0));
    let workers = (0..2)
        .map(|_| {
            let root = root.clone();
            let key = key.clone();
            let barrier = barrier.clone();
            let generators = generators.clone();
            thread::spawn(move || {
                let cache = Cache::new(root).unwrap();
                barrier.wait();
                cache
                    .create(&key, |path| {
                        generators.fetch_add(1, Ordering::Relaxed);
                        write_wave(path, &[0.2; 800])?;
                        Ok(1.0)
                    })
                    .unwrap()
            })
        })
        .collect::<Vec<_>>();
    let results = workers
        .into_iter()
        .map(|t| t.join().unwrap())
        .collect::<Vec<_>>();
    assert_eq!(generators.load(Ordering::Relaxed), 1);
    assert_eq!(results.iter().filter(|(_, reused)| *reused).count(), 1);
    assert_eq!(Cache::new(root).unwrap().verify().unwrap(), 1);
}

fn scene() -> Scene {
    Scene {
        id: "test".into(),
        description: "Test".into(),
        expectation: "Test".into(),
        precondition: "No hardware".into(),
        trigger: Trigger::SceneStart,
        duration_ms: 1000,
        clips: vec![],
        noise: None,
    }
}

#[test]
fn mixing_never_silently_truncates_speech() {
    let mut scene = scene();
    scene.clips.push(Clip {
        utterance: "long".into(),
        at_ms: 900,
        gain_db: 0.0,
    });
    let clips = BTreeMap::from([("long".into(), vec![0.5; 3200])]);
    assert!(
        mix(&scene, &clips)
            .unwrap_err()
            .to_string()
            .contains("truncates")
    );
}

#[test]
fn overlapping_voices_are_attenuated_together_without_clipping() {
    let mut scene = scene();
    scene.clips = vec![
        Clip {
            utterance: "a".into(),
            at_ms: 0,
            gain_db: 0.0,
        },
        Clip {
            utterance: "b".into(),
            at_ms: 0,
            gain_db: 0.0,
        },
    ];
    let clips = BTreeMap::from([("a".into(), vec![0.8; 800]), ("b".into(), vec![0.8; 800])]);
    let (mixed, attenuation) = mix(&scene, &clips).unwrap();
    assert!(attenuation < 1.0);
    assert!(mixed.iter().all(|s| s.abs() <= 0.850001));
    assert!((mixed[0] - 0.85).abs() < 0.00001);
}

#[test]
fn noise_is_deterministic_and_seeded() {
    let mut scene = scene();
    scene.noise = Some(Noise {
        kind: NoiseKind::Ventilation,
        seed: 401,
        rms_dbfs: -28.0,
    });
    let first = mix(&scene, &BTreeMap::new()).unwrap().0;
    assert_eq!(first, mix(&scene, &BTreeMap::new()).unwrap().0);
    scene.noise.as_mut().unwrap().seed = 402;
    assert_ne!(first, mix(&scene, &BTreeMap::new()).unwrap().0);
}

#[test]
fn corrupt_references_and_unbounded_catalogs_are_rejected() {
    let mut catalog = tiny_catalog();
    catalog.scenes[0].clips[0].utterance = "missing".into();
    assert!(catalog.validate().is_err());
    catalog.scenes[0].clips[0].utterance = "hello".into();
    catalog.scenes[0].duration_ms = 60_001;
    assert!(catalog.validate().is_err());
}

#[test]
fn waveform_validation_rejects_non_finite_and_wrong_sample_rate() {
    let dir = TestDir::new();
    let path = dir.0.join("wrong.wav");
    assert!(write_wave(&path, &[f32::NAN]).is_err());
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate: RATE * 2,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let mut writer = hound::WavWriter::create(&path, spec).unwrap();
    writer.write_sample(1_i16).unwrap();
    writer.finalize().unwrap();
    assert!(read_wave(&path).is_err());
}

#[test]
fn cache_keys_cannot_escape_the_cache_directory() {
    let dir = TestDir::new();
    let cache = Cache::new(&dir.0).unwrap();
    assert!(cache.wav_path("../../test").is_err());
}

#[test]
fn completed_report_is_not_overwritten() {
    let dir = TestDir::new();
    let cache = Cache::new(&dir.0).unwrap();
    let report = render(&tiny_catalog(), &cache, &mut FakeSynth::default()).unwrap();
    let destination = dir.0.join("report.json");
    save_report(&destination, &report).unwrap();
    let first = fs::read(&destination).unwrap();
    assert!(save_report(&destination, &report).is_err());
    assert_eq!(first, fs::read(destination).unwrap());
}
