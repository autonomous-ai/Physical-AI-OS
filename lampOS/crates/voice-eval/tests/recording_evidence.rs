//! Synthetic file-only regressions. Never opens an audio device or transport.
mod common;

use lamp_acoustic::hash;
use lamp_voice_eval::{
    annotations::{Annotations, AttemptAnnotation, StepAnnotation},
    import::{
        ImportOptions, MAX_ROOM_WAV_BYTES, import_trace, load_room_evidence, room_evidence,
        verify_room_evidence, verify_room_wav,
    },
    plan::LoadedPlan,
    record::{AttemptRecord, AttemptStatus},
    stimulus::StimulusCatalog,
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, fs, path::Path};

fn write_wav(path: &Path, channels: u16, bits: u16, float: bool) {
    let spec = hound::WavSpec {
        channels,
        sample_rate: 16_000,
        bits_per_sample: bits,
        sample_format: if float {
            hound::SampleFormat::Float
        } else {
            hound::SampleFormat::Int
        },
    };
    let mut writer = hound::WavWriter::create(path, spec).unwrap();
    for _ in 0..32_000 * u32::from(channels) {
        if float {
            writer.write_sample(0.125_f32).unwrap();
        } else {
            writer.write_sample(512_i32).unwrap();
        }
    }
    writer.finalize().unwrap();
}

fn import(dir: &Path, metadata: &Path) -> AttemptRecord {
    let events = dir.join("events.jsonl");
    fs::write(
        &events,
        concat!(
            "{\"kind\":\"run_start\",\"at_us\":1,\"provider_kind\":\"one_cached_reply\"}\n",
            "{\"kind\":\"run_end\",\"at_us\":2,\"status\":\"completed_unscored\",\"error\":null}\n"
        ),
    )
    .unwrap();
    let catalog = StimulusCatalog::load().unwrap();
    let plan = LoadedPlan::default_plan(&catalog).unwrap();
    let out = dir.join("run");
    lamp_voice_eval::ledger::create_run_directory(&out).unwrap();
    import_trace(
        &plan,
        &catalog,
        &ImportOptions {
            run_id: "recording-evidence".into(),
            events_path: &events,
            scenario: "fixed-reply-echo-only".into(),
            turn_map: Vec::new(),
            room_metadata: Some(metadata),
            room_independent: true,
        },
        &out,
    )
    .unwrap()
}

struct Fixture {
    dir: common::Private,
    sha: String,
    metadata: Value,
    record: AttemptRecord,
}

impl Fixture {
    fn new() -> Self {
        let dir = common::Private::new("wav-evidence");
        let wav = dir.join("room.wav");
        write_wav(&wav, 1, 16, false);
        let sha = hash(&fs::read(&wav).unwrap());
        let metadata = json!({
            "valid": true, "status": "captured_unscored", "room_wav_sha256": sha,
            "requested_frames": 32000, "written_duration_s": 2.0,
            "writer": {"written_frames": 32000, "source_sequence_gaps": 0},
            "capture": {"written_frames": 32000},
            "delivered_format": {"sample_rate": 16000, "channels": 1}, "errors": []
        });
        let path = dir.join("metadata.json");
        fs::write(&path, metadata.to_string()).unwrap();
        let record = import(&dir.path, &path);
        Self {
            dir,
            sha,
            metadata,
            record,
        }
    }

    fn annotations(&self) -> Annotations {
        Annotations {
            schema: 1,
            run_id: self.record.run_id.clone(),
            annotator: "synthetic test".into(),
            method: "synthetic times, not acoustic evidence".into(),
            attempts: BTreeMap::from([(
                self.record.attempt_id.clone(),
                AttemptAnnotation {
                    room_recording_sha256: Some(self.sha.clone()),
                    listened: true,
                    steps: BTreeMap::from([(
                        "greet".into(),
                        StepAnnotation {
                            user_speech_end_s: Some(0.25),
                            first_substantive_answer_word_s: Some(1.25),
                            answer_relevant: Some(true),
                            answer_complete: Some(true),
                            ..StepAnnotation::default()
                        },
                    )]),
                    notes: None,
                },
            )]),
        }
    }

