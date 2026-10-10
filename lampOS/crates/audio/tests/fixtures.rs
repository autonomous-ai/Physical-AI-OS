use lamp_audio::{
    CAPTURE_SAMPLES, RENDER_SAMPLES,
    qualification::{
        FixtureManifest, benchmark, benchmark_loaded, export_fixture, load_fixture,
        synthetic_echo_block, write_report_new,
    },
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::{
        Barrier,
        atomic::{AtomicU64, Ordering},
    },
};

static DIRECTORY_ID: AtomicU64 = AtomicU64::new(0);

struct TestDirectory(PathBuf);
impl TestDirectory {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "lamp-audio-fixtures-{}-{}",
            std::process::id(),
            DIRECTORY_ID.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        Self(path)
    }

    fn fixture(&self) -> PathBuf {
        let path = self.0.join("fixture");
        export_fixture(&path, 500).unwrap();
        path
    }
}
impl Drop for TestDirectory {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn pcm_digest(samples: &[i16]) -> String {
    digest(
        &samples
            .iter()
            .flat_map(|sample| sample.to_le_bytes())
            .collect::<Vec<_>>(),
    )
}

fn manifest(path: &Path) -> Value {
    serde_json::from_slice(&fs::read(path.join("manifest.json")).unwrap()).unwrap()
}

fn write_manifest(path: &Path, manifest: &Value) {
    fs::write(
        path.join("manifest.json"),
        serde_json::to_vec_pretty(manifest).unwrap(),
    )
    .unwrap();
}

// Test-only resealing deliberately gets corrupt inputs past byte-integrity
// checks, so later format and semantic checks must independently reject them.
fn reseal(path: &Path, manifest: &mut Value, asset_pointer: &str) {
    let asset = manifest.pointer_mut(asset_pointer).unwrap();
    let bytes = fs::read(path.join(asset["file"].as_str().unwrap())).unwrap();
    asset["sha256"] = json!(digest(&bytes));
    asset["file_bytes"] = json!(bytes.len());
    write_manifest(path, manifest);
}

#[test]
fn exported_pcm_is_exactly_reusable_and_has_reproducible_provenance() {
    let directory = TestDirectory::new();
    let path = directory.fixture();
    let loaded = load_fixture(&path).unwrap();
    let loaded_again = load_fixture(&path).unwrap();
    assert_eq!(loaded.hashes(), loaded_again.hashes());
    assert_eq!(loaded.manifest(), loaded_again.manifest());
    for block in 0..1000 {
        let (render, capture) = synthetic_echo_block(block);
        assert_eq!(
            &loaded.render_samples()[block * RENDER_SAMPLES..(block + 1) * RENDER_SAMPLES],
            &render
        );
        assert_eq!(
            &loaded.capture_samples()[block * CAPTURE_SAMPLES..(block + 1) * CAPTURE_SAMPLES],
            &capture
        );
    }
    assert_eq!(loaded.near_capture_samples(), loaded.capture_samples());
    assert_eq!(loaded.near_render_samples().len(), 1000 * RENDER_SAMPLES);
    assert!(
        loaded
            .near_render_samples()
            .iter()
            .all(|&sample| sample == 0)
    );
    assert_eq!(loaded.hashes().render, pcm_digest(loaded.render_samples()));
    assert_eq!(
        loaded.hashes().capture,
        pcm_digest(loaded.capture_samples())
    );
    assert_eq!(
        loaded.hashes().near_render,
        pcm_digest(loaded.near_render_samples())
    );
    assert_eq!(
        loaded.hashes().near_capture,
        pcm_digest(loaded.near_capture_samples())
    );
    let m = loaded.manifest().unwrap();
    assert_eq!(m.schema_version, 1);
    assert_eq!((m.warmup_blocks, m.measured_blocks), (500, 500));
    assert_eq!(
        (m.near_end.warmup_blocks, m.near_end.measured_blocks),
        (500, 500)
    );
    for asset in [
        &m.render,
        &m.capture,
        &m.near_end.render,
        &m.near_end.capture,
    ] {
        let bytes = fs::read(path.join(&asset.file)).unwrap();
        assert_eq!(bytes.len(), asset.file_bytes);
        assert_eq!(digest(&bytes), asset.sha256);
    }
    let second = directory.0.join("same-samples");
    assert_eq!(&export_fixture(&second, 500).unwrap(), m);
    assert_eq!(
        fs::read(path.join("manifest.json")).unwrap(),
        fs::read(second.join("manifest.json")).unwrap()
    );
}

#[test]
fn schema_counts_rates_and_paths_are_checked_before_loading_pcm() {
    let directory = TestDirectory::new();
    let path = directory.fixture();
    let original = manifest(&path);
    for (pointer, value) in [
        ("/schema_version", json!(2)),
        ("/fixture_id", json!("unrecognized")),
        ("/block_ms", json!(20)),
        ("/sample_format", json!("float32")),
        ("/channels", json!(2)),
        ("/warmup_blocks", json!(499)),
        ("/measured_blocks", json!(499)),
        ("/measured_blocks", json!(60_001)),
        ("/measured_blocks", json!(usize::MAX)),
        ("/delay_ms", json!(0)),
        ("/noise_suppression", json!(false)),
        ("/render/file", json!("../outside.wav")),
        ("/render/sample_rate", json!(16_000)),
        ("/capture/frames", json!(160_001)),
        ("/render/file_bytes", json!(usize::MAX)),
        ("/render/sha256", json!("z".repeat(64))),
        ("/render/sha256", json!("a".repeat(63))),
        ("/near_end/warmup_blocks", json!(0)),
        ("/near_end/measured_blocks", json!(1000)),
        ("/near_end/delay_ms", json!(70)),
        ("/near_end/reset_before", json!(false)),
        ("/near_end/capture/frames", json!(usize::MAX)),
    ] {
        let mut changed = original.clone();
        *changed.pointer_mut(pointer).unwrap() = value;
        write_manifest(&path, &changed);
        assert!(
            load_fixture(&path).is_err(),
            "accepted mismatch at {pointer}"
        );
    }
    let mut unknown = original.clone();
    unknown["unexpected"] = json!(true);
    write_manifest(&path, &unknown);
    assert!(load_fixture(&path).is_err());
    let mut unknown = original.clone();
    unknown["capture"]["unexpected"] = json!(true);
    write_manifest(&path, &unknown);
    assert!(load_fixture(&path).is_err());
    fs::write(path.join("manifest.json"), vec![b' '; 16_385]).unwrap();
    assert!(load_fixture(&path).is_err());
    write_manifest(&path, &original);
    assert!(load_fixture(&path).is_ok());
}

#[test]
fn waveform_hash_and_file_size_cannot_be_silently_changed() {
    let directory = TestDirectory::new();
    let path = directory.fixture();
    let file = path.join("render.wav");
    let original = fs::read(&file).unwrap();
    let mut changed = original.clone();
    changed[2000] ^= 1;
    fs::write(&file, &changed).unwrap();
    assert!(load_fixture(&path).is_err());
    fs::write(&file, &original[..original.len() - 2]).unwrap();
    assert!(load_fixture(&path).is_err());
    fs::write(&file, &original).unwrap();
    fs::OpenOptions::new()
        .write(true)
        .open(&file)
        .unwrap()
        .set_len(original.len() as u64 + 4097)
        .unwrap();
    assert!(load_fixture(&path).is_err());
    fs::write(&file, &original).unwrap();
    assert!(load_fixture(&path).is_ok());
}

#[test]
fn resealed_bad_wav_format_still_fails_validation() {
    let directory = TestDirectory::new();
    let path = directory.fixture();
    let file = path.join("render.wav");
    let original = fs::read(&file).unwrap();
    let original_manifest = manifest(&path);
    for (offset, bytes) in [
        (20, 3_u16.to_le_bytes().to_vec()),
        (22, 2_u16.to_le_bytes().to_vec()),
        (24, 48_000_u32.to_le_bytes().to_vec()),
        (34, 8_u16.to_le_bytes().to_vec()),
    ] {
        let mut changed = original.clone();
        changed[offset..offset + bytes.len()].copy_from_slice(&bytes);
        fs::write(&file, changed).unwrap();
        reseal(&path, &mut original_manifest.clone(), "/render");
        assert!(
            load_fixture(&path).is_err(),
            "accepted bad WAV field {offset}"
        );
    }
    fs::write(&file, original).unwrap();
    write_manifest(&path, &original_manifest);
    assert!(load_fixture(&path).is_ok());
}

#[test]
fn resealed_truncated_data_and_extra_samples_are_rejected() {
    let directory = TestDirectory::new();
    let path = directory.fixture();
    let file = path.join("render.wav");
    let original = fs::read(&file).unwrap();
    let original_manifest = manifest(&path);
    assert_eq!(&original[36..40], b"data");
    // Extra legal header bytes keep total size within the metadata envelope,
    // while the data chunk still promises four more bytes than actually exist.
    let mut truncated = original[..36].to_vec();
    truncated.extend_from_slice(b"JUNK\0\0\0\0");
    truncated.extend_from_slice(&original[36..original.len() - 4]);
    let riff_len = (truncated.len() - 8) as u32;
    truncated[4..8].copy_from_slice(&riff_len.to_le_bytes());
    fs::write(&file, truncated).unwrap();
    reseal(&path, &mut original_manifest.clone(), "/render");
    assert!(load_fixture(&path).is_err());
    let mut extra = original.clone();
    extra.extend_from_slice(&[0, 0]);
    let data_len = u32::from_le_bytes(extra[40..44].try_into().unwrap()) + 2;
    extra[40..44].copy_from_slice(&data_len.to_le_bytes());
    let riff_len = (extra.len() - 8) as u32;
    extra[4..8].copy_from_slice(&riff_len.to_le_bytes());
    fs::write(&file, extra).unwrap();
    reseal(&path, &mut original_manifest.clone(), "/render");
    assert!(load_fixture(&path).is_err());
}

#[test]
fn resealed_silent_main_or_changed_near_end_signals_are_rejected() {
    let directory = TestDirectory::new();
    let path = directory.fixture();
    let original_manifest = manifest(&path);
    for pointer in [
        "/render",
        "/capture",
        "/near_end/render",
        "/near_end/capture",
    ] {
        let asset = original_manifest.pointer(pointer).unwrap();
        let file = path.join(asset["file"].as_str().unwrap());
        let original = fs::read(&file).unwrap();
        let mut changed = original.clone();
        if pointer.starts_with("/near_end/") {
            changed[2000] ^= 1;
        } else {
            changed[44..].fill(0);
        }
        fs::write(&file, changed).unwrap();
        reseal(&path, &mut original_manifest.clone(), pointer);
        assert!(load_fixture(&path).is_err(), "accepted changed {pointer}");
        fs::write(&file, original).unwrap();
    }
    write_manifest(&path, &original_manifest);
    assert!(load_fixture(&path).is_ok());
}

#[test]
fn existing_destinations_and_invalid_bounds_never_destroy_prior_data() {
    let directory = TestDirectory::new();
    let file = directory.0.join("existing-file");
    fs::write(&file, b"retain me").unwrap();
    assert!(export_fixture(&file, 500).is_err());
    assert_eq!(fs::read(&file).unwrap(), b"retain me");
    let empty = directory.0.join("existing-empty-directory");
    fs::create_dir(&empty).unwrap();
    assert!(export_fixture(&empty, 500).is_err());
    assert!(empty.is_dir());
    assert_eq!(fs::read_dir(&empty).unwrap().count(), 0);
    let path = directory.fixture();
    let original = fs::read(path.join("manifest.json")).unwrap();
    assert!(export_fixture(&path, 501).is_err());
    assert_eq!(fs::read(path.join("manifest.json")).unwrap(), original);
    for blocks in [0, 499, 60_001, usize::MAX] {
        let new_path = directory.0.join(format!("invalid-{blocks}"));
        assert!(export_fixture(&new_path, blocks).is_err());
        assert!(!new_path.exists());
    }
}

#[test]
fn generated_and_preloaded_benchmarks_match_quality_and_report_publication_is_exclusive() {
    let directory = TestDirectory::new();
    let path = directory.fixture();
    let loaded = load_fixture(&path).unwrap();
    let from_fixture = benchmark_loaded(&loaded).unwrap();
    let generated = benchmark(500).unwrap();
    assert_eq!(from_fixture.input_pcm_sha256, generated.input_pcm_sha256);
    assert_eq!(
        from_fixture.synthetic_echo_reduction_db,
        generated.synthetic_echo_reduction_db
    );
    assert_eq!(
        from_fixture.near_end_with_silent_render_output_input_rms_ratio,
        generated.near_end_with_silent_render_output_input_rms_ratio
    );
    assert_eq!(from_fixture.cold_start_echo, generated.cold_start_echo);
    assert_eq!(
        from_fixture.cold_start_near_end_with_silent_render,
        generated.cold_start_near_end_with_silent_render
    );
    assert_eq!(from_fixture.measured_blocks, 500);
    assert_eq!(from_fixture.warmup_blocks, 500);
    assert_eq!(from_fixture.near_end_measured_blocks, 500);
    assert_eq!(from_fixture.near_end_warmup_blocks, 500);
    assert_eq!(from_fixture.input_source, "validated_fixture_directory");
    assert_eq!(generated.input_source, "generated_in_memory");
    assert!(generated.fixture_manifest_sha256.is_none());
    assert_eq!(
        from_fixture.fixture_manifest_sha256.as_deref(),
        Some(digest(&fs::read(path.join("manifest.json")).unwrap()).as_str())
    );
    assert_eq!(from_fixture.fixture_manifest.as_ref(), loaded.manifest());
    assert_eq!(
        from_fixture.timing_scope,
        "pcm16_render_plus_capture_processing_only"
    );
    assert!(from_fixture.processing_p50_us > 0.0);
    assert!(from_fixture.processing_p95_us >= from_fixture.processing_p50_us);
    assert!(from_fixture.processing_p99_us >= from_fixture.processing_p95_us);
    assert!(from_fixture.processing_max_us >= from_fixture.processing_p99_us);
    let output = directory.0.join("report.json");
    let barrier = Barrier::new(4);
    let winners = std::thread::scope(|scope| {
        let handles: Vec<_> = (0..4)
            .map(|_| {
                scope.spawn(|| {
                    barrier.wait();
                    write_report_new(&output, &from_fixture).is_ok()
                })
            })
            .collect();
        handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .filter(|&won| won)
            .count()
    });
    assert_eq!(winners, 1);
    let bytes = fs::read(&output).unwrap();
    assert_eq!(bytes, serde_json::to_vec_pretty(&from_fixture).unwrap());
    assert!(write_report_new(&output, &generated).is_err());
    assert_eq!(fs::read(&output).unwrap(), bytes);
    assert!(write_report_new(&directory.0, &from_fixture).is_err());
    assert!(fs::read_dir(&directory.0).unwrap().all(|entry| {
        !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .ends_with(".partial")
    }));
}

#[test]
fn cli_rejects_extra_arguments_and_existing_report_before_processing() {
    let directory = TestDirectory::new();
    let fixture = directory.0.join("cli-fixture");
    let output = Command::new(env!("CARGO_BIN_EXE_lamp-audio"))
        .args([
            "export-aec-fixture",
            fixture.to_str().unwrap(),
            "500",
            "extra",
        ])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(!fixture.exists());
    let output = Command::new(env!("CARGO_BIN_EXE_lamp-audio"))
        .args(["export-aec-fixture", fixture.to_str().unwrap(), "500"])
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    let printed: FixtureManifest = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(load_fixture(&fixture).unwrap().manifest(), Some(&printed));
    let report = directory.0.join("report.json");
    fs::write(&report, b"existing result").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_lamp-audio"))
        .args([
            "bench-aec-fixture",
            "nonexistent-fixture",
            report.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("already exists"));
    assert_eq!(fs::read(&report).unwrap(), b"existing result");
}

#[cfg(unix)]
#[test]
fn symlink_assets_and_fixture_roots_are_not_followed() {
    use std::os::unix::fs::symlink;
    let directory = TestDirectory::new();
    let path = directory.fixture();
    let alias = directory.0.join("alias");
    symlink(&path, &alias).unwrap();
    assert!(load_fixture(&alias).is_err());
    let original = path.join("render.wav");
    let moved = directory.0.join("render.wav");
    fs::rename(&original, &moved).unwrap();
    symlink(&moved, &original).unwrap();
    assert!(load_fixture(&path).is_err());
    let dangling = directory.0.join("dangling");
    symlink(directory.0.join("missing"), &dangling).unwrap();
    assert!(export_fixture(&dangling, 500).is_err());
    assert!(
        fs::symlink_metadata(dangling)
            .unwrap()
            .file_type()
            .is_symlink()
    );
}
