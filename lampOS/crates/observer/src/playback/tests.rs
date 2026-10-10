use super::*;
use std::sync::atomic::AtomicUsize;

static NEXT: AtomicUsize = AtomicUsize::new(0);
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "lamp-output-test-{}-{}-{}",
            std::process::id(),
            lamp_ipc::monotonic_ns(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn wav(rate: u32, channels: u16, samples: &[i16]) -> Vec<u8> {
    let mut bytes = b"RIFF".to_vec();
    bytes.extend_from_slice(&(36 + samples.len() as u32 * 2).to_le_bytes());
    bytes.extend_from_slice(b"WAVEfmt ");
    bytes.extend_from_slice(&16_u32.to_le_bytes());
    bytes.extend_from_slice(&1_u16.to_le_bytes());
    bytes.extend_from_slice(&channels.to_le_bytes());
    bytes.extend_from_slice(&rate.to_le_bytes());
    bytes.extend_from_slice(&(rate * u32::from(channels) * 2).to_le_bytes());
    bytes.extend_from_slice(&(channels * 2).to_le_bytes());
    bytes.extend_from_slice(&16_u16.to_le_bytes());
    bytes.extend_from_slice(b"data");
    bytes.extend_from_slice(&(samples.len() as u32 * 2).to_le_bytes());
    for sample in samples {
        bytes.extend_from_slice(&sample.to_le_bytes());
    }
    bytes
}

fn sine(rate: u32, hz: f64, count: usize) -> Source {
    decode_source(&wav(
        rate,
        1,
        &(0..count)
            .map(|n| {
                (8192.0 * (2.0 * std::f64::consts::PI * hz * n as f64 / f64::from(rate)).sin())
                    as i16
            })
            .collect::<Vec<_>>(),
    ))
    .unwrap()
}

#[test]
fn exact_unique_output_is_required() {
    let names = vec!["Headphones".into(), OUTPUT_NAME.into()];
    assert_eq!(unique_output_index(&names, OUTPUT_NAME).unwrap(), 1);
    for name in [
        "",
        "default",
        "imac speakers",
        "iMac Speakers ",
        "Headphones",
    ] {
        assert!(unique_output_index(&names, name).is_err());
    }
    assert!(unique_output_index(&[], OUTPUT_NAME).is_err());
    assert!(unique_output_index(&[OUTPUT_NAME.into(), OUTPUT_NAME.into()], OUTPUT_NAME).is_err());
}

#[test]
fn bounded_pcm_rejects_incomplete_unsupported_and_silent_files() {
    let good = wav(16000, 1, &[1, 2, -3, 4]);
    for length in 0..good.len() {
        assert!(decode_source(&good[..length]).is_err());
    }
    let mut extra = good.clone();
    extra.push(0);
    assert!(decode_source(&extra).is_err());
    for (offset, value) in [(20, 3), (22, 3), (32, 9), (34, 24)] {
        let mut bad = good.clone();
        bad[offset] = value;
        assert!(decode_source(&bad).is_err(), "offset {offset}");
    }
    assert!(decode_source(&wav(7999, 1, &[1])).is_err());
    assert!(decode_source(&wav(192001, 1, &[1])).is_err());
    assert!(decode_source(&wav(16000, 2, &[1])).is_err());
    assert!(decode_source(&wav(16000, 1, &[])).is_err());
    assert!(decode_source(&wav(16000, 1, &[0, 0])).is_err());
    for rail in [i16::MIN, i16::MAX] {
        assert!(decode_source(&wav(16000, 1, &[1, rail])).is_err());
    }
    assert!(decode_source(&wav(8000, 1, &vec![1; 8000 * 120 + 1])).is_err());
    let mut huge = good;
    huge[40..44].copy_from_slice(&u32::MAX.to_le_bytes());
    assert!(decode_source(&huge).is_err());
}

#[test]
fn accepts_padded_metadata_and_preserves_exact_source_hash() {
    let mut bytes = wav(16000, 1, &[200, -200]);
    bytes.extend_from_slice(b"JUNK\x01\0\0\0x\0");
    let size = bytes.len() as u32 - 8;
    bytes[4..8].copy_from_slice(&size.to_le_bytes());
    let source = decode_source(&bytes).unwrap();
    assert_eq!(source.info.sha256, format!("{:x}", Sha256::digest(&bytes)));
    assert_eq!(source.info.frames, 2);
    for complete_chunk in [&bytes[12..36], &bytes[36..48]] {
        let mut duplicate = bytes.clone();
        duplicate.extend_from_slice(complete_chunk);
        let size = duplicate.len() as u32 - 8;
        duplicate[4..8].copy_from_slice(&size.to_le_bytes());
        assert!(decode_source(&duplicate).is_err());
    }
}

#[test]
fn opens_only_bounded_regular_non_symlink_files() {
    let temp = Temp::new();
    let path = temp.0.join("source.wav");
    let bytes = wav(16000, 1, &[100, -200]);
    std::fs::write(&path, &bytes).unwrap();
    assert_eq!(
        load_source(&path).unwrap().info.file_bytes,
        bytes.len() as u64
    );
    let link = temp.0.join("link.wav");
    std::os::unix::fs::symlink(&path, &link).unwrap();
    assert!(load_source(&link).is_err());
    let fifo = temp.0.join("fifo.wav");
    // rustix exposes mkfifoat on Linux but not macOS; this POSIX test helper
    // creates only a private fixture and never invokes an audio/device command.
    assert!(
        std::process::Command::new("mkfifo")
            .arg(&fifo)
            .status()
            .unwrap()
            .success()
    );
    assert!(load_source(&fifo).is_err());
    assert!(load_source(&temp.0).is_err());
    let oversized = temp.0.join("oversized.wav");
    File::create(&oversized)
        .unwrap()
        .set_len(MAX_WAV_BYTES + 1)
        .unwrap();
    assert!(load_source(&oversized).is_err());
    assert_eq!(std::fs::read(path).unwrap(), bytes);
}

#[test]
fn native_rate_preserves_samples_and_stereo_order() {
    let source = decode_source(&wav(48000, 2, &[1000, -2000, 3000, -4000])).unwrap();
    let prepared = prepare(&source, 48000, 2, || Ok(false)).unwrap().unwrap();
    assert_eq!(prepared.samples, source.samples);
    assert_eq!(prepared.info.rate_conversion, "none");
    assert!(prepare(&source, 48000, 1, || Ok(false)).is_err());
}

#[test]
fn mono_16k_to_stereo_48k_preserves_duration_pitch_and_identity() {
    let source = sine(16000, 1000.0, 1600);
    let prepared = prepare(&source, 48000, 2, || Ok(false)).unwrap().unwrap();
    assert_eq!(prepared.info.frames, 4800);
    assert_eq!(prepared.info.duration_s, source.info.duration_s);
    let mut error = 0.0_f64;
    for (frame, pair) in prepared
        .samples
        .chunks_exact(2)
        .enumerate()
        .skip(48)
        .take(4704)
    {
        assert_eq!(pair[0], pair[1]);
        let expected = 0.25 * (2.0 * std::f64::consts::PI * frame as f64 / 48.0).sin();
        error += (f64::from(pair[0]) - expected).powi(2);
    }
    assert!((error / 4704.0).sqrt() < 0.001);
    let again = prepare(&source, 48000, 2, || Ok(false)).unwrap().unwrap();
    assert_eq!(
        prepared.info.sha256_float32_le,
        again.info.sha256_float32_le
    );
    assert_ne!(prepared.info.sha256_float32_le, source.info.sha256);
}

#[test]
fn downsampling_rejects_alias_energy() {
    let source = sine(16000, 6000.0, 1600);
    let prepared = prepare(&source, 8000, 1, || Ok(false)).unwrap().unwrap();
    let rms = (prepared.samples[32..768]
        .iter()
        .map(|v| f64::from(*v).powi(2))
        .sum::<f64>()
        / 736.0)
        .sqrt();
    assert!(rms < 0.01, "aliased RMS {rms}");
}

#[test]
fn conversion_budget_cancel_and_peak_are_enforced() {
    let source = sine(16000, 1000.0, 1600);
    assert!(prepare(&source, 8001, 2, || Ok(false)).is_err());
    assert!(prepare(&source, 48000, 3, || Ok(false)).is_err());
    assert!(prepare(&source, 48000, 2, || Ok(true)).unwrap().is_none());
    let checks = std::cell::Cell::new(0);
    let longer = sine(16000, 1000.0, 3200);
    assert!(
        prepare(&longer, 48000, 2, || {
            checks.set(checks.get() + 1);
            Ok(checks.get() >= 2)
        })
        .unwrap()
        .is_none()
    );
    assert_eq!(
        checks.get(),
        2,
        "mid-prepare cancellation is checked before completion"
    );
    let mut oversized = sine(8000, 500.0, 8);
    oversized.info.frames = 8000 * 120;
    assert!(prepare(&oversized, 192000, 2, || Ok(false)).is_err());
    let mut invalid = sine(16000, 1000.0, 16);
    invalid.samples[1] = f32::NAN;
    assert!(prepare(&invalid, 16000, 1, || Ok(false)).is_err());
    invalid.samples[1] = 1.0;
    assert!(prepare(&invalid, 16000, 1, || Ok(false)).is_err());
}

fn renderer() -> Renderer {
    let source = decode_source(&wav(8000, 1, &[100, 200, 300, 400, 500, 600])).unwrap();
    Renderer {
        prepared: prepare(&source, 8000, 1, || Ok(false)).unwrap().unwrap(),
        shared: Arc::default(),
        cursor: 0,
        previous: None,
    }
}
fn time(index: u64) -> Times {
    Times {
        host_ns: 1_000_000_000 + index * 500_000,
        callback_ns: 2_000_000_000 + index * 500_000,
        device_ns: 2_001_000_000 + index * 500_000,
    }
}

#[test]
fn last_buffer_trim_tail_checks_and_estimated_drain_are_distinct() {
    let mut renderer = renderer();
    let mut output = [0.0; 4];
    renderer.render(&mut output, time(0), false);
    assert!(output.iter().all(|v| *v != 0.0));
    renderer.render(&mut output, time(1), false);
    assert_ne!(output[1], 0.0);
    assert_eq!(&output[2..], &[0.0, 0.0]);
    assert!(!renderer.shared.snapshot().complete(6));
    for i in 2..=3 {
        renderer.render(&mut output, time(i), false);
    }
    assert_eq!(renderer.shared.snapshot().tail_callbacks, 2);
    assert!(
        !renderer.shared.snapshot().complete(6),
        "estimated hardware drain still ahead"
    );
    renderer.render(&mut output, time(4), false);
    let snapshot = renderer.shared.snapshot();
    assert!(snapshot.complete(6));
    assert_eq!(snapshot.submitted_frames, 6);
    assert_eq!(snapshot.intentional_zero_frames, 14);
    assert_eq!(snapshot.rejected_frames, 0);
    renderer.render(&mut output, time(5), true);
    assert!(
        !renderer.shared.snapshot().complete(6),
        "late xrun invalidates complete frames"
    );
}

#[test]
fn faults_cancel_and_malformed_callbacks_submit_no_new_source() {
    for fault in 0..5 {
        let mut renderer = renderer();
        let mut output = [1.0; 4];
        match fault {
            0 => renderer.shared.cancelled.store(true, Ordering::Release),
            1 => renderer.shared.note_error(4),
            2 => {
                renderer
                    .shared
                    .callbacks
                    .store(MAX_CALLBACKS, Ordering::Release);
            }
            _ => {}
        }
        let mut timestamp = time(0);
        if fault == 3 {
            timestamp.callback_ns = 0;
        }
        renderer.render(&mut output, timestamp, fault == 4);
        assert_eq!(output, [0.0; 4]);
        assert_eq!(renderer.shared.snapshot().submitted_frames, 0);
    }
    let mut renderer = renderer();
    renderer.render(&mut vec![1.0; MAX_CALLBACK_FRAMES + 1], time(0), false);
    assert_ne!(renderer.shared.snapshot().faults & 2, 0);
    let source = decode_source(&wav(8000, 2, &[100; 8])).unwrap();
    renderer.prepared = prepare(&source, 8000, 2, || Ok(false)).unwrap().unwrap();
    renderer.render(&mut [1.0; 3], time(1), false);
    assert_eq!(renderer.shared.snapshot().submitted_frames, 0);
}

#[test]
fn cancel_after_partial_delivery_is_not_completion() {
    let mut renderer = renderer();
    renderer.render(&mut [0.0; 4], time(0), false);
    renderer.shared.cancelled.store(true, Ordering::Release);
    let mut output = [1.0; 4];
    renderer.render(&mut output, time(1), false);
    assert_eq!(output, [0.0; 4]);
    assert_eq!(renderer.shared.snapshot().submitted_frames, 4);
    assert!(!renderer.shared.snapshot().complete(6));
}

#[test]
fn stream_clock_loss_fails_but_host_arrival_jitter_is_diagnostic() {
    let mut renderer = renderer();
    renderer.render(&mut [0.0; 4], time(0), false);
    let mut delayed = time(1);
    delayed.host_ns += 15_000_000;
    renderer.render(&mut [0.0; 4], delayed, false);
    assert_eq!(renderer.shared.snapshot().faults, 0);
    let mut lost = time(3);
    lost.host_ns = delayed.host_ns + 1000;
    renderer.render(&mut [0.0; 4], lost, false);
    assert_eq!(renderer.shared.snapshot().timestamp_gaps, 1);
}

#[test]
fn controller_cancels_and_times_out_without_any_callback() {
    let state = Shared::default();
    assert_eq!(
        stop_decision(&state.snapshot(), 6, 1_000_000, 0, 1, true),
        Some("cancelled")
    );
    assert_eq!(
        stop_decision(&state.snapshot(), 6, 1_000_000, 5_000_000_000, 1, false),
        Some("first_callback_timeout")
    );
    assert_eq!(
        stop_decision(&state.snapshot(), 6, 1_000_000, 8_001_000_000, 1, false),
        Some("wall_deadline")
    );
    state.callbacks.store(1, Ordering::Release);
    state.last_host_ns.store(10, Ordering::Release);
    assert_eq!(
        stop_decision(&state.snapshot(), 6, 1_000_000, 1, 2_000_000_010, false),
        Some("callback_stall")
    );
}

#[test]
fn pre_cancel_and_invalid_wav_leave_final_failure_without_device_access() {
    let temp = Temp::new();
    let cancel = temp.0.join("cancel");
    std::fs::write(&cancel, b"").unwrap();
    let options = PlayOptions {
        output_device: OUTPUT_NAME.into(),
        wav: temp.0.join("missing.wav"),
        output: temp.0.join("cancelled"),
        cancel_file: Some(cancel),
    };
    let report = play(&options).unwrap();
    assert_eq!(report["status"], "cancelled_before_prepare");
    assert_eq!(report["audio_stream_opened"], false);
    assert_eq!(report["valid"], false);
    assert!(options.output.join("metadata.json").is_file());
    assert!(play(&options).is_err(), "never overwrite an attempt");
    let invalid = PlayOptions {
        output: temp.0.join("invalid"),
        cancel_file: None,
        ..options
    };
    let report = play(&invalid).unwrap();
    assert_eq!(report["status"], "failed");
    assert_eq!(report["audio_stream_opened"], false);
    assert_eq!(report["valid"], false);
}