    fn unscored(&self, annotations: &Annotations, contains: &str) {
        let scored = annotations.score(&self.record);
        assert_eq!(scored.len(), 1);
        assert!(scored[0].measurement().is_none());
        assert!(
            scored[0]
                .unscored_reason
                .as_deref()
                .unwrap()
                .contains(contains),
            "{:?}",
            scored[0]
        );
        assert!(annotations.reviewed_step(&self.record, "greet").is_none());
        assert_eq!(self.record.status, AttemptStatus::Completed);
    }
}

fn step_mut<'a>(
    annotations: &'a mut Annotations,
    record: &AttemptRecord,
) -> &'a mut StepAnnotation {
    annotations
        .attempts
        .get_mut(&record.attempt_id)
        .unwrap()
        .steps
        .get_mut("greet")
        .unwrap()
}

#[test]
fn actual_wav_hash_format_frames_and_duration_are_verified() {
    let f = Fixture::new();
    let room = &f.record.evidence["room_audio"];
    assert_eq!(room["valid"], true);
    assert_eq!(room["verified_wav"]["frames"], 32000);
    assert_eq!(room["verified_wav"]["duration_s"], 2.0);
    assert!(Path::new(room["wav_path"].as_str().unwrap()).is_absolute());
    assert_eq!(verify_room_evidence(room).unwrap().sha256, f.sha);
    let annotations = f.annotations();
    assert_eq!(annotations.score(&f.record)[0].value_ms, Some(1000.0));
    assert!(annotations.reviewed_step(&f.record, "greet").is_some());
}

#[test]
fn observer_float_and_extensible_multichannel_pcm_are_supported() {
    let dir = common::Private::new("wav-formats");
    for (channels, bits, float) in [
        (1, 16, false),
        (2, 24, false),
        (3, 32, false),
        (2, 32, true),
        (8, 32, true),
    ] {
        let path = dir.join("room.wav");
        write_wav(&path, channels, bits, float);
        let verified = verify_room_wav(&path, &hash(&fs::read(&path).unwrap())).unwrap();
        assert_eq!(verified.channels, channels);
        assert_eq!(verified.bits_per_sample, bits);
        assert_eq!(verified.float_samples, float);
        assert_eq!(verified.frames, 32000);
    }
}

#[test]
fn matching_metadata_and_annotation_strings_cannot_mint_acoustic_evidence() {
    let mut f = Fixture::new();
    f.record.evidence["room_audio"] = json!({"valid":true,"sha256":f.sha});
    f.unscored(&f.annotations(), "integrity");
}

#[test]
fn removing_wav_after_import_invalidates_report_regeneration() {
    let f = Fixture::new();
    let annotations = f.annotations();
    assert!(annotations.score(&f.record)[0].measurement().is_some());
    fs::remove_file(f.dir.join("room.wav")).unwrap();
    f.unscored(&annotations, "No such file");
}

#[test]
fn same_length_replacement_after_import_is_not_the_reviewed_recording() {
    let f = Fixture::new();
    let path = f.dir.join("room.wav");
    let mut bytes = fs::read(&path).unwrap();
    let last = bytes.len() - 1;
    bytes[last] ^= 1;
    fs::write(path, bytes).unwrap();
    f.unscored(&f.annotations(), "SHA-256");
}

#[test]
fn truncated_riff_and_payload_are_rejected_even_with_their_new_hash() {
    let f = Fixture::new();
    let path = f.dir.join("room.wav");
    let mut bytes = fs::read(&path).unwrap();
    bytes.truncate(bytes.len() - 10);
    fs::write(&path, &bytes).unwrap();
    assert!(
        verify_room_wav(&path, &hash(&bytes))
            .unwrap_err()
            .to_string()
            .contains("declared length")
    );
    let len = bytes.len() as u32 - 8;
    bytes[4..8].copy_from_slice(&len.to_le_bytes());
    fs::write(&path, &bytes).unwrap();
    assert!(
        verify_room_wav(&path, &hash(&bytes))
            .unwrap_err()
            .to_string()
            .contains("truncated WAV chunk")
    );
}

