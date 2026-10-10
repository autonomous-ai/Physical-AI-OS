#![cfg(unix)]
use lamp_audio::{
    EchoProcessor,
    replay::{self, HistoricalProcessing, ReplayOptions},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write,
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};

static NEXT: AtomicU64 = AtomicU64::new(0);
const BOOT: [u8; 16] = [3; 16];
struct Fixture {
    root: PathBuf,
    run: PathBuf,
    capture: Vec<Value>,
    render: Vec<Value>,
    pre: Vec<i16>,
    post: Vec<i16>,
    accepted: Vec<i16>,
    valid: bool,
    processing: Option<Value>,
}
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "lamp-replay-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
        fs::create_dir(root.join("artifacts")).unwrap();
        let run = root.join("run");
        fs::create_dir(&run).unwrap();
        for leaf in ["capture-audio", "render-audio"] {
            fs::create_dir(run.join(leaf)).unwrap();
        }
        let mut capture = vec![
            row(
                "privacy",
                json!({"at_us":1,"generation":2,"open":true}),
                0,
                0,
                0,
                None,
            ),
            row("reset", reset(20, "start", 1, 1, None), 0, 0, 0, None),
        ];
        let mut render = vec![
            row(
                "privacy",
                json!({"at_us":1,"generation":2,"open":true}),
                0,
                0,
                0,
                None,
            ),
            row("reset", reset(2, "start", 1, 1, None), 0, 0, 0, None),
        ];
        let mut accepted = vec![0; 480];
        let first: [i16; 240] = std::array::from_fn(|i| ((i as i32 * 317) % 16000 - 8000) as i16);
        let second: [i16; 240] = std::array::from_fn(|i| ((i as i32 * 263) % 12000 - 6000) as i16);
        accepted.extend(first);
        accepted.extend(second);
        for (begin, count, at, prime) in [
            (0, 120, 3, true),
            (120, 120, 4, true),
            (240, 240, 5, true),
            (480, 120, 21, false),
            (600, 120, 22, false),
            (720, 240, 41, false),
        ] {
            render.push(row("render_accepted",json!({"playback_epoch":1,"privacy_generation":2,"first_sample":begin,"end_sample":begin+count,"accepted_at_us":at,"queue_observed_at_us":at,"queued_frames":480,"owner":null,"chunk_sequence":null}),count,0,begin*2,prime.then_some("prime")));
        }
        let mut processor = EchoProcessor::new(true);
        processor.render(&[0; 240]).unwrap();
        processor.render(&[0; 240]).unwrap();
        let mut pre = Vec::new();
        let mut post = Vec::new();
        for index in 0..3 {
            if index == 0 {
                processor.render(&first).unwrap();
            } else if index == 1 {
                processor.render(&second).unwrap();
            }
            let mut samples: [i16; 160] =
                std::array::from_fn(|i| ((i as i32 * 191 + index * 337) % 12000 - 6000) as i16);
            if index == 0 {
                samples[0] = i16::MAX;
            }
            post.extend(processor.capture(&samples, 18).unwrap());
            pre.extend(samples);
            let at = 30 + index as u64 * 20;
            let analysed = if index == 0 { 720 } else { 960 };
            let reference_at = if index == 0 { 22 } else { 41 };
            capture.push(row("capture",json!({"epoch":1,"dsp_epoch":1,"privacy_generation":2,"frame_sequence":index+1,"first_read_started_at_us":at,"last_read_started_at_us":at,"read_completed_at_us":at+2,"successful_reads":1,"alsa_status_monotonic_us":at+2,"status_observed_at_us":at+3,"available_frames":0,"delayed_frames":32,"processing_completed_at_us":at+5,"aec_queue_delay_ms":18,"reference":{"playback_epoch":1,"accepted_through":analysed,"analysed_through":analysed,"capture_blocks_processed":index+1,"accepted_at_us":reference_at,"analysed_at_us":at-2,"queue_observed_at_us":reference_at,"queued_frames":480,"clock_started_at_us":21,"ignored_old_epoch_packets":0},"vad_score":0.4}),160,index as u64*320,0,None));
        }
        capture.push(row(
            "end",
            json!({"at_us":90,"reason":"stopped"}),
            0,
            960,
            0,
            None,
        ));
        render.push(row(
            "end",
            json!({"at_us":90,"reason":"stopped"}),
            0,
            0,
            1920,
            None,
        ));
        let fixture = Self {
            root,
            run,
            capture,
            render,
            pre,
            post,
            accepted,
            valid: true,
            processing: Some(json!({"aec":"sonora_aec3","noise_suppression":true})),
        };
        fixture.save();
        fixture
    }
    fn output(&self, name: &str) -> PathBuf {
        self.root.join("artifacts").join(name)
    }
    fn save(&self) {
        let mut trace = Vec::new();
        for (kind, leaf, rows, pre, post, accepted, worker_boot) in [
            (
                "capture",
                "capture-audio",
                &self.capture,
                self.pre.as_slice(),
                self.post.as_slice(),
                &[][..],
                [4u8; 16],
            ),
            (
                "render",
                "render-audio",
                &self.render,
                &[][..],
                &[][..],
                self.accepted.as_slice(),
                [5u8; 16],
            ),
        ] {
            let directory = self.run.join(leaf);
            let mut events = Vec::new();
            for (index, row) in rows.iter().enumerate() {
                let mut row = row.clone();
                row["sequence"] = json!(index + 1);
                if row["kind"] == "end" && !self.valid {
                    row["meta"]["reason"] = json!("fault");
                }
                events.extend(serde_json::to_vec(&row).unwrap());
                events.push(b'\n');
            }
            let values = [
                ("events.jsonl", events),
                ("pre_aec.pcm16le", pcm(pre)),
                ("post_aec.pcm16le", pcm(post)),
                ("render_accepted.pcm16le", pcm(accepted)),
            ];
            for (name, bytes) in &values {
                write(&directory.join(name), bytes);
            }
            let mut pending = json!({"schema_version":1,"boot":BOOT,"stream":kind,"started_at_us":0,"max_seconds":1,"max_bytes":16*1024*1024});
            if kind == "capture"
                && let Some(processing) = &self.processing
            {
                pending["software_processing"] = processing.clone();
            }
            write(
                &directory.join("manifest.pending.json"),
                &serde_json::to_vec(&pending).unwrap(),
            );
            let ended_at = rows.last().unwrap()["meta"]["at_us"].as_u64().unwrap();
            let finalized_at = ended_at + 10;
            let zeros = rows
                .iter()
                .filter(|r| !r["accepted_silence_kind"].is_null())
                .map(|r| r["frames"].as_u64().unwrap())
                .sum::<u64>();
            let mut result = json!({"boot":BOOT,"stream":kind,"started_at_us":0,"finalized_at_us":finalized_at,"valid":self.valid,"faults":if self.valid{0}else{2048},"records_queued":rows.len(),"records_written":rows.len(),"records_rejected":0,"capture_frames":pre.len(),"render_frames":accepted.len(),"accepted_zero_frames":zeros,"pre_aec_sha256":hash(&values[1].1),"post_aec_sha256":hash(&values[2].1),"render_accepted_sha256":hash(&values[3].1),"events_sha256":hash(&values[0].1),"end":{"at_us":ended_at,"reason":if self.valid{"stopped"}else{"fault"}}});
            if kind == "capture"
                && let Some(processing) = &self.processing
            {
                result["software_processing"] = processing.clone();
            }
            let bytes = serde_json::to_vec(&result).unwrap();
            write(&directory.join("result.json"), &bytes);
            let complete = directory.join("complete.json");
            if complete.exists() {
                fs::remove_file(&complete).unwrap();
            }
            if self.valid {
                write(&complete, &bytes);
            }
            let identity = json!({"boot":BOOT,"stream":kind,"started_at_us":0});
            let worker = if kind == "capture" {
                "capture"
            } else {
                "speaker"
            };
            trace.push(json!({"kind":"audio_diagnostics_started","worker":worker,"worker_boot":worker_boot,"details":identity}));
            let mut receipt = json!({"started":identity,"valid":self.valid,"acknowledged":true,"outcome":if self.valid{"complete"}else{"invalid"},"faults":if self.valid{0}else{2048},"completion_marker_published":self.valid,"completion_sha256":hash(&bytes),"finalized_at_us":finalized_at});
            for field in [
                "records_written",
                "records_rejected",
                "capture_frames",
                "render_frames",
                "accepted_zero_frames",
            ] {
                receipt[field] = result[field].clone();
            }
            trace.push(json!({"kind":"audio_diagnostics_certified","worker":worker,"worker_boot":worker_boot,"valid":self.valid,"receipt":receipt}));
        }
        write(&self.run.join("events.jsonl"), &jsonl(&trace));
    }
    fn replay(
        &self,
        name: &str,
        options: ReplayOptions,
    ) -> lamp_audio::Result<replay::ReplayReport> {
        replay::replay(&self.root, &self.run, &self.output(name), options)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}
