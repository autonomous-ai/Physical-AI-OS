//! Score an existing lamp-live trace (for example a retained physical trial)
//! against a declared scenario. Traces carry no stimulus times, so turns are
//! attributed to steps by explicit declaration and labeled as such.
use crate::{
    Result,
    events::{ClockDomain, EventKind, parse_trace},
    invalid,
    ledger::{Ledger, unix_ms},
    plan::{LoadedPlan, ProviderKind},
    record::{AttemptRecord, AttemptStatus, Attribution, StepRecord, StepStatus, Stratum},
    stimulus::StimulusCatalog,
};
use lamp_acoustic::hash;
use serde::Serialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, Metadata},
    io::{BufReader, Read},
    path::Path,
    time::{Duration, Instant},
};

/// Same duration/rate/channel limits as the observer. The byte cap includes
/// room for RIFF headers; audio is streamed through a fixed 64 KiB buffer.
pub const MAX_ROOM_WAV_BYTES: u64 = 1024 * 1024 * 1024 + 4096;
pub const MAX_ROOM_SECONDS: u32 = 600;
const MAX_METADATA_BYTES: u64 = 1024 * 1024;
const MAX_CHUNKS: usize = 4096;
const VERIFY_LIMIT: Duration = Duration::from_secs(30);

#[derive(Clone, Debug, Serialize)]
pub struct VerifiedRoom {
    pub sha256: String,
    pub file_bytes: u64,
    pub frames: u64,
    pub sample_rate: u32,
    pub channels: u16,
    pub bits_per_sample: u16,
    pub float_samples: bool,
    pub duration_s: f64,
}

fn open_regular(path: &Path, minimum: u64, maximum: u64) -> Result<(File, Metadata)> {
    // A racing FIFO cannot block open, and a final symlink is never followed.
    // Parent paths are operator-selected local evidence locations, not a sandbox.
    let descriptor = rustix::fs::open(
        path,
        rustix::fs::OFlags::RDONLY
            | rustix::fs::OFlags::CLOEXEC
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::NONBLOCK,
        rustix::fs::Mode::empty(),
    )?;
    let file = File::from(descriptor);
    let metadata = file.metadata()?;
    if !metadata.is_file() || !(minimum..=maximum).contains(&metadata.len()) {
        return Err(invalid("recording evidence must be a bounded regular file"));
    }
    Ok((file, metadata))
}

fn read_hashed(
    reader: &mut impl Read,
    digest: &mut Sha256,
    bytes: &mut [u8],
    started: Instant,
) -> Result<()> {
    if started.elapsed() >= VERIFY_LIMIT {
        return Err(invalid("room WAV verification exceeded 30 seconds"));
    }
    reader.read_exact(bytes)?;
    digest.update(bytes);
    Ok(())
}

fn le16(bytes: &[u8]) -> u16 {
    u16::from_le_bytes([bytes[0], bytes[1]])
}
fn le32(bytes: &[u8]) -> u32 {
    u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]])
}

#[derive(Clone, Copy)]
struct PcmFormat {
    rate: u32,
    channels: u16,
    bits: u16,
    align: u16,
    float: bool,
}

fn pcm_format(bytes: &[u8]) -> Result<PcmFormat> {
    if bytes.len() < 16 {
        return Err(invalid("truncated WAV format"));
    }
    let mut encoding = le16(bytes);
    let channels = le16(&bytes[2..]);
    let rate = le32(&bytes[4..]);
    let byte_rate = le32(&bytes[8..]);
    let align = le16(&bytes[12..]);
    let bits = le16(&bytes[14..]);
    if encoding == 0xfffe {
        // WAVE_FORMAT_EXTENSIBLE, as emitted by hound for multichannel PCM.
        if bytes.len() < 40
            || le16(&bytes[16..]) < 22
            || usize::from(le16(&bytes[16..])) + 18 > bytes.len()
            || le16(&bytes[18..]) != bits
            || bytes[26..40] != [0, 0, 0, 0, 0x10, 0, 0x80, 0, 0, 0xaa, 0, 0x38, 0x9b, 0x71]
        {
            return Err(invalid("unsupported extensible WAV format"));
        }
        encoding = le16(&bytes[24..]);
    }
    if !((encoding == 1 && [16, 24, 32].contains(&bits)) || (encoding == 3 && bits == 32))
        || !(1..=8).contains(&channels)
        || !(8_000..=192_000).contains(&rate)
        || align != channels * (bits / 8)
        || byte_rate != rate * u32::from(align)
    {
        return Err(invalid(
            "require PCM16/24/32 or float32, 1..8 channels, 8000..192000 Hz and consistent frame alignment",
        ));
    }
    Ok(PcmFormat {
        rate,
        channels,
        bits,
        align,
        float: encoding == 3,
    })
}