#[test]
fn incomplete_sample_frame_and_nonfinite_float_are_rejected() {
    let dir = common::Private::new("wav-samples");
    let path = dir.join("room.wav");
    write_wav(&path, 1, 16, false);
    let mut bytes = fs::read(&path).unwrap();
    let data = bytes.windows(4).position(|bytes| bytes == b"data").unwrap();
    let length = u32::from_le_bytes(bytes[data + 4..data + 8].try_into().unwrap()) - 1;
    bytes[data + 4..data + 8].copy_from_slice(&length.to_le_bytes());
    fs::write(&path, &bytes).unwrap();
    assert!(
        verify_room_wav(&path, &hash(&bytes))
            .unwrap_err()
            .to_string()
            .contains("partial WAV sample")
    );
    for value in [f32::NAN, f32::INFINITY, f32::NEG_INFINITY] {
        write_wav(&path, 1, 32, true);
        let mut bytes = fs::read(&path).unwrap();
        let data = bytes.windows(4).position(|bytes| bytes == b"data").unwrap();
        bytes[data + 8..data + 12].copy_from_slice(&value.to_le_bytes());
        fs::write(&path, &bytes).unwrap();
        assert!(
            verify_room_wav(&path, &hash(&bytes))
                .unwrap_err()
                .to_string()
                .contains("nonfinite")
        );
    }
}

#[test]
fn empty_and_over_byte_limit_files_fail_before_sample_allocation() {
    let dir = common::Private::new("wav-bounds");
    let path = dir.join("room.wav");
    let writer = hound::WavWriter::create(
        &path,
        hound::WavSpec {
            channels: 1,
            sample_rate: 16000,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        },
    )
    .unwrap();
    writer.finalize().unwrap();
    assert!(
        verify_room_wav(&path, &hash(&fs::read(&path).unwrap()))
            .unwrap_err()
            .to_string()
            .contains("empty")
    );
    fs::File::options()
        .write(true)
        .open(&path)
        .unwrap()
        .set_len(MAX_ROOM_WAV_BYTES + 1)
        .unwrap();
    assert!(
        verify_room_wav(&path, &"0".repeat(64))
            .unwrap_err()
            .to_string()
            .contains("bounded regular")
    );
}

#[test]
fn duration_rate_and_channel_bounds_are_enforced() {
    let dir = common::Private::new("wav-format-bounds");
    let path = dir.join("room.wav");
    write_wav(&path, 1, 16, false);
    let original = fs::read(&path).unwrap();
    let fmt = original
        .windows(4)
        .position(|bytes| bytes == b"fmt ")
        .unwrap()
        + 8;
    for (offset, replacement) in [
        (fmt + 2, 9_u16.to_le_bytes().to_vec()),
        (fmt + 4, 7999_u32.to_le_bytes().to_vec()),
    ] {
        let mut bytes = original.clone();
        bytes[offset..offset + replacement.len()].copy_from_slice(&replacement);
        fs::write(&path, &bytes).unwrap();
        assert!(verify_room_wav(&path, &hash(&bytes)).is_err());
    }
    // Sparse, structurally complete 600 seconds plus one frame. The duration
    // guard rejects it before reading the sample payload.
    let data = original
        .windows(4)
        .position(|bytes| bytes == b"data")
        .unwrap();
    let mut header = original[..data + 8].to_vec();
    let payload = (16_000_u32 * 600 + 1) * 2;
    let total = header.len() as u32 + payload;
    header[4..8].copy_from_slice(&(total - 8).to_le_bytes());
    header[data + 4..data + 8].copy_from_slice(&payload.to_le_bytes());
    fs::write(&path, header).unwrap();
    fs::File::options()
        .write(true)
        .open(&path)
        .unwrap()
        .set_len(u64::from(total))
        .unwrap();
    assert!(
        verify_room_wav(&path, &"0".repeat(64))
            .unwrap_err()
            .to_string()
            .contains("600 seconds")
    );
}