fn write(path: &Path, bytes: &[u8]) {
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path)
        .unwrap();
    file.write_all(bytes).unwrap();
}
fn row(kind: &str, meta: Value, frames: u64, pre: u64, render: u64, zero: Option<&str>) -> Value {
    json!({"kind":kind,"sequence":0,"meta":meta,"frames":frames,"accepted_silence_kind":zero,"pre_aec_byte_offset":pre,"post_aec_byte_offset":pre,"render_accepted_byte_offset":render})
}
fn reset(at: u64, reason: &str, dsp: u64, playback: u64, discard: Option<Value>) -> Value {
    json!({"at_us":at,"reason":reason,"capture_epoch":1,"dsp_epoch":dsp,"playback_epoch":playback,"privacy_generation":2,"discarded":discard})
}
fn pcm(samples: &[i16]) -> Vec<u8> {
    samples.iter().flat_map(|v| v.to_le_bytes()).collect()
}
fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn jsonl(rows: &[Value]) -> Vec<u8> {
    let mut bytes = Vec::new();
    for row in rows {
        bytes.extend(serde_json::to_vec(row).unwrap());
        bytes.push(b'\n');
    }
    bytes
}

#[test]
fn certified_partial_writes_reproduce_recorded_call_order_and_exact_ns_pcm() {
    let fixture = Fixture::new();
    let report = fixture.replay("paired", ReplayOptions::default()).unwrap();
    assert!(report.source_certified);
    assert!(report.replay_complete_for_recorded_capture);
    assert_eq!(report.replayed_capture_frames, 480);
    assert_eq!(report.segments.len(), 1);
    assert_eq!(report.segments[0].aec_ns_mismatched_samples_vs_recorded, 0);
    assert_eq!(report.segments[0].render_blocks, 4);
    assert_eq!(report.segments[0].pre_aec.saturated_samples, 1);
    assert_eq!(report.word_error_rate, None);
    assert_eq!(report.acoustic_quality_verdict, None);
    let wav =
        hound::WavReader::open(fixture.output("paired").join("segment-001-aec_ns.wav")).unwrap();
    assert_eq!(wav.spec().sample_rate, 16000);
    assert_eq!(
        wav.into_samples::<i16>()
            .collect::<std::result::Result<Vec<_>, _>>()
            .unwrap(),
        fixture.post
    );
    let operations = fs::read_to_string(fixture.output("paired").join("operations.jsonl")).unwrap();
    let rows: Vec<Value> = operations
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(rows[0]["source_meta"]["generation"], 2);
    assert_eq!(rows[0]["source_meta"]["open"], true);
    let order: Vec<_> = rows
        .iter()
        .filter_map(|v| match v["kind"].as_str() {
            Some("render_before_capture") => Some("render"),
            Some("capture_pair") => Some("capture"),
            _ => None,
        })
        .collect();
    assert_eq!(
        order,
        vec![
            "render", "render", "render", "capture", "render", "capture", "capture"
        ]
    );
}
#[test]
fn replay_preserves_start_request_bounds_without_inventing_legacy_evidence() {
    for requested in [None, Some(20)] {
        let mut fixture = Fixture::new();
        if let Some(requested) = requested {
            for row in &mut fixture.capture {
                if row["kind"] == "capture" {
                    row["meta"]["reference"]["clock_start_requested_at_us"] = json!(requested);
                }
            }
            fixture.save();
        }
        let report = fixture
            .replay("clock-bounds", ReplayOptions::default())
            .unwrap();
        assert!(report.replay_complete_for_recorded_capture);
        assert_eq!(report.segments[0].aec_ns_mismatched_samples_vs_recorded, 0);
        let operations =
            fs::read_to_string(fixture.output("clock-bounds").join("operations.jsonl")).unwrap();
        let captures: Vec<Value> = operations
            .lines()
            .map(|line| serde_json::from_str::<Value>(line).unwrap())
            .filter(|row| row["kind"] == "capture_pair")
            .collect();
        assert_eq!(captures.len(), 3);
        for row in captures {
            let reference = &row["meta"]["reference"];
            assert_eq!(reference["clock_started_at_us"], 21);
            assert_eq!(
                reference
                    .get("clock_start_requested_at_us")
                    .and_then(Value::as_u64),
                requested
            );
            assert_eq!(
                reference.get("clock_start_requested_at_us").is_some(),
                requested.is_some()
            );
        }
    }
}