/// Verify the actual bytes, not the observer's declared hash. No audio device
/// is opened. This offline operation has bounded bytes/frames/chunks and no
/// whole-recording allocation; native filesystem calls cannot be preempted.
pub fn verify_room_wav(path: &Path, claimed_sha256: &str) -> Result<VerifiedRoom> {
    if claimed_sha256.len() != 64 || !claimed_sha256.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(invalid("room recording has no valid SHA-256 claim"));
    }
    let (file, before) = open_regular(path, 44, MAX_ROOM_WAV_BYTES)?;
    let mut reader = BufReader::with_capacity(64 * 1024, file);
    let started = Instant::now();
    let mut digest = Sha256::new();
    let mut header = [0_u8; 12];
    read_hashed(&mut reader, &mut digest, &mut header, started)?;
    if &header[..4] != b"RIFF"
        || &header[8..] != b"WAVE"
        || u64::from(le32(&header[4..])) + 8 != before.len()
    {
        return Err(invalid(
            "require complete RIFF/WAVE with exact declared length",
        ));
    }
    let mut at = 12_u64;
    let mut chunks = 0;
    let mut format = None;
    let mut frames = None;
    let mut buffer = [0_u8; 64 * 1024];
    while at < before.len() {
        chunks += 1;
        if chunks > MAX_CHUNKS || before.len() - at < 8 {
            return Err(invalid("too many or truncated WAV chunks"));
        }
        let mut chunk = [0_u8; 8];
        read_hashed(&mut reader, &mut digest, &mut chunk, started)?;
        let size = u64::from(le32(&chunk[4..]));
        at += 8;
        if size + size % 2 > before.len() - at {
            return Err(invalid("truncated WAV chunk or padding"));
        }
        let mut floating = false;
        if &chunk[..4] == b"fmt " {
            if format.is_some() || !(16..=64).contains(&size) {
                return Err(invalid("duplicate or unsupported WAV format chunk"));
            }
            read_hashed(
                &mut reader,
                &mut digest,
                &mut buffer[..size as usize],
                started,
            )?;
            format = Some(pcm_format(&buffer[..size as usize])?);
        } else {
            if &chunk[..4] == b"data" {
                let spec = format.ok_or_else(|| invalid("WAV data precedes its format"))?;
                if frames.is_some() || size == 0 || !size.is_multiple_of(u64::from(spec.align)) {
                    return Err(invalid("empty, duplicate, or partial WAV sample frames"));
                }
                let count = size / u64::from(spec.align);
                if count > u64::from(spec.rate) * u64::from(MAX_ROOM_SECONDS) {
                    return Err(invalid("room WAV exceeds 600 seconds"));
                }
                frames = Some(count);
                floating = spec.float;
            }
            let mut remaining = size;
            while remaining > 0 {
                let count = remaining.min(buffer.len() as u64) as usize;
                read_hashed(&mut reader, &mut digest, &mut buffer[..count], started)?;
                if floating
                    && buffer[..count]
                        .chunks_exact(4)
                        .any(|v| !f32::from_bits(le32(v)).is_finite())
                {
                    return Err(invalid("room WAV contains nonfinite samples"));
                }
                remaining -= count as u64;
            }
        }
        if size % 2 != 0 {
            read_hashed(&mut reader, &mut digest, &mut buffer[..1], started)?;
        }
        at += size + size % 2;
    }
    let after = reader.get_ref().metadata()?;
    if after.len() != before.len()
        || after.modified().ok() != before.modified().ok()
        || reader.read(&mut buffer[..1])? != 0
        || started.elapsed() >= VERIFY_LIMIT
    {
        return Err(invalid(
            "room WAV changed during verification or verification timed out",
        ));
    }
    let sha256 = format!("{:x}", digest.finalize());
    if !sha256.eq_ignore_ascii_case(claimed_sha256) {
        return Err(invalid(
            "room WAV SHA-256 differs from retained recording hash",
        ));
    }
    let spec = format.ok_or_else(|| invalid("room WAV has no format"))?;
    let frames = frames.ok_or_else(|| invalid("room WAV has no samples"))?;
    Ok(VerifiedRoom {
        sha256,
        file_bytes: before.len(),
        frames,
        sample_rate: spec.rate,
        channels: spec.channels,
        bits_per_sample: spec.bits,
        float_samples: spec.float,
        duration_s: frames as f64 / f64::from(spec.rate),
    })
}