#[test]
fn symlink_and_nonregular_paths_cannot_be_recordings() {
    let f = Fixture::new();
    let link = f.dir.join("alias.wav");
    std::os::unix::fs::symlink(f.dir.join("room.wav"), &link).unwrap();
    assert!(verify_room_wav(&link, &f.sha).is_err());
    assert!(verify_room_wav(&f.dir.path, &f.sha).is_err());
}

#[test]
fn wrong_run_and_outside_recording_boundaries_are_unscored() {
    let f = Fixture::new();
    let mut annotations = f.annotations();
    annotations.run_id = "another-run".into();
    f.unscored(&annotations, "run_id");
    for value in [2.00001, -0.01, f64::NAN, f64::INFINITY] {
        let mut annotations = f.annotations();
        step_mut(&mut annotations, &f.record).first_substantive_answer_word_s = Some(value);
        f.unscored(&annotations, "out-of-recording");
    }
    let mut annotations = f.annotations();
    step_mut(&mut annotations, &f.record).first_substantive_answer_word_s = Some(2.0);
    assert_eq!(annotations.score(&f.record)[0].value_ms, Some(1750.0));
}

#[test]
fn interruption_times_and_uncertainties_are_checked_too() {
    let f = Fixture::new();
    for bad in [
        StepAnnotation {
            interruption_onset_s: Some(f64::NAN),
            ..StepAnnotation::default()
        },
        StepAnnotation {
            lamp_silent_s: Some(2.001),
            ..StepAnnotation::default()
        },
        StepAnnotation {
            user_speech_end_uncertainty_ms: Some(f64::INFINITY),
            ..StepAnnotation::default()
        },
        StepAnnotation {
            answer_onset_uncertainty_ms: Some(2001.0),
            ..StepAnnotation::default()
        },
        StepAnnotation {
            silence_uncertainty_ms: Some(-1.0),
            ..StepAnnotation::default()
        },
    ] {
        let mut annotations = f.annotations();
        *step_mut(&mut annotations, &f.record) = bad;
        f.unscored(&annotations, "out-of-recording");
    }
}

#[test]
fn reversed_intervals_do_not_erase_independent_content_review() {
    let f = Fixture::new();
    let mut annotations = f.annotations();
    let step = step_mut(&mut annotations, &f.record);
    step.user_speech_end_s = Some(1.75);
    step.answer_relevant = Some(false);
    let scored = annotations.score(&f.record);
    assert!(scored[0].measurement().is_none());
    assert!(
        scored[0]
            .unscored_reason
            .as_deref()
            .unwrap()
            .contains("negative")
    );
    assert_eq!(
        annotations
            .reviewed_step(&f.record, "greet")
            .unwrap()
            .answer_relevant,
        Some(false)
    );
}

#[test]
fn failed_or_timed_out_recorder_cannot_reuse_valid_metadata() {
    let f = Fixture::new();
    let room = room_evidence(
        &f.metadata,
        true,
        "synthetic metadata",
        &f.dir.join("room.wav"),
        Some(false),
    );
    assert_eq!(room["valid"], false);
    assert!(
        room["verification_error"]
            .as_str()
            .unwrap()
            .contains("recorder failed")
    );
    assert!(verify_room_evidence(&room).is_err());
    let mut room = room_evidence(
        &f.metadata,
        true,
        "synthetic metadata",
        &f.dir.join("room.wav"),
        Some(true),
    );
    assert_eq!(room["valid"], true);
    room["recorder_exit"] = json!(1);
    assert!(verify_room_evidence(&room).is_err());
    room["recorder_exit"] = Value::Null;
    assert!(verify_room_evidence(&room).is_err());
}

