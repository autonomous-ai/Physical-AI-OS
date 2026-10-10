use crate::{
    RecordOptions, Result,
    capture::{self, Format, Ingress, Shared, Times},
    files::{self, Attempt, sink::Sink},
    unique_name_index,
};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::File,
    io::{self, Read},
    sync::{Arc, atomic::Ordering},
    time::{Duration, Instant},
};

fn identity(device: &cpal::Device) -> Result<Value> {
    Ok(json!({"name": device.to_string(), "id": device.id()?.to_string()}))
}

fn default_identity(host: &cpal::Host) -> Result<Value> {
    host.default_input_device()
        .as_ref()
        .map(identity)
        .transpose()
        .map(|v| v.unwrap_or(Value::Null))
}

fn input_devices(host: &cpal::Host) -> Result<Vec<cpal::Device>> {
    let devices: Vec<_> = host.input_devices()?.take(257).collect();
    if devices.len() > 256 {
        return Err(io::Error::other("input device inventory exceeds 256").into());
    }
    Ok(devices)
}

pub fn devices() -> Result<Value> {
    let host = cpal::default_host();
    let default = default_identity(&host)?;
    let mut rows = Vec::new();
    for device in input_devices(&host)? {
        let mut row = identity(&device)?;
        row["default_input_config"] = match device.default_input_config() {
            Ok(config) => {
                json!({"sample_rate": config.sample_rate(), "channels": config.channels(),
                "sample_format": config.sample_format().to_string(), "buffer_size": format!("{:?}", config.buffer_size())})
            }
            Err(error) => json!({"error": error.to_string()}),
        };
        rows.push(row);
    }
    Ok(
        json!({"host": "CoreAudio", "default_input": default, "inputs": rows,
        "permission": "not tested by enumeration; record requires microphone TCC access"}),
    )
}

fn build<T: cpal::SizedSample + Copy + 'static>(
    device: &cpal::Device,
    config: cpal::StreamConfig,
    mut ingress: Ingress,
    state: Arc<Shared>,
    convert: fn(T) -> f32,
) -> Result<cpal::Stream> {
    let errors = state.clone();
    Ok(device.build_input_stream(
        config,
        move |data: &[T], info: &cpal::CallbackInfo| {
            if info.xrun() {
                state.note_xrun();
            }
            let timestamp = info.timestamp();
            ingress.receive(
                data,
                Times {
                    host_monotonic_ns: lamp_ipc::monotonic_ns(),
                    capture_stream_ns: u64::try_from(timestamp.device.as_nanos()).unwrap_or(0),
                    callback_stream_ns: u64::try_from(timestamp.callback.as_nanos()).unwrap_or(0),
                },
                convert,
            );
        },
        move |error| {
            let kind = match error.kind() {
                cpal::ErrorKind::DeviceNotAvailable => 1,
                cpal::ErrorKind::PermissionDenied => 2,
                cpal::ErrorKind::StreamInvalidated => 4,
                cpal::ErrorKind::DeviceChanged => 8,
                _ => 16,
            };
            errors.note_stream_error(kind);
        },
        Some(Duration::from_secs(3)),
    )?)
}

fn file_hash(path: &std::path::Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut hash = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let size = file.read(&mut buffer)?;
        if size == 0 {
            break;
        }
        hash.update(&buffer[..size]);
    }
    Ok(format!("{:x}", hash.finalize()))
}