fn check_observer_claims(metadata: &Value, verified: &VerifiedRoom) -> Result<()> {
    for (key, expected) in [
        ("/requested_frames", verified.frames),
        ("/capture/written_frames", verified.frames),
        ("/writer/written_frames", verified.frames),
        ("/writer/source_sequence_gaps", 0),
        (
            "/delivered_format/sample_rate",
            u64::from(verified.sample_rate),
        ),
        ("/delivered_format/channels", u64::from(verified.channels)),
    ] {
        if metadata
            .pointer(key)
            .is_some_and(|value| value.as_u64() != Some(expected))
        {
            return Err(invalid(&format!(
                "observer {key} differs from verified recording"
            )));
        }
    }
    if metadata.get("written_duration_s").is_some_and(|value| {
        value.as_f64().is_none_or(|seconds| {
            !seconds.is_finite()
                || (seconds - verified.duration_s).abs() > 0.5 / f64::from(verified.sample_rate)
        })
    }) {
        return Err(invalid("observer duration differs from verified recording"));
    }
    if metadata
        .get("errors")
        .is_some_and(|errors| errors.as_array().is_none_or(|a| !a.is_empty()))
    {
        return Err(invalid("observer final report contains errors"));
    }
    Ok(())
}

pub struct ImportOptions<'a> {
    pub run_id: String,
    pub events_path: &'a Path,
    pub scenario: String,
    /// Declared `step -> admitted turn` pairs; `None` declares that the step
    /// produced no admission. Every stimulus step must be declared, except in a
    /// single-stimulus scenario, where the default gives that step the first
    /// admission (or none) and leaves later admissions unattributed.
    pub turn_map: Vec<(String, Option<u64>)>,
    /// Observer `metadata.json` of a continuous room recording, when one exists.
    pub room_metadata: Option<&'a Path>,
    /// Whether that recorder is independent of Lamp's own audio hardware.
    pub room_independent: bool,
}

/// Retain failed evidence as well as verified recordings. An imported final
/// report has no process exit receipt (`None`); a supervised capture must supply
/// its successful/failed exit explicitly. Neither metadata nor annotations can
/// replace the actual WAV.
pub fn room_evidence(
    metadata: &Value,
    independent: bool,
    source: &str,
    wav_path: &Path,
    recorder_success: Option<bool>,
) -> Value {
    let absolute = std::path::absolute(wav_path);
    let result = (|| -> Result<VerifiedRoom> {
        if metadata["valid"] != true || metadata["status"] != "captured_unscored" {
            return Err(invalid(
                "observer did not report a valid completed recording",
            ));
        }
        if recorder_success == Some(false) {
            return Err(invalid(
                "room recorder failed or did not exit within its bound",
            ));
        }
        let sha = metadata["room_wav_sha256"]
            .as_str()
            .ok_or_else(|| invalid("observer did not retain a recording hash"))?;
        let path = absolute
            .as_ref()
            .map_err(|error| invalid(&error.to_string()))?;
        path.to_str()
            .ok_or_else(|| invalid("room WAV path cannot be retained as UTF-8 JSON"))?;
        let verified = verify_room_wav(path, sha)?;
        check_observer_claims(metadata, &verified)?;
        Ok(verified)
    })();
    let mut evidence = json!({
        "sha256": metadata["room_wav_sha256"],
        "valid": result.is_ok(),
        "status": metadata["status"],
        "capture_requested_runner_us": metadata["start_requested_host_ns"].as_u64().map(|ns| ns / 1000),
        "independent_of_lamp": independent,
        "source": source,
        "wav_path": absolute.as_ref().ok().and_then(|path| path.to_str()),
        "recorder_success": recorder_success,
        "observer_valid": metadata["valid"],
        "verification_version": 1,
    });
    match result {
        Ok(verified) => evidence["verified_wav"] = json!(verified),
        Err(error) => evidence["verification_error"] = json!(error.to_string()),
    }
    evidence
}