#[test]
fn fault_prefix_requires_explicit_opt_in_and_missing_mode_is_not_inferred() {
    let mut fixture = Fixture::new();
    fixture.valid = false;
    fixture.processing = None;
    fixture.save();
    assert!(fixture.replay("refused", ReplayOptions::default()).is_err());
    assert!(!fixture.output("refused").exists());
    let report = fixture
        .replay(
            "exploratory",
            ReplayOptions {
                exploratory_prefix: true,
                ..Default::default()
            },
        )
        .unwrap();
    assert!(!report.source_certified);
    assert_eq!(
        report.processing_provenance["kind"],
        "unspecified_in_recording"
    );
    assert_eq!(
        report.source_classification,
        "exploratory_fault_prefix_not_benchmark_evidence"
    );
    let evidence = fixture.root.join("source.json");
    write(
        &evidence,
        &serde_json::to_vec(&json!({"source_id":"a".repeat(64)})).unwrap(),
    );
    let report = fixture
        .replay(
            "historical",
            ReplayOptions {
                exploratory_prefix: true,
                historical_processing: Some(HistoricalProcessing {
                    noise_suppression: true,
                    source_evidence: evidence,
                }),
            },
        )
        .unwrap();
    assert_eq!(
        report.processing_provenance["kind"],
        "operator_asserted_historical_source_not_runtime_mode_receipt"
    );
    assert!(!report.source_certified);
}
#[test]
fn marker_without_timely_receipt_or_with_timeout_cannot_certify() {
    for timeout in [false, true] {
        let fixture = Fixture::new();
        let path = fixture.run.join("events.jsonl");
        let mut rows: Vec<Value> = fs::read_to_string(&path)
            .unwrap()
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        if timeout {
            rows[1]["receipt"]["outcome"] = json!("timed_out");
            rows[1]["receipt"]["acknowledged"] = json!(false);
        } else {
            rows.retain(|v| v["kind"] != "audio_diagnostics_certified");
        }
        write(&path, &jsonl(&rows));
        assert!(fixture.replay("refused", ReplayOptions::default()).is_err());
    }
}
#[test]
fn missing_prime_and_analysis_gaps_stop_without_padding_or_relabeling() {
    for prime in [false, true] {
        let mut fixture = Fixture::new();
        if prime {
            for row in &mut fixture.render {
                if row["accepted_silence_kind"] == "prime" {
                    row["accepted_silence_kind"] = Value::Null;
                }
            }
        } else {
            fixture.capture[3]["meta"]["reference"]["analysed_through"] = json!(1200);
            fixture.capture[3]["meta"]["reference"]["accepted_through"] = json!(1200);
        }
        fixture.save();
        let report = fixture.replay("stopped", ReplayOptions::default()).unwrap();
        assert!(!report.replay_complete_for_recorded_capture);
        assert_eq!(report.replayed_capture_frames, if prime { 0 } else { 160 });
        assert!(
            report
                .limitations
                .iter()
                .any(|s| s.contains("replay stopped"))
        );
    }
}
#[test]
fn changed_hash_privacy_sequence_and_byte_offsets_are_not_exploratory_repairs() {
    for mutation in 0..4 {
        let mut fixture = Fixture::new();
        match mutation {
            0 => {
                fixture.pre[0] = 0;
                write(
                    &fixture.run.join("capture-audio/pre_aec.pcm16le"),
                    &pcm(&fixture.pre),
                );
            }
            1 => {
                fixture.capture[0]["meta"]["open"] = json!(false);
                fixture.save();
            }
            2 => {
                fixture.capture[3]["meta"]["frame_sequence"] = json!(9);
                fixture.save();
            }
            _ => {
                fixture.capture[3]["pre_aec_byte_offset"] = json!(0);
                fixture.save();
            }
        }
        let result = fixture.replay(
            "invalid",
            ReplayOptions {
                exploratory_prefix: true,
                ..Default::default()
            },
        );
        if mutation == 2 {
            let report = result.unwrap();
            assert!(!report.replay_complete_for_recorded_capture);
            assert_eq!(report.replayed_capture_frames, 160);
        } else {
            assert!(result.is_err());
        }
    }
}
#[test]
fn output_is_exclusive_private_and_limited_to_workspace_artifact_roots() {
    let fixture = Fixture::new();
    fixture.replay("one", ReplayOptions::default()).unwrap();
    assert!(fixture.replay("one", ReplayOptions::default()).is_err());
    assert!(
        replay::replay(
            &fixture.root,
            &fixture.run,
            &fixture.root.join("outside"),
            ReplayOptions::default()
        )
        .is_err()
    );
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(
        fs::metadata(fixture.output("one"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o700
    );
    assert_eq!(
        fs::metadata(fixture.output("one").join("segment-001-aec_ns.wav"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    let input = fixture.run.join("capture-audio/pre_aec.pcm16le");
    let original = fixture.root.join("original");
    fs::rename(&input, &original).unwrap();
    std::os::unix::fs::symlink(&original, &input).unwrap();
    assert!(fixture.replay("symlink", ReplayOptions::default()).is_err());
}

#[test]
fn dsp_reset_preserves_mic_sequence_splits_wavs_and_uses_new_actual_prime() {
    for wrong_sequence in [false, true] {
        let mut fixture = Fixture::new();
        fixture.capture.pop();
        fixture.render.pop();
        let discard = json!({"playback_epoch":1,"retired_through":950,"accepted_through":960});
        fixture.render.push(row(
            "reset",
            reset(95, "playback_reset", 2, 2, Some(discard.clone())),
            0,
            0,
            1920,
            None,
        ));
        for (first, at) in [(0, 96), (240, 97)] {
            fixture.render.push(row("render_accepted",json!({"playback_epoch":2,"privacy_generation":2,"first_sample":first,"end_sample":first+240,"accepted_at_us":at,"queue_observed_at_us":at,"queued_frames":480,"owner":null,"chunk_sequence":null}),240,0,1920+first*2,Some("prime")));
        }
        fixture.accepted.extend([0; 480]);
        fixture.capture.push(row(
            "reset",
            reset(100, "dsp_reset", 2, 2, Some(discard)),
            0,
            960,
            0,
            None,
        ));
        let mut meta = fixture.capture[2]["meta"].clone();
        meta["dsp_epoch"] = json!(2);
        meta["frame_sequence"] = json!(if wrong_sequence { 1 } else { 4 });
        for field in [
            "first_read_started_at_us",
            "last_read_started_at_us",
            "read_completed_at_us",
            "status_observed_at_us",
            "processing_completed_at_us",
            "alsa_status_monotonic_us",
        ] {
            meta[field] = json!(110);
        }
        meta["reference"] = json!({"playback_epoch":2,"accepted_through":480,"analysed_through":480,"capture_blocks_processed":1,"capture_blocks_before_clock_start":1,"accepted_at_us":97,"analysed_at_us":100,"queue_observed_at_us":97,"queued_frames":480,"clock_started_at_us":null,"ignored_old_epoch_packets":0});
        fixture
            .capture
            .push(row("capture", meta, 160, 960, 0, None));
        let samples = [300i16; 160];
        fixture.pre.extend(samples);
        let mut processor = EchoProcessor::new(true);
        processor.render(&[0; 240]).unwrap();
        processor.render(&[0; 240]).unwrap();
        fixture
            .post
            .extend(processor.capture(&samples, 18).unwrap());
        fixture.capture.push(row(
            "end",
            json!({"at_us":120,"reason":"stopped"}),
            0,
            1280,
            0,
            None,
        ));
        fixture.render.push(row(
            "end",
            json!({"at_us":120,"reason":"stopped"}),
            0,
            0,
            2880,
            None,
        ));
        fixture.save();
        let report = fixture.replay("reset", ReplayOptions::default()).unwrap();
        assert_eq!(report.segments.len(), if wrong_sequence { 1 } else { 2 });
        assert_eq!(report.replay_complete_for_recorded_capture, !wrong_sequence);
        if !wrong_sequence {
            assert_eq!(report.segments[1].aec_ns_mismatched_samples_vs_recorded, 0);
            assert_eq!(report.segments[1].capture_blocks, 1);
            assert_eq!(report.segments[1].render_blocks, 2);
            assert!(
                fixture
                    .output("reset")
                    .join("segment-002-aec_only.wav")
                    .is_file()
            );
        }
    }
}

#[test]
fn oversized_input_metadata_and_inconsistent_processing_provenance_are_refused() {
    for mutation in 0..4 {
        let mut fixture = Fixture::new();
        match mutation {
            0 => {
                let file = fs::OpenOptions::new()
                    .write(true)
                    .open(fixture.run.join("capture-audio/pre_aec.pcm16le"))
                    .unwrap();
                file.set_len(160 * 1024 * 1024 + 1).unwrap();
            }
            1 => {
                fixture.capture[2]["meta"]["oversized"] = json!("x".repeat(4096));
                fixture.save();
            }
            2 => {
                let path = fixture.run.join("capture-audio/manifest.pending.json");
                let mut value: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
                value["max_seconds"] = json!(601);
                write(&path, &serde_json::to_vec(&value).unwrap());
            }
            _ => {
                let path = fixture.run.join("capture-audio/manifest.pending.json");
                let mut value: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
                value["software_processing"]["noise_suppression"] = json!(false);
                write(&path, &serde_json::to_vec(&value).unwrap());
            }
        }
        assert!(
            fixture
                .replay(
                    "refused",
                    ReplayOptions {
                        exploratory_prefix: true,
                        ..Default::default()
                    }
                )
                .is_err()
        );
    }
}

#[test]
fn missing_or_zero_worker_identity_cannot_certify_matching_markers() {
    for missing in [false, true] {
        let fixture = Fixture::new();
        let path = fixture.run.join("events.jsonl");
        let mut rows: Vec<Value> = fs::read_to_string(&path)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        for row in &mut rows {
            if missing {
                row.as_object_mut().unwrap().remove("worker_boot");
            } else {
                row["worker_boot"] = json!(vec![0u8; 16]);
            }
        }
        write(&path, &jsonl(&rows));
        assert!(fixture.replay("refused", ReplayOptions::default()).is_err());
    }
}