pub fn record(options: &RecordOptions, attempt: &Attempt) -> Result<Value> {
    let authorization_start = crate::authorization::snapshot()?;
    if authorization_start["capture_allowed"] != true {
        return Ok(
            json!({"valid": false, "status": "microphone_authorization_blocked",
            "stop_reason": "authorization_preflight", "authorization_at_start": authorization_start,
            "error": authorization_start["preflight_error"], "audio_stream_opened": false}),
        );
    }
    let host = cpal::default_host();
    let default_start = default_identity(&host)?;
    let devices = input_devices(&host)?;
    let index = unique_name_index(
        &devices.iter().map(ToString::to_string).collect::<Vec<_>>(),
        &options.input,
    )?;
    let device = &devices[index];
    let selected = identity(device)?;
    let supported = device.default_input_config()?;
    let sample_format = supported.sample_format();
    let config = supported.config();
    let format = Format {
        sample_rate: config.sample_rate,
        channels: config.channels,
    };
    let target = format.validate(options.seconds)?;
    let (ingress, consumer, state) = capture::channel(format, options.seconds)?;
    let sink = Sink::new(&attempt.directory, format)?;
    let worker_state = state.clone();
    let writer_deadline = Instant::now() + Duration::from_secs(u64::from(options.seconds) + 20);
    let worker = std::thread::Builder::new()
        .name("lamp-room-writer".into())
        .spawn(move || {
            let result = sink.consume(consumer, worker_state.clone(), writer_deadline);
            if result.is_err() {
                worker_state.writer_failed.store(true, Ordering::Release);
            }
            result
        })?;
    let build_started = lamp_ipc::monotonic_ns();
    let stream_result = match sample_format {
        cpal::SampleFormat::F32 => build(device, config, ingress, state.clone(), |x: f32| x),
        cpal::SampleFormat::F64 => build(device, config, ingress, state.clone(), |x: f64| x as f32),
        cpal::SampleFormat::I16 => build(device, config, ingress, state.clone(), |x: i16| {
            f32::from(x) / 32768.0
        }),
        cpal::SampleFormat::I32 => build(device, config, ingress, state.clone(), |x: i32| {
            x as f32 / 2_147_483_648.0
        }),
        cpal::SampleFormat::U16 => build(device, config, ingress, state.clone(), |x: u16| {
            (f32::from(x) - 32768.0) / 32768.0
        }),
        _ => Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "default device sample format is unsupported",
        )
        .into()),
    };
    let mut errors = Vec::new();
    let mut audio_stream_opened = false;
    let mut ready = false;
    let mut stop_reason = "stream_build_failed";
    let mut stream_buffer_frames = None;
    let mut final_stream_buffer_frames = None;
    let mut start_requested_ns = None;
    let mut stop_requested_ns = None;
    match stream_result {
        Err(error) => errors.push(error.to_string()),
        Ok(stream) => {
            audio_stream_opened = true;
            stream_buffer_frames = stream.buffer_size().ok();
            start_requested_ns = Some(lamp_ipc::monotonic_ns());
            match stream.start() {
                Err(error) => {
                    errors.push(error.to_string());
                    stop_reason = "stream_start_failed";
                }
                Ok(()) => {
                    let wait_started = Instant::now();
                    loop {
                        let now = lamp_ipc::monotonic_ns();
                        let snapshot = state.snapshot();
                        if !ready
                            && snapshot.written_frames > 0
                            && snapshot.nonzero_samples > 0
                            && snapshot.faults == 0
                            && !snapshot.writer_failed
                        {
                            let receipt = json!({"status": "capturing_not_yet_validated",
                                "attempt_id": attempt.id, "host_monotonic_ns": now,
                                "written_frames": snapshot.written_frames,
                                "first_callback_host_monotonic_ns": snapshot.first_host_ns,
                                "warning": "Final metadata determines recording integrity; no acoustic boundaries inferred."});
                            match files::write_json(&attempt.directory.join("ready.json"), &receipt)
                            {
                                Ok(()) => ready = true,
                                Err(error) => {
                                    errors.push(error.to_string());
                                    stop_reason = "ready_receipt_failed";
                                    break;
                                }
                            }
                        }
                        if snapshot.writer_failed {
                            stop_reason = "writer_failed";
                            break;
                        }
                        if snapshot.stream_errors > 0 {
                            stop_reason = "stream_error";
                            break;
                        }
                        if snapshot.done
                            && snapshot.tail_callbacks >= capture::REQUIRED_TAIL_CALLBACKS
                        {
                            stop_reason = "target_frames_received_and_tail_checked";
                            break;
                        }
                        if !ready && wait_started.elapsed() > Duration::from_secs(5) {
                            stop_reason =
                                if snapshot.received_frames > 0 && snapshot.nonzero_samples == 0 {
                                    "all_zero_audio_after_authorized_preflight"
                                } else {
                                    "readiness_timeout"
                                };
                            break;
                        }
                        let last_callback = snapshot.last_host_ns.max(snapshot.tail_last_host_ns);
                        if last_callback > 0 && now.saturating_sub(last_callback) > 2_000_000_000 {
                            stop_reason = "callback_stall";
                            break;
                        }
                        if wait_started.elapsed()
                            > Duration::from_secs(u64::from(options.seconds) + 8)
                        {
                            stop_reason = "duration_deadline";
                            break;
                        }
                        std::thread::sleep(Duration::from_millis(2));
                    }
                }
            }
            stop_requested_ns = Some(lamp_ipc::monotonic_ns());
            final_stream_buffer_frames = stream.buffer_size().ok();
            if let Err(error) = stream.pause() {
                errors.push(error.to_string());
            }
            drop(stream); // Stop callbacks before telling the worker it can finish draining.
        }
    }
    let stream_closed_ns = lamp_ipc::monotonic_ns();
    state.producer_stopped.store(true, Ordering::Release);
    let writer = match worker.join() {
        Ok(Ok(result)) => result,
        Ok(Err(error)) => {
            errors.push(error.to_string());
            Value::Null
        }
        Err(_) => {
            errors.push("observer writer panicked".into());
            Value::Null
        }
    };
    let snapshot = state.snapshot();
    if stop_reason == "all_zero_audio_after_authorized_preflight" {
        errors.push("Microphone authorization passed, but every captured sample was zero. Check input mute/gain, physical routing and the launcher's macOS privacy identity; silence alone does not identify the cause.".into());
    }
    let default_end = match default_identity(&host) {
        Ok(value) => value,
        Err(error) => {
            errors.push(error.to_string());
            Value::Null
        }
    };
    let wav_hash = match file_hash(&attempt.directory.join("room.wav")) {
        Ok(value) => Some(value),
        Err(error) => {
            errors.push(error.to_string());
            None
        }
    };
    let authorization_end = match crate::authorization::snapshot() {
        Ok(value) => value,
        Err(error) => {
            errors.push(error.to_string());
            Value::Null
        }
    };
    let valid = authorization_end["capture_allowed"] == true
        && ready
        && errors.is_empty()
        && stop_reason == "target_frames_received_and_tail_checked"
        && snapshot.valid_for(target)
        && writer["source_sequence_gaps"] == 0;
    Ok(json!({
        "authorization_at_start": authorization_start, "authorization_at_end": authorization_end,
        "audio_stream_opened": audio_stream_opened,
        "valid": valid, "status": if valid { "captured_unscored" } else { "invalid_capture" },
        "input_device": selected, "default_input_at_start": default_start,
        "default_input_at_end": default_end, "default_input_changed": default_start != default_end,
        "host": "CoreAudio", "delivered_format": {"sample_rate": format.sample_rate,
            "channels": format.channels, "input_sample_format": sample_format.to_string()},
        "wav_format": "IEEE-float32-interleaved-no-resampling-no-downmix-no-added-DSP",
        "buffer_request": "CoreAudio default", "backend_buffer_frames_at_start": stream_buffer_frames,
        "backend_buffer_frames_at_stop": final_stream_buffer_frames,
        "buffer_note": "Backend estimate; actual per-callback frame counts are in ledger.jsonl.",
        "requested_frames": target, "written_duration_s": snapshot.written_frames as f64 / f64::from(format.sample_rate),
        "build_started_host_ns": build_started, "start_requested_host_ns": start_requested_ns,
        "stop_requested_host_ns": stop_requested_ns, "stream_closed_host_ns": stream_closed_ns,
        "stop_reason": stop_reason, "ready_receipt_written": ready, "capture": snapshot, "writer": writer,
        "queue_capacity_packets": capture::QUEUE_PACKETS, "max_interleaved_samples_per_callback": capture::MAX_SAMPLES,
        "max_callbacks": capture::MAX_CALLBACKS, "timestamp_tolerance_ns": 2_000_000_000_u64.div_ceil(u64::from(format.sample_rate)),
        "clip_threshold": capture::CLIP_THRESHOLD,
        "fault_bit_definitions": {"1": "malformed_callback", "2": "oversized_callback", "4": "queue_full",
            "8": "timestamp_discontinuity", "16": "nonfinite_sample", "32": "digital_rail_clipping",
            "64": "stream_error", "128": "callback_count_limit", "256": "backend_xrun"},
        "stream_error_kind_bits": {"1": "device_unavailable", "2": "permission_denied",
            "4": "stream_invalidated", "8": "device_changed", "16": "other"},
        "room_wav_sha256": wav_hash,
        "tail_callback_note": "After the requested WAV frames, two later callback timestamps and backend xrun flags are checked; tail audio is not written.",
        "errors": errors,
        "limitations": ["CoreAudio/TCC and physical microphone identity require live qualification.",
            "CPAL capture time subtracts its device-latency estimate from CoreAudio host time; it is not word onset.",
            "No stimulus is played by this command; start external cached iMac playback only after ready.json.",
            "Valid means observed capture integrity, not audibility, speaker separation or conversational success.",
            "No silence is inserted for missing audio; an invalid WAV must not be scored as continuous.",
            "Nonfinite samples are replaced by zero and always invalidate the recording.",
            "TCC prompts, native calls and blocked filesystem I/O require an external process deadline."]
    }))
}