/// Read final observer metadata without dropping an attempt when metadata or
/// audio is missing/corrupt. Its recording is always the sibling `room.wav`.
pub fn load_room_evidence(
    metadata_path: &Path,
    independent: bool,
    recorder_success: Option<bool>,
) -> Value {
    let wav_path = metadata_path.with_file_name("room.wav");
    let result = (|| -> Result<Value> {
        let (mut file, before) = open_regular(metadata_path, 2, MAX_METADATA_BYTES)?;
        let mut bytes = Vec::with_capacity(before.len() as usize);
        (&mut file)
            .take(MAX_METADATA_BYTES + 1)
            .read_to_end(&mut bytes)?;
        if bytes.len() as u64 != before.len()
            || file.metadata()?.modified().ok() != before.modified().ok()
        {
            return Err(invalid("observer metadata changed during bounded read"));
        }
        Ok(serde_json::from_slice(&bytes)?)
    })();
    match result {
        Ok(metadata) => room_evidence(
            &metadata,
            independent,
            &metadata_path.display().to_string(),
            &wav_path,
            recorder_success,
        ),
        Err(error) => json!({
            "valid": false, "sha256": null, "status": "unavailable_final_metadata",
            "source": metadata_path.display().to_string(),
            "wav_path": std::path::absolute(&wav_path).ok().and_then(|path| path.into_os_string().into_string().ok()),
            "independent_of_lamp": independent, "recorder_success": recorder_success,
            "verification_error": error.to_string(), "verification_version": 1,
        }),
    }
}

/// Revalidate at offline review time. A cached `valid: true` or `verified_wav`
/// object is only a prior receipt, never permission to skip reading the file.
pub fn verify_room_evidence(evidence: &Value) -> Result<VerifiedRoom> {
    if evidence["valid"] != true
        || evidence["observer_valid"] != true
        || evidence["status"] != "captured_unscored"
        || evidence["recorder_success"] == false
        || (evidence.get("recorder_exit").is_some() && evidence["recorder_exit"] != 0)
    {
        return Err(invalid(&format!(
            "room recording failed its integrity checks: {}",
            evidence["verification_error"]
                .as_str()
                .unwrap_or("invalid observer/recorder receipt")
        )));
    }
    let path = evidence["wav_path"]
        .as_str()
        .ok_or_else(|| invalid("room recording has no WAV path for integrity verification"))?;
    let sha = evidence["sha256"]
        .as_str()
        .ok_or_else(|| invalid("room recording has no retained hash"))?;
    verify_room_wav(Path::new(path), sha)
}

