use lamp_interaction::BootId;
use lamp_live::diagnostics::*;
use lamp_live::options::NoiseSuppression;
use sha2::{Digest, Sha256};
use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf, time::Duration};

static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
struct Private(PathBuf);
impl Private {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "lamp-diag-test-{}-{}-{}",
            std::process::id(),
            lamp_ipc::monotonic_ns(),
            NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
        ));
        fs::create_dir(&root).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        Self(root)
    }
    fn config(&self, stream: StreamKind) -> Config {
        Config::new(self.0.join("audio"), BootId::new([7; 16]).unwrap(), stream)
    }
}
impl Drop for Private {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn capture(at: u64, sequence: u64) -> CaptureMeta {
    CaptureMeta {
        epoch: 1,
        dsp_epoch: 1,
        privacy_generation: 2,
        frame_sequence: sequence,
        first_read_started_at_us: at,
        last_read_started_at_us: at,
        read_completed_at_us: at,
        successful_reads: 1,
        processing_completed_at_us: at,
        vad_score: 0.73,
        ..CaptureMeta::default()
    }
}
fn render(at: u64, first: u64, count: u64) -> RenderMeta {
    RenderMeta {
        playback_epoch: 1,
        privacy_generation: 2,
        first_sample: first,
        end_sample: first + count,
        accepted_at_us: at,
        queue_observed_at_us: at,
        queued_frames: count as i64,
        ..RenderMeta::default()
    }
}
fn end(after: u64) -> EndMeta {
    EndMeta {
        at_us: lamp_ipc::monotonic_us().max(after),
        reason: EndReason::Completed,
    }
}
fn rows(path: &std::path::Path) -> Vec<serde_json::Value> {
    fs::read_to_string(path.join("events.jsonl"))
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect()
}

#[test]
fn capture_files_preserve_pcm_offsets_hashes_and_full_acknowledgement() {
    let temp = Private::new();
    let config = temp.config(StreamKind::Capture);
    let path = config.directory.clone();
    let mut recorder = Recorder::start(config).unwrap();
    let at = recorder.started_at_us();
    assert_eq!(
        recorder.try_privacy(PrivacyMeta {
            at_us: at,
            generation: 2,
            open: true
        }),
        SubmitResult::Queued
    );
    let mut pre = [0i16; 160];
    pre[0] = i16::MIN;
    pre[159] = 1000;
    let post = [200i16; 160];
    assert_eq!(
        recorder.try_capture(capture(at + 1, 1), &pre, &post),
        SubmitResult::Queued
    );
    assert_eq!(
        recorder.try_capture(capture(at + 2, 2), &pre, &post),
        SubmitResult::Queued
    );
    let report = recorder.finish(end(at + 3), DEFAULT_FINISH_WAIT);
    assert!(report.valid, "{report:?}");
    assert_eq!(report.outcome, FinishOutcome::Complete);
    assert!(report.completion_marker_published);
    let marker = fs::read(path.join("complete.json")).unwrap();
    assert_eq!(
        report.completion_sha256.as_deref(),
        Some(format!("{:x}", Sha256::digest(&marker)).as_str())
    );
    let summary = report.summary.unwrap();
    assert_eq!(summary.capture_frames, 320);
    assert_eq!(summary.records_written, 4);
    let actual = fs::read(path.join("pre_aec.pcm16le")).unwrap();
    let expected: Vec<u8> = pre
        .iter()
        .chain(pre.iter())
        .flat_map(|v| v.to_le_bytes())
        .collect();
    assert_eq!(actual, expected);
    assert_eq!(
        summary.pre_aec_sha256,
        format!("{:x}", Sha256::digest(&actual))
    );
    let events = rows(&path);
    assert_eq!(events[1]["pre_aec_byte_offset"], 0);
    assert_eq!(events[2]["pre_aec_byte_offset"], 320);
    assert_eq!(events[1]["meta"]["vad_score"], serde_json::json!(0.73f32));
    assert!(
        fs::read_to_string(path.join("manifest.pending.json"))
            .unwrap()
            .contains("ALSA-resampled")
    );
    assert!(
        fs::read(path.join("render_accepted.pcm16le"))
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        fs::metadata(path).unwrap().permissions().mode() & 0o777,
        0o700
    );
}

#[test]
fn processing_provenance_matches_pending_result_and_acknowledged_summary() {
    for processing in [
        None,
        Some(NoiseSuppression::On.software_processing()),
        Some(NoiseSuppression::Off.software_processing()),
    ] {
        let temp = Private::new();
        let mut config = temp.config(StreamKind::Capture);
        config.software_processing = processing;
        let path = config.directory.clone();
        let mut recorder = Recorder::start(config).unwrap();
        let at = recorder.started_at_us();
        assert_eq!(
            recorder.try_privacy(PrivacyMeta {
                at_us: at,
                generation: 2,
                open: true
            }),
            SubmitResult::Queued
        );
        assert_eq!(
            recorder.try_capture(capture(at + 1, 1), &[321; 160], &[123; 160]),
            SubmitResult::Queued
        );
        let report = recorder.finish(end(at + 2), DEFAULT_FINISH_WAIT);
        assert!(report.valid, "{report:?}");
        let summary = report.summary.unwrap();
        assert_eq!(summary.software_processing, processing);
        let expected = serde_json::to_value(processing).unwrap();
        for name in ["manifest.pending.json", "result.json", "complete.json"] {
            let value: serde_json::Value =
                serde_json::from_slice(&fs::read(path.join(name)).unwrap()).unwrap();
            assert!(value.get("software_processing").is_some(), "{name}");
            assert_eq!(value["software_processing"], expected, "{name}");
        }
    }
}

#[test]
fn capture_processing_cannot_be_mislabelled_as_render_processing() {
    let temp = Private::new();
    let mut config = temp.config(StreamKind::Render);
    let path = config.directory.clone();
    config.software_processing = Some(NoiseSuppression::Off.software_processing());
    assert!(Recorder::start(config).is_err());
    assert!(
        !path.exists(),
        "invalid provenance must fail before creating files"
    );
}

#[test]
fn render_records_only_accepted_prefix_and_typed_priming_silence() {
    let temp = Private::new();
    let config = temp.config(StreamKind::Render);
    let path = config.directory.clone();
    let mut recorder = Recorder::start(config).unwrap();
    let at = recorder.started_at_us();
    recorder.try_privacy(PrivacyMeta {
        at_us: at,
        generation: 2,
        open: true,
    });
    assert_eq!(
        recorder.try_silence(render(at, 0, 240), SilenceKind::Prime, 240),
        SubmitResult::Queued
    );
    assert_eq!(
        recorder.try_silence(render(at + 1, 240, 240), SilenceKind::Prime, 240),
        SubmitResult::Queued
    );
    assert_eq!(
        recorder.try_render(render(at + 2, 480, 3), &[100, -200, 300]),
        SubmitResult::Queued
    );
    let report = recorder.finish(end(at + 3), DEFAULT_FINISH_WAIT);
    assert!(report.valid, "{report:?}");
    let summary = report.summary.unwrap();
    assert_eq!(summary.render_frames, 483);
    assert_eq!(summary.accepted_zero_frames, 480);
    let bytes = fs::read(path.join("render_accepted.pcm16le")).unwrap();
    assert_eq!(bytes.len(), 966);
    assert!(bytes[..960].iter().all(|v| *v == 0));
    assert_eq!(&bytes[960..], &[100, 0, 56, 255, 44, 1]);
    let events = rows(&path);
    assert_eq!(events[1]["accepted_silence_kind"], "prime");
    assert_eq!(events[3]["render_accepted_byte_offset"], 960);
}

#[test]
fn accepted_speech_gap_zeros_are_explicit_and_do_not_replace_neighboring_pcm() {
    let temp = Private::new();
    let config = temp.config(StreamKind::Render);
    let path = config.directory.clone();
    let mut recorder = Recorder::start(config).unwrap();
    let at = recorder.started_at_us();
    recorder.try_privacy(PrivacyMeta {
        at_us: at,
        generation: 2,
        open: true,
    });
    assert_eq!(
        recorder.try_render(render(at + 1, 0, 3), &[100, -200, 300]),
        SubmitResult::Queued
    );
    assert_eq!(
        recorder.try_silence(render(at + 2, 3, 240), SilenceKind::SpeechGap, 240),
        SubmitResult::Queued
    );
    assert_eq!(
        recorder.try_render(render(at + 3, 243, 2), &[400, -500]),
        SubmitResult::Queued
    );
    let report = recorder.finish(end(at + 4), DEFAULT_FINISH_WAIT);
    assert!(
        report.valid,
        "the recorder may certify an explicitly discontinuous audio timeline: {report:?}"
    );
    assert_eq!(report.summary.unwrap().accepted_zero_frames, 240);
    let bytes = fs::read(path.join("render_accepted.pcm16le")).unwrap();
    assert_eq!(&bytes[..6], &[100, 0, 56, 255, 44, 1]);
    assert!(bytes[6..486].iter().all(|byte| *byte == 0));
    assert_eq!(&bytes[486..], &[144, 1, 12, 254]);
    let events = rows(&path);
    assert_eq!(events[2]["accepted_silence_kind"], "speech_gap");
    assert_eq!(events[3]["render_accepted_byte_offset"], 486);
}

#[test]
fn capture_sequence_gap_and_unannounced_epoch_change_fail() {
    for epoch_change in [false, true] {
        let temp = Private::new();
        let config = temp.config(StreamKind::Capture);
        let path = config.directory.clone();
        let mut recorder = Recorder::start(config).unwrap();
        let at = recorder.started_at_us();
        recorder.try_privacy(PrivacyMeta {
            at_us: at,
            generation: 2,
            open: true,
        });
        recorder.try_capture(capture(at, 1), &[1; 160], &[2; 160]);
        let mut next = capture(at + 1, if epoch_change { 2 } else { 3 });
        if epoch_change {
            next.dsp_epoch = 2;
        }
        recorder.try_capture(next, &[1; 160], &[2; 160]);
        let report = recorder.finish(end(at + 2), DEFAULT_FINISH_WAIT);
        assert!(!report.valid);
        assert_ne!(report.faults, 0);
        assert!(!path.join("complete.json").exists());
        assert_eq!(fs::read(path.join("pre_aec.pcm16le")).unwrap().len(), 320);
    }
}

#[test]
fn render_cursor_gap_does_not_insert_silence() {
    let temp = Private::new();
    let config = temp.config(StreamKind::Render);
    let path = config.directory.clone();
    let mut recorder = Recorder::start(config).unwrap();
    let at = recorder.started_at_us();
    recorder.try_privacy(PrivacyMeta {
        at_us: at,
        generation: 2,
        open: true,
    });
    recorder.try_render(render(at, 0, 3), &[1, 2, 3]);
    recorder.try_render(render(at + 1, 4, 3), &[4, 5, 6]);
    let report = recorder.finish(end(at + 2), DEFAULT_FINISH_WAIT);
    assert!(!report.valid);
    assert_eq!(
        fs::read(path.join("render_accepted.pcm16le"))
            .unwrap()
            .len(),
        6
    );
    assert!(!path.join("complete.json").exists());
}

#[test]
fn explicit_reset_and_privacy_boundaries_are_retained_and_split_epochs() {
    let temp = Private::new();
    let config = temp.config(StreamKind::Capture);
    let path = config.directory.clone();
    let mut recorder = Recorder::start(config).unwrap();
    let at = recorder.started_at_us();
    recorder.try_privacy(PrivacyMeta {
        at_us: at,
        generation: 2,
        open: true,
    });
    recorder.try_capture(capture(at, 1), &[1; 160], &[2; 160]);
    recorder.try_privacy(PrivacyMeta {
        at_us: at + 1,
        generation: 3,
        open: false,
    });
    recorder.try_privacy(PrivacyMeta {
        at_us: at + 2,
        generation: 4,
        open: true,
    });
    recorder.try_reset(ResetMeta {
        at_us: at + 3,
        reason: ResetReason::CaptureRestart,
        capture_epoch: 2,
        dsp_epoch: 2,
        playback_epoch: 0,
        privacy_generation: 4,
        discarded: Some(DiscardedRange {
            playback_epoch: 1,
            retired_through: 3,
            accepted_through: 240,
        }),
    });
    let mut next = capture(at + 4, 1);
    next.epoch = 2;
    next.dsp_epoch = 2;
    next.privacy_generation = 4;
    recorder.try_capture(next, &[3; 160], &[4; 160]);
    let report = recorder.finish(end(at + 5), DEFAULT_FINISH_WAIT);
    assert!(report.valid, "{report:?}");
    let events = rows(&path);
    assert_eq!(events[4]["kind"], "reset");
    assert_eq!(events[4]["meta"]["discarded"]["accepted_through"], 240);
    assert_eq!(events[5]["pre_aec_byte_offset"], 320);
    assert_eq!(report.summary.unwrap().boundaries, 4);
}

#[test]
fn closed_privacy_cannot_be_overridden_by_pcm_metadata() {
    let temp = Private::new();
    let mut recorder = Recorder::start(temp.config(StreamKind::Capture)).unwrap();
    let at = recorder.started_at_us();
    recorder.try_privacy(PrivacyMeta {
        at_us: at,
        generation: 2,
        open: false,
    });
    recorder.try_capture(capture(at + 1, 1), &[1; 160], &[2; 160]);
    let report = recorder.finish(end(at + 2), DEFAULT_FINISH_WAIT);
    assert!(!report.valid);
    assert!(!temp.0.join("audio/complete.json").exists());
}

#[test]
fn no_overwrite_private_parent_and_symlink_rejection() {
    let temp = Private::new();
    let config = temp.config(StreamKind::Capture);
    let recorder = Recorder::start(config.clone()).unwrap();
    assert!(Recorder::start(config.clone()).is_err());
    drop(recorder);
    let target = temp.0.join("other");
    fs::create_dir(&target).unwrap();
    let link = temp.0.join("linked");
    std::os::unix::fs::symlink(&target, &link).unwrap();
    let mut linked = config.clone();
    linked.directory = link;
    assert!(Recorder::start(linked).is_err());
    let public = temp.0.join("public");
    fs::create_dir(&public).unwrap();
    fs::set_permissions(&public, fs::Permissions::from_mode(0o755)).unwrap();
    let mut bad = config;
    bad.directory = public.join("audio");
    assert!(Recorder::start(bad).is_err());
    assert!(!public.join("audio").exists());
}

#[test]
fn replaced_artifact_never_receives_original_pcm_or_valid_marker() {
    let temp = Private::new();
    let config = temp.config(StreamKind::Capture);
    let path = config.directory.clone();
    let mut recorder = Recorder::start(config).unwrap();
    let at = recorder.started_at_us();
    recorder.try_privacy(PrivacyMeta {
        at_us: at,
        generation: 2,
        open: true,
    });
    fs::rename(path.join("pre_aec.pcm16le"), path.join("original.pcm")).unwrap();
    let outside = temp.0.join("untouched");
    fs::write(&outside, b"do not overwrite").unwrap();
    std::os::unix::fs::symlink(&outside, path.join("pre_aec.pcm16le")).unwrap();
    recorder.try_capture(capture(at, 1), &[1; 160], &[2; 160]);
    let report = recorder.finish(end(at + 1), DEFAULT_FINISH_WAIT);
    assert!(!report.valid);
    assert_eq!(fs::read(outside).unwrap(), b"do not overwrite");
    assert!(!path.join("complete.json").exists());
}

#[test]
fn duration_and_frame_shape_limits_disable_evidence_without_blocking() {
    let temp = Private::new();
    let mut config = temp.config(StreamKind::Capture);
    config.max_seconds = 1;
    let mut recorder = Recorder::start(config).unwrap();
    let at = recorder.started_at_us();
    recorder.try_privacy(PrivacyMeta {
        at_us: at,
        generation: 2,
        open: true,
    });
    assert_eq!(
        recorder.try_capture(capture(at + 1_000_001, 1), &[1; 160], &[2; 160]),
        SubmitResult::DurationLimit
    );
    assert!(
        !recorder
            .finish(end(at + 1_000_001), DEFAULT_FINISH_WAIT)
            .valid
    );
    let temp = Private::new();
    let mut recorder = Recorder::start(temp.config(StreamKind::Render)).unwrap();
    let at = recorder.started_at_us();
    recorder.try_privacy(PrivacyMeta {
        at_us: at,
        generation: 2,
        open: true,
    });
    assert_eq!(
        recorder.try_render(render(at, 0, 241), &[1; 241]),
        SubmitResult::InvalidRecord
    );
    assert!(
        !recorder
            .finish(end(at + 1), Duration::from_millis(100))
            .valid
    );
}

#[test]
fn omitted_open_privacy_and_missing_opening_frames_are_invalid() {
    for case in 0..3 {
        let temp = Private::new();
        let mut recorder = Recorder::start(temp.config(if case == 2 {
            StreamKind::Render
        } else {
            StreamKind::Capture
        }))
        .unwrap();
        let at = recorder.started_at_us();
        if case != 0 {
            recorder.try_privacy(PrivacyMeta {
                at_us: at,
                generation: 2,
                open: true,
            });
        }
        if case == 2 {
            recorder.try_render(render(at, 1, 3), &[1, 2, 3]);
        } else {
            recorder.try_capture(
                capture(at, if case == 1 { 2 } else { 1 }),
                &[1; 160],
                &[2; 160],
            );
        }
        let report = recorder.finish(end(at + 1), DEFAULT_FINISH_WAIT);
        assert!(!report.valid, "case {case}");
        assert!(!temp.0.join("audio/complete.json").exists());
    }
}

#[test]
fn dsp_reset_preserves_capture_sequence_and_cannot_cover_a_gap() {
    for next_sequence in [1, 2, 3] {
        let temp = Private::new();
        let mut recorder = Recorder::start(temp.config(StreamKind::Capture)).unwrap();
        let at = recorder.started_at_us();
        recorder.try_privacy(PrivacyMeta {
            at_us: at,
            generation: 2,
            open: true,
        });
        recorder.try_capture(capture(at + 1, 1), &[1; 160], &[2; 160]);
        recorder.try_reset(ResetMeta {
            at_us: at + 2,
            reason: ResetReason::DspReset,
            capture_epoch: 1,
            dsp_epoch: 2,
            playback_epoch: 0,
            privacy_generation: 2,
            discarded: None,
        });
        let mut next = capture(at + 3, next_sequence);
        next.dsp_epoch = 2;
        recorder.try_capture(next, &[1; 160], &[2; 160]);
        let report = recorder.finish(end(at + 4), DEFAULT_FINISH_WAIT);
        assert_eq!(report.valid, next_sequence == 2, "{report:?}");
    }
}

#[test]
fn playback_reset_requires_matching_old_discard_and_advancing_epoch() {
    for case in 0..3 {
        let temp = Private::new();
        let mut recorder = Recorder::start(temp.config(StreamKind::Render)).unwrap();
        let at = recorder.started_at_us();
        recorder.try_privacy(PrivacyMeta {
            at_us: at,
            generation: 2,
            open: true,
        });
        recorder.try_render(render(at + 1, 0, 3), &[1, 2, 3]);
        recorder.try_reset(ResetMeta {
            at_us: at + 2,
            reason: ResetReason::PlaybackReset,
            capture_epoch: 0,
            dsp_epoch: 0,
            playback_epoch: if case == 2 { 1 } else { 2 },
            privacy_generation: 2,
            discarded: Some(DiscardedRange {
                playback_epoch: 1,
                retired_through: 1,
                accepted_through: if case == 1 { 4 } else { 3 },
            }),
        });
        let mut next = render(at + 3, 0, 3);
        next.playback_epoch = 2;
        recorder.try_silence(next, SilenceKind::Prime, 3);
        let report = recorder.finish(end(at + 4), DEFAULT_FINISH_WAIT);
        assert_eq!(report.valid, case == 0, "{report:?}");
    }
}

#[test]
fn audio_fault_end_is_incomplete_even_with_good_written_pcm() {
    let temp = Private::new();
    let mut recorder = Recorder::start(temp.config(StreamKind::Capture)).unwrap();
    let at = recorder.started_at_us();
    recorder.try_privacy(PrivacyMeta {
        at_us: at,
        generation: 2,
        open: true,
    });
    recorder.try_capture(capture(at + 1, 1), &[1; 160], &[2; 160]);
    let report = recorder.finish(
        EndMeta {
            at_us: at + 2,
            reason: EndReason::Fault,
        },
        DEFAULT_FINISH_WAIT,
    );
    assert!(!report.valid);
    assert!(!report.completion_marker_published);
    assert_eq!(report.summary.unwrap().capture_frames, 160);
    assert!(!temp.0.join("audio/complete.json").exists());
}

#[test]
fn replacement_directory_and_existing_final_marker_cannot_be_certified() {
    for directory_swap in [false, true] {
        let temp = Private::new();
        let config = temp.config(StreamKind::Capture);
        let path = config.directory.clone();
        let mut recorder = Recorder::start(config).unwrap();
        let at = recorder.started_at_us();
        recorder.try_privacy(PrivacyMeta {
            at_us: at,
            generation: 2,
            open: true,
        });
        if directory_swap {
            fs::rename(&path, temp.0.join("original")).unwrap();
            fs::create_dir(&path).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        } else {
            fs::write(path.join("complete.json"), b"existing marker").unwrap();
        }
        recorder.try_capture(capture(at + 1, 1), &[1; 160], &[2; 160]);
        let report = recorder.finish(end(at + 2), DEFAULT_FINISH_WAIT);
        assert!(!report.valid);
        if directory_swap {
            assert!(!path.join("pre_aec.pcm16le").exists());
        } else {
            assert_eq!(
                fs::read(path.join("complete.json")).unwrap(),
                b"existing marker"
            );
        }
    }
}

#[test]
fn repeated_privacy_observation_does_not_reset_the_render_cursor() {
    let temp = Private::new();
    let mut recorder = Recorder::start(temp.config(StreamKind::Render)).unwrap();
    let at = recorder.started_at_us();
    recorder.try_privacy(PrivacyMeta {
        at_us: at,
        generation: 2,
        open: true,
    });
    recorder.try_render(render(at + 1, 0, 3), &[1, 2, 3]);
    recorder.try_privacy(PrivacyMeta {
        at_us: at + 2,
        generation: 2,
        open: true,
    });
    recorder.try_render(render(at + 3, 0, 3), &[4, 5, 6]);
    let report = recorder.finish(end(at + 4), DEFAULT_FINISH_WAIT);
    assert!(!report.valid);
}