#[test]
fn observer_frame_format_and_error_claims_must_match_actual_bytes() {
    let f = Fixture::new();
    for (pointer, bad) in [
        ("/requested_frames", json!(32001)),
        ("/writer/written_frames", json!(31999)),
        ("/capture/written_frames", json!(31999)),
        ("/writer/source_sequence_gaps", json!(1)),
        ("/delivered_format/channels", json!(2)),
        ("/delivered_format/sample_rate", json!(48000)),
        ("/written_duration_s", json!(1.9)),
        ("/errors", json!(["writer failed"])),
    ] {
        let mut metadata = f.metadata.clone();
        *metadata.pointer_mut(pointer).unwrap() = bad;
        let room = room_evidence(
            &metadata,
            true,
            "synthetic metadata",
            &f.dir.join("room.wav"),
            None,
        );
        assert_eq!(room["valid"], false, "{pointer}");
        assert!(room["verification_error"].is_string());
    }
}

#[test]
fn missing_or_corrupt_metadata_retains_the_attempt_and_reason() {
    for bytes in [None, Some("not json")] {
        let dir = common::Private::new("missing-metadata");
        let path = dir.join("metadata.json");
        if let Some(bytes) = bytes {
            fs::write(&path, bytes).unwrap();
        }
        let record = import(&dir.path, &path);
        assert_eq!(record.status, AttemptStatus::Completed);
        assert_eq!(record.evidence["room_audio"]["valid"], false);
        assert!(record.evidence["room_audio"]["verification_error"].is_string());
        let (rows, truncated) = lamp_voice_eval::ledger::read_rows(&dir.join("run")).unwrap();
        assert!(!truncated);
        let finished = rows
            .iter()
            .find(|row| row["row"] == "attempt_finished")
            .unwrap();
        assert_eq!(finished["record"]["evidence"]["room_audio"]["valid"], false);
        assert_eq!(load_room_evidence(&path, true, None)["valid"], false);
    }
}

#[test]
fn valid_metadata_without_its_recording_is_retained_as_invalid_evidence() {
    let dir = common::Private::new("missing-wav");
    let path = dir.join("metadata.json");
    fs::write(
        &path,
        json!({"valid":true,"status":"captured_unscored","room_wav_sha256":"a".repeat(64)})
            .to_string(),
    )
    .unwrap();
    let record = import(&dir.path, &path);
    assert_eq!(record.evidence["room_audio"]["valid"], false);
    assert!(record.evidence["room_audio"]["verification_error"].is_string());
    let (rows, truncated) = lamp_voice_eval::ledger::read_rows(&dir.join("run")).unwrap();
    assert!(!truncated);
    assert!(rows.iter().any(|row| row["row"] == "attempt_finished"));
}

#[test]
fn unrepresentable_path_is_an_invalid_receipt_not_a_serialization_panic() {
    use std::{ffi::OsString, os::unix::ffi::OsStringExt};
    let f = Fixture::new();
    let path = f.dir.path.join(OsString::from_vec(vec![0xff]));
    // Some filesystems cannot create this name. It must be rejected before
    // file access so the invalid receipt itself is always serializable.
    let evidence = room_evidence(&f.metadata, true, "synthetic metadata", &path, None);
    assert_eq!(evidence["valid"], false);
    assert!(
        evidence["verification_error"]
            .as_str()
            .unwrap()
            .contains("UTF-8")
    );
}

#[test]
fn annotation_parser_rejects_negative_values_without_using_software_hints() {
    let f = Fixture::new();
    let mut annotations = f.annotations();
    step_mut(&mut annotations, &f.record).user_speech_end_s = Some(-0.1);
    assert!(Annotations::parse(&serde_json::to_vec(&annotations).unwrap()).is_err());
    let mut annotations = f.annotations();
    step_mut(&mut annotations, &f.record).hint_stimulus_start_s = Some(99.0);
    assert_eq!(
        annotations.score(&f.record)[0].value_ms,
        Some(1000.0),
        "software hint is never a timing boundary"
    );
}