pub fn import_trace(
    plan: &LoadedPlan,
    catalog: &StimulusCatalog,
    options: &ImportOptions,
    run_directory: &Path,
) -> Result<AttemptRecord> {
    let scenario = plan.scenario(&options.scenario)?;
    let text = fs::read_to_string(options.events_path)?;
    if text.len() > 64 * 1024 * 1024 {
        return Err(invalid("trace exceeds 64 MiB"));
    }
    let (events, unmapped) = parse_trace(&text)?;
    let provider = events.iter().find_map(|e| match &e.kind {
        EventKind::RunStart { provider_kind } => provider_kind.clone(),
        _ => None,
    });
    let traced = match provider.as_deref() {
        Some("one_cached_reply") => ProviderKind::FixedReply,
        Some("gemini") => ProviderKind::Gemini,
        _ => return Err(invalid("trace has no recognizable run_start provider_kind")),
    };
    if traced != scenario.provider {
        return Err(invalid(
            "trace provider differs from the declared scenario provider",
        ));
    }
    let admitted: Vec<u64> = events
        .iter()
        .filter(|e| matches!(e.kind, EventKind::InputAdmitted { .. }))
        .filter_map(|e| e.turn)
        .collect();
    let stimuli: Vec<&str> = scenario
        .steps
        .iter()
        .filter(|s| s.scene.is_some())
        .map(|s| s.id.as_str())
        .collect();
    let mut turn_map = options.turn_map.clone();
    let mut assumption = "declared by the operator";
    if turn_map.is_empty() && stimuli.len() == 1 {
        assumption = "default: the only stimulus step owns the first admission; later admissions are unattributed";
        turn_map.push((stimuli[0].to_owned(), admitted.first().copied()));
    }
    for (step, turn) in &turn_map {
        let known_step = scenario.steps.iter().any(|s| &s.id == step);
        if !known_step || turn.is_some_and(|turn| !admitted.contains(&turn)) {
            return Err(invalid(&format!(
                "turn map entry {step}={turn:?} matches no step or admitted turn"
            )));
        }
    }
    // Undeclared stimuli would make every unattributed admission ambiguous.
    if let Some(missing) = stimuli
        .iter()
        .find(|id| !turn_map.iter().any(|(step, _)| step == *id))
    {
        return Err(invalid(&format!(
            "declare every stimulus step with --turn STEP=TURN or STEP=none; {missing} is missing"
        )));
    }
    let absent: Vec<String> = turn_map
        .iter()
        .filter(|(_, turn)| turn.is_none())
        .map(|(step, _)| step.clone())
        .collect();
    let turn_map: Vec<(String, u64)> = turn_map
        .into_iter()
        .filter_map(|(step, turn)| Some((step, turn?)))
        .collect();
    let mut steps = Vec::new();
    let mut previous_turn = None;
    for step in &scenario.steps {
        let mut record = StepRecord::planned(step, StepStatus::Injected);
        record.timing = step
            .scene
            .as_deref()
            .map(|scene| catalog.estimate(scene))
            .transpose()?;
        record.bound_turn = previous_turn;
        record.detail = Some("imported: stimulus timing not in the trace".into());
        if let Some((_, turn)) = turn_map.iter().find(|(id, _)| id == &step.id) {
            previous_turn = Some(*turn);
        }
        steps.push(record);
    }
    let room = match options.room_metadata {
        Some(path) => load_room_evidence(path, options.room_independent, None),
        None => Value::Null,
    };
    let record = AttemptRecord {
        attempt_id: format!("{}-import-{}", options.run_id, scenario.id),
        run_id: options.run_id.clone(),
        scenario: scenario.id.clone(),
        cohort: scenario.cohort,
        capability: scenario.capability,
        provider: scenario.provider,
        stratum: Stratum::ImportedTrace,
        repetition: 0,
        order: 0,
        profile: None,
        seed: None,
        step_domain: ClockDomain::LampMonotonic,
        steps,
        events,
        live_events: Vec::new(),
        clock: None,
        attribution: Attribution::Declared {
            turns: turn_map,
            absent,
        },
        status: AttemptStatus::Completed,
        spoken_failure_notice: None,
        evidence: json!({
            "trace_path": options.events_path.display().to_string(),
            "trace_sha256": hash(text.as_bytes()),
            "attribution_assumption": assumption,
            "room_audio": room,
        }),
        unmapped_trace_records: unmapped,
        reproduce: format!(
            "cargo run -p lamp-voice-eval -- import-trace --events {} --scenario {} --out NEW_DIR",
            options.events_path.display(),
            scenario.id
        ),
    };
    let mut ledger = Ledger::create(run_directory)?;
    ledger.append(
        "run_start",
        json!({"run_id": options.run_id, "plan_id": plan.plan.id, "plan_sha256": plan.sha256,
            "merged_catalog_sha256": catalog.merged_sha256, "backend": {"kind": "imported_trace"},
            "started_unix_ms": unix_ms()}),
        true,
    )?;
    ledger.append("attempt_planned", json!({"attempt_id": record.attempt_id, "scenario": scenario.id,
        "stratum": Stratum::ImportedTrace, "note": "imported after the fact; no stimulus was played by this tool"}), true)?;
    ledger.append(
        "attempt_finished",
        json!({"attempt_id": record.attempt_id, "record": record}),
        true,
    )?;
    ledger.append(
        "run_end",
        json!({"run_id": options.run_id, "attempts": 1, "finished_unix_ms": unix_ms()}),
        true,
    )?;
    Ok(record)
}
