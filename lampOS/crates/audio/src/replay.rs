//! Offline paired AEC/NS replay of bounded local diagnostic evidence.
//! No audio device, provider connection, inferred silence, or acoustic verdict.
use crate::{CAPTURE_RATE, CaptureBlock, EchoProcessor, RENDER_SAMPLES, Result};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::{self, Read, Write},
    os::{fd::OwnedFd, unix::fs::MetadataExt},
    path::{Path, PathBuf},
};

const STREAM_LIMIT: usize = 160 * 1024 * 1024;
const TRACE_LIMIT: usize = 32 * 1024 * 1024;
const MAX_RECORDS: usize = 120_000;
const MAX_SEGMENTS: usize = 128;
const MAX_CAPTURE_FRAMES: u64 = 600 * 16_000;
const MAX_RENDER_FRAMES: u64 = 600 * 24_000;
const FILES: [&str; 6] = [
    "manifest.pending.json",
    "result.json",
    "events.jsonl",
    "pre_aec.pcm16le",
    "post_aec.pcm16le",
    "render_accepted.pcm16le",
];

#[derive(Default)]
pub struct ReplayOptions {
    pub exploratory_prefix: bool,
    /// Explicit operator assertion backed by a pinned source evidence file.
    /// Never substitutes for a diagnostic completion receipt.
    pub historical_processing: Option<HistoricalProcessing>,
}
pub struct HistoricalProcessing {
    pub noise_suppression: bool,
    pub source_evidence: PathBuf,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct Processing {
    pub aec: String,
    pub noise_suppression: bool,
}
#[derive(Deserialize)]
struct Header {
    schema_version: u8,
    boot: [u8; 16],
    stream: String,
    started_at_us: u64,
    max_seconds: u64,
    max_bytes: usize,
    #[serde(default)]
    software_processing: Option<Processing>,
}
struct Bundle {
    header: Header,
    result: Value,
    files: BTreeMap<String, Vec<u8>>,
    hashes: BTreeMap<String, String>,
}
#[derive(Deserialize)]
struct Row {
    sequence: u64,
    kind: String,
    meta: Value,
    frames: u64,
    accepted_silence_kind: Option<String>,
    pre_aec_byte_offset: u64,
    post_aec_byte_offset: u64,
    render_accepted_byte_offset: u64,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
struct Reference {
    playback_epoch: u64,
    accepted_through: u64,
    analysed_through: u64,
    capture_blocks_processed: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    capture_blocks_before_clock_start: Option<u64>,
    accepted_at_us: u64,
    analysed_at_us: u64,
    queue_observed_at_us: u64,
    queued_frames: u64,
    // Older recordings have only start-call completion. Do not invent a request bound.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    clock_start_requested_at_us: Option<u64>,
    clock_started_at_us: Option<u64>,
    ignored_old_epoch_packets: u64,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
struct Capture {
    epoch: u64,
    dsp_epoch: u64,
    privacy_generation: u64,
    frame_sequence: u64,
    first_read_started_at_us: u64,
    last_read_started_at_us: u64,
    read_completed_at_us: u64,
    successful_reads: u16,
    alsa_status_monotonic_us: Option<u64>,
    status_observed_at_us: u64,
    available_frames: i64,
    delayed_frames: i64,
    processing_completed_at_us: u64,
    aec_queue_delay_ms: u16,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    aec_internal_alignment_ms: Option<i32>,
    reference: Reference,
    vad_score: f32,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
struct Discard {
    playback_epoch: u64,
    retired_through: u64,
    accepted_through: u64,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
struct Reset {
    at_us: u64,
    reason: String,
    capture_epoch: u64,
    dsp_epoch: u64,
    playback_epoch: u64,
    privacy_generation: u64,
    discarded: Option<Discard>,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
struct Render {
    playback_epoch: u64,
    privacy_generation: u64,
    first_sample: u64,
    end_sample: u64,
    accepted_at_us: u64,
    queue_observed_at_us: u64,
    queued_frames: i64,
    owner: Option<lamp_interaction::TurnOwner>,
    chunk_sequence: Option<u64>,
}
#[derive(Clone, Debug, Deserialize)]
struct Privacy {
    at_us: u64,
    generation: u64,
    open: bool,
}
struct Packet {
    meta: Render,
    sequence: u64,
    offset: usize,
    prime: bool,
}
#[derive(Default)]
struct Epoch {
    packets: Vec<Packet>,
    frames: u64,
    prime_frames: u64,
}
enum CaptureStep {
    Reset(Reset),
    Frame {
        meta: Capture,
        sequence: u64,
        offset: usize,
    },
    Boundary {
        kind: String,
        at_us: u64,
        source_meta: Value,
    },
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct SignalStats {
    pub frames: u64,
    pub rms_pcm: f64,
    pub peak_absolute_pcm: u32,
    pub saturated_samples: u64,
    pub near_rail_samples: u64,
    pub near_rail_flat_runs_at_least_3: u64,
}
fn signal(samples: &[i16]) -> SignalStats {
    let mut result = SignalStats::default();
    let mut energy = 0.0;
    let mut run = 0;
    let mut previous = 0;
    for &sample in samples {
        result.frames += 1;
        energy += f64::from(sample).powi(2);
        result.peak_absolute_pcm = result
            .peak_absolute_pcm
            .max(i32::from(sample).unsigned_abs());
        result.saturated_samples += u64::from(sample == i16::MIN || sample == i16::MAX);
        if i32::from(sample).unsigned_abs() >= 32_760 {
            result.near_rail_samples += 1;
            run = if sample == previous { run + 1 } else { 1 };
            result.near_rail_flat_runs_at_least_3 += u64::from(run == 3);
        } else {
            run = 0;
        }
        previous = sample;
    }
    if !samples.is_empty() {
        result.rms_pcm = (energy / samples.len() as f64).sqrt();
    }
    result
}
#[derive(Serialize)]
pub struct SegmentReport {
    pub capture_epoch: u64,
    pub dsp_epoch: u64,
    pub playback_epoch: u64,
    pub privacy_generation: u64,
    pub first_read_started_at_us: u64,
    pub last_processing_completed_at_us: u64,
    pub capture_blocks: u64,
    pub render_blocks: u64,
    pub last_analysed_through: u64,
    pub delay_ms_min: u16,
    pub delay_ms_max: u16,
    pub maximum_read_span_us: u64,
    pub maximum_processing_span_us: u64,
    pub pre_aec: SignalStats,
    pub recorded_post_aec: SignalStats,
    pub aec_only: SignalStats,
    pub aec_ns: SignalStats,
    pub aec_only_mismatched_samples_vs_recorded: u64,
    pub aec_ns_mismatched_samples_vs_recorded: u64,
    pub wav_pcm_sha256: BTreeMap<String, String>,
}
#[derive(Serialize)]
pub struct ReplayReport {
    pub schema_version: u8,
    pub status: &'static str,
    pub source_classification: &'static str,
    pub source_certified: bool,
    pub replay_complete_for_recorded_capture: bool,
    pub capture_boot: [u8; 16],
    pub source_hashes: BTreeMap<String, String>,
    pub processing_provenance: Value,
    pub source_capture_frames: u64,
    pub replayed_capture_frames: u64,
    pub source_render_frames: u64,
    pub segments: Vec<SegmentReport>,
    pub limitations: Vec<String>,
    pub acoustic_quality_verdict: Option<bool>,
    pub word_error_rate: Option<f64>,
}

/// Run from an explicit workspace root. Output must be a fresh leaf beneath its
/// existing artifacts/ or .cache/ directory; no build-machine path is embedded.
pub fn replay(
    workspace: &Path,
    input: &Path,
    output: &Path,
    options: ReplayOptions,
) -> Result<ReplayReport> {
    let capture = load_bundle(&input.join("capture-audio"), "capture")?;
    let render = load_bundle(&input.join("render-audio"), "render")?;
    if capture.header.boot != render.header.boot {
        return Err(invalid("capture/render session boot mismatch"));
    }
    let trace = read_bounded(&input.join("events.jsonl"), TRACE_LIMIT)?;
    let certification = certify(&capture, &render, input, &trace);
    if !options.exploratory_prefix {
        certification.as_ref().map_err(|_| invalid("source is not certified; only explicit --exploratory-prefix permits fault diagnostics"))?;
    }
    let provenance = processing_provenance(&capture, options.historical_processing.as_ref())?;
    let (steps, source_capture_frames) = capture_steps(&capture)?;
    let epochs = render_epochs(&render)?;
    let mut hashes = BTreeMap::new();
    for (name, bundle) in [("capture-audio", &capture), ("render-audio", &render)] {
        for (file, hash) in &bundle.hashes {
            hashes.insert(format!("{name}/{file}"), hash.clone());
        }
    }
    hashes.insert("events.jsonl".into(), digest(&trace));
    let mut report = ReplayReport {
        schema_version: 1, status: "offline_dsp_comparison_not_acoustic_acceptance",
        source_classification: if certification.is_ok() { "certified_diagnostic_recording" } else { "exploratory_fault_prefix_not_benchmark_evidence" },
        source_certified: certification.is_ok(), replay_complete_for_recorded_capture: true,
        capture_boot: capture.header.boot, source_hashes: hashes, processing_provenance: provenance,
        source_capture_frames, replayed_capture_frames: 0, source_render_frames: render.files["render_accepted.pcm16le"].len() as u64 / 2,
        segments: Vec::new(), limitations: vec![
            "pre_aec is ALSA-resampled 16 kHz mono after hardware/driver processing, not raw capsule audio".into(),
            "accepted render is not proof of physical playback; discarded ranges are retained and only analysed cursors are replayed".into(),
            "recorded host clocks, nominal delay and DSP call order do not establish USB clock synchronization or acoustic latency".into(),
            "RMS/clipping and replay differences are signal diagnostics, not intelligibility, naturalness, WER or a pass verdict".into(),
            "loudspeaker replay cannot qualify direct-human microphone speech quality; host DSP numerics may differ from target hardware".into(),
        ], acoustic_quality_verdict: None, word_error_rate: None,
    };
    if let Err(reason) = certification {
        report
            .limitations
            .push(format!("source certification failed: {reason}"));
    }
    let output = Output::create(workspace, output)?;
    output.json(
        "incomplete.json",
        &json!({"status":"incomplete_offline_replay","source_hashes":report.source_hashes}),
    )?;
    let mut operations = output.file("operations.jsonl")?;
    let mut state: Option<Segment> = None;
    let mut completed = Vec::new();
    for step in steps {
        let result = apply_step(
            step,
            &capture,
            &render,
            &epochs,
            &mut state,
            &mut completed,
            &mut operations,
        );
        if let Err(error) = result {
            report.replay_complete_for_recorded_capture = false;
            report.limitations.push(format!(
                "replay stopped at first unsupported/ambiguous boundary: {error}"
            ));
            break;
        }
    }
    if let Some(segment) = state {
        completed.push(segment);
    }
    for segment in completed {
        finish_segment(segment, &output, &mut report)?;
    }
    if report.replayed_capture_frames != source_capture_frames {
        report.replay_complete_for_recorded_capture = false;
    }
    if report.replayed_capture_frames == 0 {
        report.status = "insufficient_evidence_for_dsp_replay";
    }
    operations.sync_all()?;
    output.json("report.json", &report)?;
    rustix::fs::fsync(&output.directory)?;
    Ok(report)
}
fn processing_provenance(
    bundle: &Bundle,
    historical: Option<&HistoricalProcessing>,
) -> Result<Value> {
    if let Some(processing) = &bundle.header.software_processing {
        if historical.is_some() {
            return Err(invalid(
                "historical assertion cannot override recorded processing provenance",
            ));
        }
        return Ok(json!({"kind":"recorded", "software_processing":processing}));
    }
    if let Some(historical) = historical {
        let bytes = read_bounded(&historical.source_evidence, 65_536)?;
        let source: Value = serde_json::from_slice(&bytes)?;
        let id = source["source_id"]
            .as_str()
            .filter(|s| valid_hash(s))
            .ok_or_else(|| invalid("historical source evidence needs a SHA256 source_id"))?;
        return Ok(
            json!({"kind":"operator_asserted_historical_source_not_runtime_mode_receipt", "source_id":id, "evidence_sha256":digest(&bytes), "software_processing":{"aec":"sonora_aec3","noise_suppression":historical.noise_suppression}}),
        );
    }
    Ok(json!({"kind":"unspecified_in_recording", "software_processing":null}))
}
fn load_bundle(directory: &Path, stream: &str) -> Result<Bundle> {
    if !fs::symlink_metadata(directory)?.is_dir() {
        return Err(invalid("diagnostic directory must not be a symlink"));
    }
    let mut files = BTreeMap::new();
    let mut hashes = BTreeMap::new();
    let mut total = 0;
    for name in FILES {
        let limit = match name {
            "manifest.pending.json" | "result.json" => 16_384,
            _ => STREAM_LIMIT,
        };
        let bytes = read_bounded(&directory.join(name), limit)?;
        total += bytes.len();
        if total > STREAM_LIMIT {
            return Err(invalid("diagnostic stream exceeds 160 MiB"));
        }
        hashes.insert(name.to_owned(), digest(&bytes));
        files.insert(name.to_owned(), bytes);
    }
    let header: Header = serde_json::from_slice(&files["manifest.pending.json"])?;
    let result: Value = serde_json::from_slice(&files["result.json"])?;
    if header.schema_version != 1
        || header.stream != stream
        || header.boot == [0; 16]
        || !(1..=600).contains(&header.max_seconds)
        || header.max_bytes > STREAM_LIMIT
        || total > header.max_bytes
        || result["boot"] != json!(header.boot)
        || result["stream"] != stream
        || result["started_at_us"].as_u64() != Some(header.started_at_us)
    {
        return Err(invalid(
            "unsupported diagnostic schema, identity, or bounds",
        ));
    }
    let processing: Option<Processing> = serde_json::from_value(
        result
            .get("software_processing")
            .cloned()
            .unwrap_or(Value::Null),
    )?;
    if processing != header.software_processing
        || processing.as_ref().is_some_and(|p| p.aec != "sonora_aec3")
    {
        return Err(invalid(
            "processing provenance missing, mismatched or unsupported",
        ));
    }
    for (file, field) in [
        ("pre_aec.pcm16le", "pre_aec_sha256"),
        ("post_aec.pcm16le", "post_aec_sha256"),
        ("render_accepted.pcm16le", "render_accepted_sha256"),
        ("events.jsonl", "events_sha256"),
    ] {
        if result[field].as_str() != Some(&hashes[file]) {
            return Err(invalid(
                "diagnostic content hash mismatch; corrupted input is not an exploratory prefix",
            ));
        }
    }
    let capture_frames = files["pre_aec.pcm16le"].len() as u64 / 2;
    let render_frames = files["render_accepted.pcm16le"].len() as u64 / 2;
    if files["pre_aec.pcm16le"].len() % 320 != 0
        || files["post_aec.pcm16le"].len() != files["pre_aec.pcm16le"].len()
        || files["render_accepted.pcm16le"].len() % 2 != 0
        || capture_frames > MAX_CAPTURE_FRAMES
        || capture_frames > header.max_seconds * 16_000
        || render_frames > MAX_RENDER_FRAMES
        || render_frames > header.max_seconds * 24_000
        || result["capture_frames"].as_u64() != Some(capture_frames)
        || result["render_frames"].as_u64() != Some(render_frames)
        || (stream == "capture" && render_frames != 0)
        || (stream == "render" && capture_frames != 0)
    {
        return Err(invalid("PCM frame count or stream shape mismatch"));
    }
    Ok(Bundle {
        header,
        result,
        files,
        hashes,
    })
}
fn certify(capture: &Bundle, render: &Bundle, input: &Path, trace: &[u8]) -> Result<()> {
    let mut starts: BTreeMap<String, Value> = BTreeMap::new();
    let mut receipts: BTreeMap<String, Value> = BTreeMap::new();
    for line in json_lines(trace, 65_536)? {
        let row: Value = serde_json::from_slice(line)?;
        let target = match row["kind"].as_str() {
            Some("audio_diagnostics_started") => &mut starts,
            Some("audio_diagnostics_certified") => &mut receipts,
            _ => continue,
        };
        let worker = row["worker"]
            .as_str()
            .ok_or_else(|| invalid("diagnostic trace missing worker"))?
            .to_owned();
        if target.insert(worker, row).is_some() {
            return Err(invalid("duplicate diagnostic trace identity/receipt"));
        }
    }
    for (bundle, worker, leaf) in [
        (capture, "capture", "capture-audio"),
        (render, "speaker", "render-audio"),
    ] {
        let started = starts
            .get(worker)
            .ok_or_else(|| invalid("missing diagnostic start trace"))?;
        let certified = receipts
            .get(worker)
            .ok_or_else(|| invalid("missing diagnostic receipt"))?;
        let receipt = &certified["receipt"];
        let identity = json!({"boot":bundle.header.boot,"stream":bundle.header.stream,"started_at_us":bundle.header.started_at_us});
        let worker_boot: Option<[u8; 16]> =
            serde_json::from_value(started["worker_boot"].clone()).ok();
        if worker_boot.is_none_or(|boot| boot == [0; 16])
            || started["details"] != identity
            || receipt["started"] != identity
            || started["worker_boot"] != certified["worker_boot"]
            || certified["valid"] != true
            || receipt["acknowledged"] != true
            || receipt["valid"] != true
            || receipt["outcome"] != "complete"
            || receipt["faults"] != 0
            || receipt["completion_marker_published"] != true
            || bundle.result["valid"] != true
            || bundle.result["faults"] != 0
            || bundle.result["records_rejected"] != 0
            || bundle.result["records_queued"] != bundle.result["records_written"]
            || !matches!(
                bundle.result["end"]["reason"].as_str(),
                Some("completed" | "stopped" | "privacy_closed")
            )
        {
            return Err(invalid(
                "fault, timeout, invalid identity or unsuccessful completion receipt",
            ));
        }
        let marker = read_bounded(&input.join(leaf).join("complete.json"), 16_384)?;
        if marker != bundle.files["result.json"]
            || receipt["completion_sha256"].as_str() != Some(&digest(&marker))
            || receipt["finalized_at_us"] != bundle.result["finalized_at_us"]
        {
            return Err(invalid(
                "completion marker/hash does not match timely receipt",
            ));
        }
        for field in [
            "records_written",
            "records_rejected",
            "capture_frames",
            "render_frames",
            "accepted_zero_frames",
        ] {
            if receipt[field] != bundle.result[field] {
                return Err(invalid("receipt counters disagree with completion marker"));
            }
        }
    }
    Ok(())
}
fn json_lines(bytes: &[u8], line_limit: usize) -> Result<Vec<&[u8]>> {
    if !bytes.is_empty() && !bytes.ends_with(b"\n") {
        return Err(invalid(
            "incomplete JSONL suffix; no fabricated missing record",
        ));
    }
    let lines: Vec<_> = bytes
        .split(|b| *b == b'\n')
        .filter(|line| !line.is_empty())
        .collect();
    if lines.len() > MAX_RECORDS || lines.iter().any(|line| line.len() > line_limit) {
        return Err(invalid("JSONL line/record bound"));
    }
    Ok(lines)
}
fn rows(bundle: &Bundle) -> Result<Vec<Row>> {
    let mut rows = Vec::new();
    let mut previous = bundle.header.started_at_us;
    let mut pre = 0;
    let mut rendered = 0;
    let mut ended = false;
    for line in json_lines(&bundle.files["events.jsonl"], 4096)? {
        let row: Row = serde_json::from_slice(line)?;
        let at = match row.kind.as_str() {
            "capture" => row.meta["processing_completed_at_us"].as_u64(),
            "render_accepted" => row.meta["queue_observed_at_us"].as_u64(),
            "reset" | "privacy" | "end" => row.meta["at_us"].as_u64(),
            _ => None,
        }
        .ok_or_else(|| invalid("unknown record kind or missing clock"))?;
        if ended
            || row.sequence != rows.len() as u64 + 1
            || at < previous
            || at - bundle.header.started_at_us > bundle.header.max_seconds * 1_000_000
            || row.pre_aec_byte_offset != pre
            || row.post_aec_byte_offset != pre
            || row.render_accepted_byte_offset != rendered
        {
            return Err(invalid(
                "record sequence, clock or PCM offset discontinuity",
            ));
        }
        previous = at;
        match row.kind.as_str() {
            "capture" if bundle.header.stream == "capture" && row.frames == 160 => pre += 320,
            "render_accepted"
                if bundle.header.stream == "render" && (1..=240).contains(&row.frames) =>
            {
                rendered += row.frames * 2
            }
            "reset" | "privacy" | "end" if row.frames == 0 => {}
            _ => return Err(invalid("record role/frame mismatch")),
        }
        ended = row.kind == "end";
        rows.push(row);
    }
    if (bundle.result["valid"] == true && !ended)
        || pre != bundle.files["pre_aec.pcm16le"].len() as u64
        || rendered != bundle.files["render_accepted.pcm16le"].len() as u64
        || bundle.result["records_written"].as_u64() != Some(rows.len() as u64)
    {
        return Err(invalid("recorded offsets do not cover exact hashed PCM"));
    }
    if let Some(last) = rows.last() {
        if last.kind == "end" && bundle.result["end"] != last.meta {
            return Err(invalid("end summary contradicts recorded boundary"));
        }
        if bundle.result["finalized_at_us"]
            .as_u64()
            .is_none_or(|at| at < previous)
        {
            return Err(invalid("finalization clock precedes recorded events"));
        }
    }
    Ok(rows)
}
fn observe_privacy(current: &mut Option<Privacy>, next: Privacy) -> Result<()> {
    if next.generation == 0
        || current.as_ref().is_some_and(|old| {
            next.generation < old.generation
                || (next.open != old.open && next.generation == old.generation)
        })
    {
        return Err(invalid("privacy generation discontinuity"));
    }
    *current = Some(next);
    Ok(())
}
fn capture_steps(bundle: &Bundle) -> Result<(Vec<CaptureStep>, u64)> {
    let mut result = Vec::new();
    let mut privacy: Option<Privacy> = None;
    for row in rows(bundle)? {
        match row.kind.as_str() {
            "privacy" => {
                let next: Privacy = serde_json::from_value(row.meta.clone())?;
                result.push(CaptureStep::Boundary {
                    kind: format!("privacy_{}", if next.open { "open" } else { "closed" }),
                    at_us: next.at_us,
                    source_meta: row.meta,
                });
                observe_privacy(&mut privacy, next)?;
            }
            "capture" => {
                let meta: Capture = serde_json::from_value(row.meta)?;
                if privacy
                    .as_ref()
                    .is_none_or(|p| !p.open || p.generation != meta.privacy_generation)
                    || meta.epoch == 0
                    || meta.dsp_epoch == 0
                    || meta.first_read_started_at_us > meta.last_read_started_at_us
                    || meta.last_read_started_at_us > meta.read_completed_at_us
                    || meta.read_completed_at_us > meta.processing_completed_at_us
                    || meta.status_observed_at_us > meta.processing_completed_at_us
                    || meta.successful_reads == 0
                    || !meta.vad_score.is_finite()
                    || !(0.0..=1.0).contains(&meta.vad_score)
                    || meta.aec_queue_delay_ms > 500
                {
                    return Err(invalid(
                        "invalid capture privacy, clock, VAD or delay evidence",
                    ));
                }
                result.push(CaptureStep::Frame {
                    meta,
                    sequence: row.sequence,
                    offset: row.pre_aec_byte_offset as usize,
                });
            }
            "reset" => result.push(CaptureStep::Reset(serde_json::from_value(row.meta)?)),
            "end" => result.push(CaptureStep::Boundary {
                kind: format!("end_{}", row.meta["reason"].as_str().unwrap_or("unknown")),
                at_us: row.meta["at_us"].as_u64().unwrap_or(0),
                source_meta: row.meta,
            }),
            _ => return Err(invalid("unexpected capture stream record")),
        }
    }
    Ok((result, bundle.files["pre_aec.pcm16le"].len() as u64 / 2))
}
fn render_epochs(bundle: &Bundle) -> Result<BTreeMap<u64, Epoch>> {
    let mut epochs: BTreeMap<u64, Epoch> = BTreeMap::new();
    let mut privacy: Option<Privacy> = None;
    let mut current = 0;
    for row in rows(bundle)? {
        match row.kind.as_str() {
            "privacy" => observe_privacy(&mut privacy, serde_json::from_value(row.meta)?)?,
            "reset" => {
                let reset: Reset = serde_json::from_value(row.meta)?;
                if matches!(reset.reason.as_str(), "start" | "playback_reset") {
                    if reset.playback_epoch == 0
                        || reset.playback_epoch <= current
                        || (current == 0 && reset.reason != "start")
                    {
                        return Err(invalid(
                            "missing initial render start or non-advancing reset",
                        ));
                    }
                    if current != 0 {
                        let discarded = reset
                            .discarded
                            .as_ref()
                            .ok_or_else(|| invalid("playback reset missing discarded range"))?;
                        if discarded.playback_epoch != current
                            || discarded.retired_through > discarded.accepted_through
                            || discarded.accepted_through
                                != epochs.get(&current).map_or(0, |e| e.frames)
                        {
                            return Err(invalid("discarded range does not match accepted render"));
                        }
                    }
                    current = reset.playback_epoch;
                    epochs.entry(current).or_default();
                }
            }
            "render_accepted" => {
                let meta: Render = serde_json::from_value(row.meta)?;
                if current == 0
                    || meta.playback_epoch != current
                    || privacy
                        .as_ref()
                        .is_none_or(|p| !p.open || p.generation != meta.privacy_generation)
                    || meta.accepted_at_us > meta.queue_observed_at_us
                    || meta
                        .owner
                        .is_some_and(|o| o.boot().bytes() != bundle.header.boot)
                {
                    return Err(invalid("invalid render epoch, privacy, owner or clock"));
                }
                let epoch = epochs
                    .get_mut(&current)
                    .ok_or_else(|| invalid("unannounced render epoch"))?;
                if meta.first_sample != epoch.frames
                    || meta.first_sample.checked_add(row.frames) != Some(meta.end_sample)
                {
                    return Err(invalid("accepted render cursor gap"));
                }
                let offset = row.render_accepted_byte_offset as usize;
                let bytes = &bundle.files["render_accepted.pcm16le"]
                    [offset..offset + row.frames as usize * 2];
                let prime = row.accepted_silence_kind.as_deref() == Some("prime");
                if let Some(kind) = row.accepted_silence_kind.as_deref()
                    && (!matches!(kind, "prime" | "idle" | "speech_gap")
                        || bytes.iter().any(|b| *b != 0)
                        || meta.owner.is_some()
                        || meta.chunk_sequence.is_some())
                {
                    return Err(invalid("typed accepted zeros contradict PCM/lineage"));
                }
                if prime {
                    if meta.first_sample != epoch.prime_frames || meta.end_sample > 480 {
                        return Err(invalid(
                            "prime is not exactly an initial accepted zero prefix",
                        ));
                    }
                    epoch.prime_frames = meta.end_sample;
                }
                epoch.frames = meta.end_sample;
                epoch.packets.push(Packet {
                    meta,
                    sequence: row.sequence,
                    offset,
                    prime,
                });
            }
            "end" => {}
            _ => return Err(invalid("unexpected render stream record")),
        }
    }
    Ok(epochs)
}

struct Segment {
    reset: Reset,
    next_sequence: u64,
    frames: u64,
    analysed: u64,
    packet_index: usize,
    closed: bool,
    first_at: u64,
    last_at: u64,
    delay_min: u16,
    delay_max: u16,
    read_span_max: u64,
    process_span_max: u64,
    pre: Vec<i16>,
    post: Vec<i16>,
    aec: Vec<i16>,
    ns: Vec<i16>,
    aec_processor: EchoProcessor,
    ns_processor: EchoProcessor,
}
impl Segment {
    fn new(reset: Reset, next_sequence: u64) -> Self {
        Self {
            reset,
            next_sequence,
            frames: 0,
            analysed: 0,
            packet_index: 0,
            closed: false,
            first_at: 0,
            last_at: 0,
            delay_min: 500,
            delay_max: 0,
            read_span_max: 0,
            process_span_max: 0,
            pre: Vec::new(),
            post: Vec::new(),
            aec: Vec::new(),
            ns: Vec::new(),
            aec_processor: EchoProcessor::new(false),
            ns_processor: EchoProcessor::new(true),
        }
    }
}
fn apply_step(
    step: CaptureStep,
    capture: &Bundle,
    render: &Bundle,
    epochs: &BTreeMap<u64, Epoch>,
    state: &mut Option<Segment>,
    completed: &mut Vec<Segment>,
    operations: &mut File,
) -> Result<()> {
    match step {
        CaptureStep::Reset(reset) => {
            if !matches!(reset.reason.as_str(), "start" | "dsp_reset") {
                return Err(invalid(
                    "capture boundary lacks a supported recorded cold-prime transition",
                ));
            }
            if completed.len() + usize::from(state.is_some()) >= MAX_SEGMENTS
                || reset.capture_epoch == 0
                || reset.dsp_epoch == 0
                || reset.playback_epoch == 0
                || reset.privacy_generation == 0
            {
                return Err(invalid("reset identity or segment bound"));
            }
            let next_sequence = if let Some(old) = state.as_ref() {
                let discarded = reset
                    .discarded
                    .as_ref()
                    .ok_or_else(|| invalid("DSP reset has no prior discarded render range"))?;
                if reset.reason != "dsp_reset"
                    || reset.capture_epoch != old.reset.capture_epoch
                    || reset.privacy_generation != old.reset.privacy_generation
                    || reset.dsp_epoch <= old.reset.dsp_epoch
                    || reset.playback_epoch <= old.reset.playback_epoch
                    || discarded.playback_epoch != old.reset.playback_epoch
                    || discarded.retired_through > discarded.accepted_through
                    || epochs
                        .get(&discarded.playback_epoch)
                        .is_none_or(|epoch| epoch.frames != discarded.accepted_through)
                {
                    return Err(invalid(
                        "DSP reset would erase capture continuity or mislabel discarded render",
                    ));
                }
                old.next_sequence
            } else {
                if reset.reason != "start" || !completed.is_empty() || reset.discarded.is_some() {
                    return Err(invalid("replay has no known cold initial state"));
                }
                1
            };
            let epoch = epochs
                .get(&reset.playback_epoch)
                .ok_or_else(|| invalid("reset refers to missing render epoch"))?;
            if epoch.prime_frames != 480
                || epoch.packets.iter().filter(|p| p.prime).any(|p| {
                    p.meta.queue_observed_at_us > reset.at_us
                        || p.meta.privacy_generation != reset.privacy_generation
                })
            {
                return Err(invalid("missing or late actually accepted 480-frame prime"));
            }
            if let Some(old) = state.take() {
                completed.push(old);
            }
            line(
                operations,
                &json!({"kind":"dsp_reset","source_reset":reset,"next_capture_sequence":next_sequence}),
            )?;
            *state = Some(Segment::new(reset, next_sequence));
        }
        CaptureStep::Boundary {
            kind,
            at_us,
            source_meta,
        } => {
            if (kind == "privacy_closed" || kind.starts_with("end_"))
                && let Some(segment) = state
            {
                segment.closed = true;
            }
            line(
                operations,
                &json!({"kind":"source_boundary","boundary":kind,"at_us":at_us,"source_meta":source_meta}),
            )?;
        }
        CaptureStep::Frame {
            meta,
            sequence,
            offset,
        } => {
            let segment = state
                .as_mut()
                .ok_or_else(|| invalid("capture arrived without a recorded Start/prime"))?;
            let reference = &meta.reference;
            if segment.closed
                || meta.epoch != segment.reset.capture_epoch
                || meta.dsp_epoch != segment.reset.dsp_epoch
                || meta.privacy_generation != segment.reset.privacy_generation
                || meta.frame_sequence != segment.next_sequence
                || reference.playback_epoch != segment.reset.playback_epoch
                || reference.capture_blocks_processed != segment.frames + 1
                || reference.analysed_through < segment.analysed
                || reference.analysed_through < 480
                || reference.analysed_through % 240 != 0
                || reference.accepted_through < reference.analysed_through
                || reference.analysed_at_us > meta.processing_completed_at_us
                || reference.queue_observed_at_us > meta.processing_completed_at_us
                || reference.accepted_at_us > reference.queue_observed_at_us
                || meta.processing_completed_at_us < segment.reset.at_us
            {
                return Err(invalid(
                    "capture/analysis cursor, phase count or clock continuity is insufficient",
                ));
            }
            let epoch = epochs
                .get(&reference.playback_epoch)
                .ok_or_else(|| invalid("missing accepted render epoch"))?;
            if reference.accepted_through > epoch.frames {
                return Err(invalid(
                    "capture claims render samples absent from recording",
                ));
            }
            while segment.analysed < reference.analysed_through {
                let (samples, first_packet, last_packet) = render_block(
                    epoch,
                    &render.files["render_accepted.pcm16le"],
                    segment.analysed,
                    &mut segment.packet_index,
                    reference.analysed_at_us,
                    meta.privacy_generation,
                )?;
                segment.aec_processor.render(&samples)?;
                segment.ns_processor.render(&samples)?;
                line(
                    operations,
                    &json!({"kind":"render_before_capture","dsp_epoch":meta.dsp_epoch,"playback_epoch":reference.playback_epoch,"first_sample":segment.analysed,"end_sample":segment.analysed+240,"first_source_render_record":first_packet,"last_source_render_record":last_packet,"before_capture_record":sequence,"latest_recorded_analysis_at_us":reference.analysed_at_us}),
                )?;
                segment.analysed += 240;
            }
            let pre = block(&capture.files["pre_aec.pcm16le"], offset)?;
            let post = block(&capture.files["post_aec.pcm16le"], offset)?;
            let only = segment
                .aec_processor
                .capture(&pre, meta.aec_queue_delay_ms)?;
            let ns = segment
                .ns_processor
                .capture(&pre, meta.aec_queue_delay_ms)?;
            if segment.frames == 0 {
                segment.first_at = meta.first_read_started_at_us;
            }
            segment.last_at = meta.processing_completed_at_us;
            segment.delay_min = segment.delay_min.min(meta.aec_queue_delay_ms);
            segment.delay_max = segment.delay_max.max(meta.aec_queue_delay_ms);
            segment.read_span_max = segment
                .read_span_max
                .max(meta.read_completed_at_us - meta.first_read_started_at_us);
            segment.process_span_max = segment
                .process_span_max
                .max(meta.processing_completed_at_us - meta.read_completed_at_us);
            segment.frames += 1;
            segment.next_sequence += 1;
            segment.pre.extend_from_slice(&pre);
            segment.post.extend_from_slice(&post);
            segment.aec.extend_from_slice(&only);
            segment.ns.extend_from_slice(&ns);
            line(
                operations,
                &json!({"kind":"capture_pair","source_record":sequence,"source_pcm_byte_offset":offset,"segment_capture_sample_offset":(segment.frames-1)*160,"meta":meta,
                    "replayed_internal_alignment_ms": {
                        "aec_only": segment.aec_processor.internal_alignment_ms(),
                        "aec_ns": segment.ns_processor.internal_alignment_ms(),
                    }
                }),
            )?;
        }
    }
    Ok(())
}
fn render_block(
    epoch: &Epoch,
    pcm: &[u8],
    first: u64,
    index: &mut usize,
    analysed_at: u64,
    privacy: u64,
) -> Result<(crate::RenderBlock, u64, u64)> {
    let mut samples = [0i16; RENDER_SAMPLES];
    let mut count = 0;
    let mut first_record = 0;
    let mut last_record = 0;
    while count < RENDER_SAMPLES {
        while epoch
            .packets
            .get(*index)
            .is_some_and(|p| p.meta.end_sample <= first + count as u64)
        {
            *index += 1;
        }
        let packet = epoch
            .packets
            .get(*index)
            .ok_or_else(|| invalid("missing accepted PCM while assembling actual render block"))?;
        let cursor = first + count as u64;
        if packet.meta.first_sample > cursor
            || packet.meta.end_sample <= cursor
            || packet.meta.queue_observed_at_us > analysed_at
            || packet.meta.privacy_generation != privacy
        {
            return Err(invalid(
                "accepted render cannot reproduce recorded analysis order",
            ));
        }
        if first_record == 0 {
            first_record = packet.sequence;
        }
        last_record = packet.sequence;
        let take = (packet.meta.end_sample - cursor).min((RENDER_SAMPLES - count) as u64) as usize;
        let begin = packet.offset + (cursor - packet.meta.first_sample) as usize * 2;
        for (target, bytes) in samples[count..count + take]
            .iter_mut()
            .zip(pcm[begin..begin + take * 2].chunks_exact(2))
        {
            *target = i16::from_le_bytes([bytes[0], bytes[1]]);
        }
        count += take;
    }
    Ok((samples, first_record, last_record))
}
fn block(bytes: &[u8], offset: usize) -> Result<CaptureBlock> {
    let bytes = bytes
        .get(offset..offset + 320)
        .ok_or_else(|| invalid("truncated capture PCM block"))?;
    Ok(std::array::from_fn(|index| {
        i16::from_le_bytes([bytes[index * 2], bytes[index * 2 + 1]])
    }))
}
fn finish_segment(segment: Segment, output: &Output, report: &mut ReplayReport) -> Result<()> {
    if segment.frames == 0 {
        return Ok(());
    }
    let mut files = BTreeMap::new();
    for (name, samples) in [
        ("pre_aec", &segment.pre),
        ("recorded_post_aec", &segment.post),
        ("aec_only", &segment.aec),
        ("aec_ns", &segment.ns),
    ] {
        let file = format!("segment-{:03}-{name}.wav", report.segments.len() + 1);
        output.wav(&file, samples)?;
        files.insert(file, digest_pcm(samples));
    }
    report.replayed_capture_frames += segment.pre.len() as u64;
    report.segments.push(SegmentReport {
        capture_epoch: segment.reset.capture_epoch,
        dsp_epoch: segment.reset.dsp_epoch,
        playback_epoch: segment.reset.playback_epoch,
        privacy_generation: segment.reset.privacy_generation,
        first_read_started_at_us: segment.first_at,
        last_processing_completed_at_us: segment.last_at,
        capture_blocks: segment.frames,
        render_blocks: segment.analysed / 240,
        last_analysed_through: segment.analysed,
        delay_ms_min: segment.delay_min,
        delay_ms_max: segment.delay_max,
        maximum_read_span_us: segment.read_span_max,
        maximum_processing_span_us: segment.process_span_max,
        pre_aec: signal(&segment.pre),
        recorded_post_aec: signal(&segment.post),
        aec_only: signal(&segment.aec),
        aec_ns: signal(&segment.ns),
        aec_only_mismatched_samples_vs_recorded: segment
            .aec
            .iter()
            .zip(&segment.post)
            .filter(|(a, b)| a != b)
            .count() as u64,
        aec_ns_mismatched_samples_vs_recorded: segment
            .ns
            .iter()
            .zip(&segment.post)
            .filter(|(a, b)| a != b)
            .count() as u64,
        wav_pcm_sha256: files,
    });
    Ok(())
}
struct Output {
    directory: OwnedFd,
}
impl Output {
    fn create(workspace: &Path, requested: &Path) -> Result<Self> {
        let workspace = workspace.canonicalize()?;
        let requested = if requested.is_absolute() {
            requested.to_owned()
        } else {
            workspace.join(requested)
        };
        let parent = requested
            .parent()
            .ok_or_else(|| invalid("output needs a parent"))?
            .canonicalize()?;
        let mut allowed = false;
        for name in ["artifacts", ".cache"] {
            let root = workspace.join(name);
            if fs::symlink_metadata(&root).is_ok_and(|m| m.is_dir())
                && parent.starts_with(root.canonicalize()?)
            {
                allowed = true;
            }
        }
        if !allowed {
            return Err(invalid(
                "output must be a fresh leaf beneath workspace artifacts/ or .cache/",
            ));
        }
        let leaf = requested
            .file_name()
            .filter(|leaf| *leaf != "." && *leaf != "..")
            .ok_or_else(|| invalid("output needs a fresh leaf"))?;
        let parent = rustix::fs::open(
            &parent,
            rustix::fs::OFlags::RDONLY
                | rustix::fs::OFlags::DIRECTORY
                | rustix::fs::OFlags::NOFOLLOW
                | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::empty(),
        )?;
        rustix::fs::mkdirat(&parent, leaf, rustix::fs::Mode::RWXU)?;
        let directory = rustix::fs::openat(
            &parent,
            leaf,
            rustix::fs::OFlags::RDONLY
                | rustix::fs::OFlags::DIRECTORY
                | rustix::fs::OFlags::NOFOLLOW
                | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::empty(),
        )?;
        Ok(Self { directory })
    }
    fn file(&self, name: &str) -> Result<File> {
        Ok(rustix::fs::openat(
            &self.directory,
            name,
            rustix::fs::OFlags::WRONLY
                | rustix::fs::OFlags::CREATE
                | rustix::fs::OFlags::EXCL
                | rustix::fs::OFlags::NOFOLLOW
                | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::RUSR | rustix::fs::Mode::WUSR,
        )?
        .into())
    }
    fn json(&self, name: &str, value: &impl Serialize) -> Result<()> {
        let mut file = self.file(name)?;
        serde_json::to_writer_pretty(&mut file, value)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        Ok(())
    }
    fn wav(&self, name: &str, samples: &[i16]) -> Result<()> {
        let file = self.file(name)?;
        let sync = file.try_clone()?;
        let mut wav = hound::WavWriter::new(
            file,
            hound::WavSpec {
                channels: 1,
                sample_rate: CAPTURE_RATE,
                bits_per_sample: 16,
                sample_format: hound::SampleFormat::Int,
            },
        )?;
        for sample in samples {
            wav.write_sample(*sample)?;
        }
        wav.finalize()?;
        sync.sync_all()?;
        Ok(())
    }
}
fn line(file: &mut File, value: &Value) -> Result<()> {
    serde_json::to_writer(&mut *file, value)?;
    file.write_all(b"\n")?;
    Ok(())
}
fn read_bounded(path: &Path, limit: usize) -> Result<Vec<u8>> {
    let before = fs::symlink_metadata(path)?;
    if !before.is_file() || before.len() > limit as u64 {
        return Err(invalid("input is not a bounded regular non-symlink file"));
    }
    let file: File = rustix::fs::open(
        path,
        rustix::fs::OFlags::RDONLY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::NONBLOCK
            | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )?
    .into();
    let opened = file.metadata()?;
    if (before.dev(), before.ino(), before.len()) != (opened.dev(), opened.ino(), opened.len()) {
        return Err(invalid("input changed while opening"));
    }
    let mut bytes = Vec::new();
    bytes.try_reserve_exact(before.len() as usize)?;
    file.take(limit as u64 + 1).read_to_end(&mut bytes)?;
    let after = fs::symlink_metadata(path)?;
    if !after.is_file()
        || bytes.len() != before.len() as usize
        || bytes.len() > limit
        || (before.dev(), before.ino(), before.len()) != (after.dev(), after.ino(), after.len())
    {
        return Err(invalid("input changed while reading"));
    }
    Ok(bytes)
}
fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn digest_pcm(samples: &[i16]) -> String {
    let mut hash = Sha256::new();
    for sample in samples {
        hash.update(sample.to_le_bytes());
    }
    format!("{:x}", hash.finalize())
}
fn valid_hash(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| b.is_ascii_hexdigit())
}
fn invalid(message: &'static str) -> Box<dyn std::error::Error + Send + Sync> {
    io::Error::new(io::ErrorKind::InvalidData, message).into()
}
